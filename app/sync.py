from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from app import db
from app.config import sync_settings
from app.intelbras_client import DeviceError, fleet_for_pool, public_media_url

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_wake_event = threading.Event()
_thread: Optional[threading.Thread] = None
_cycle_lock = threading.Lock()
_status_lock = threading.Lock()
_labels: dict[str, Optional[str]] = {
    "peer": None,
    "peer_class": None,
    "cloud": None,
    "cloud_class": None,
    "cloud_alert": None,
}
_bucket_ready = False

PHOTO_BUCKET = "fotos"


@dataclass
class MemberSnapshot:
    name: str
    registration: str
    device_user_id: Optional[int]
    photo_sha256: Optional[str]
    photo_filename: Optional[str]
    medical_exam_expires_at: datetime
    access_blocked: bool
    created_at: datetime
    updated_at: datetime
    pool: Optional[str] = None


def merge_snapshots(
    local: Optional[MemberSnapshot],
    remote: MemberSnapshot,
) -> tuple[MemberSnapshot, bool]:
    """Junta dois cadastros. A data de exame mais longa vence.

    Nome, bloqueio e foto seguem o updated_at mais recente. Uma renovação
    (exame mais longo e desbloqueado) desfaz um bloqueio feito com a data antiga.
    O bool indica que o facial precisa receber de novo quem estava bloqueado.
    """
    if local is None:
        return remote, False

    expires = max(local.medical_exam_expires_at, remote.medical_exam_expires_at)
    if remote.updated_at >= local.updated_at:
        winner, other = remote, local
    else:
        winner, other = local, remote

    blocked = winner.access_blocked
    if expires > local.medical_exam_expires_at and not remote.access_blocked:
        blocked = False
    if expires > remote.medical_exam_expires_at and not local.access_blocked:
        blocked = False

    if winner.photo_sha256:
        photo_sha = winner.photo_sha256
        photo_name = winner.photo_filename or other.photo_filename
    else:
        photo_sha = other.photo_sha256
        photo_name = other.photo_filename

    device_user_id = winner.device_user_id
    if device_user_id is None:
        device_user_id = other.device_user_id
    pool = winner.pool or other.pool

    merged = MemberSnapshot(
        name=winner.name,
        registration=local.registration,
        device_user_id=device_user_id,
        photo_sha256=photo_sha,
        photo_filename=photo_name or (f"{photo_sha}.jpg" if photo_sha else None),
        medical_exam_expires_at=expires,
        access_blocked=blocked,
        created_at=min(local.created_at, remote.created_at),
        updated_at=max(local.updated_at, remote.updated_at),
        pool=pool,
    )
    needs_unblock = bool(
        local.access_blocked
        and not merged.access_blocked
        and merged.medical_exam_expires_at > datetime.now()
    )
    return merged, needs_unblock


def status_view() -> dict[str, str]:
    settings = sync_settings()
    with _status_lock:
        peer = _labels["peer"]
        peer_class = _labels["peer_class"]
        cloud = _labels["cloud"]
        cloud_class = _labels["cloud_class"]
        cloud_alert = _labels["cloud_alert"]
    if peer is None:
        if settings.peer_url:
            peer, peer_class = "sem contato", "is-wait"
        else:
            peer, peer_class = "não configurado", "is-muted"
    if not settings.cloud_enabled:
        cloud, cloud_class, cloud_alert = "não configurada", "is-muted", ""
    elif cloud is None:
        cloud, cloud_class = "sem internet", "is-wait"
        cloud_alert = cloud_alert or (
            "A nuvem ainda não recebeu os dados deste PC. "
            "O envio será tentado de novo automaticamente."
        )
    elif cloud_class == "is-ok":
        cloud_alert = ""
    elif not cloud_alert:
        cloud_alert = (
            "A nuvem parou de receber atualizações. "
            "Os dados locais continuam neste PC e o envio será tentado de novo."
        )
    return {
        "peer": peer or "não configurado",
        "peer_class": peer_class or "is-muted",
        "cloud": cloud or "não configurada",
        "cloud_class": cloud_class or "is-muted",
        "cloud_alert": cloud_alert or "",
    }


def build_state() -> dict:
    return {
        "members": [member_payload(member) for member in db.list_members()],
        "events": [event_payload(event) for event in db.list_events_for_sync()],
        "deletions": [
            {"registration": registration, "deleted_at": _iso(deleted_at)}
            for registration, deleted_at in db.list_deletions()
        ],
    }


def member_payload(member: db.Member) -> dict:
    digest = db.file_sha256(member.photo_path)
    if digest and digest != member.photo_sha256:
        db.set_photo_sha256(member.id, digest)
    elif not digest:
        digest = member.photo_sha256
    filename = Path(member.photo_path).name if member.photo_path else None
    if digest and not filename:
        filename = f"{digest}.jpg"
    return {
        "registration": member.registration,
        "name": member.name,
        "device_user_id": member.device_user_id,
        "photo_sha256": digest,
        "photo_filename": filename,
        "medical_exam_expires_at": _iso(member.medical_exam_expires_at),
        "access_blocked": bool(member.access_blocked),
        "created_at": _iso(member.created_at),
        "updated_at": _iso(member.updated_at),
        "pool": member.pool,
    }


def event_payload(event: db.AccessEvent) -> dict:
    created = event.created_at or event.event_at
    return {
        "sync_id": event.sync_id,
        "event_at": _iso(event.event_at),
        "member_name": event.member_name,
        "registration": event.registration,
        "result": event.result,
        "source": event.source,
        "detail": event.detail,
        "external_id": event.external_id,
        "created_at": _iso(created),
    }


def run_sync_cycle() -> dict[str, dict[str, object]]:
    """Troca com o outro PC e com a nuvem. O retorno é o resumo do botão de teste."""
    with _cycle_lock:
        settings = sync_settings()
        peer = _exchange_with_peer(settings)

        remote_members: Optional[dict[str, MemberSnapshot]] = None
        remote_event_ids: Optional[set[str]] = None
        remote_deletions: Optional[dict[str, datetime]] = None

        if settings.cloud_enabled:
            try:
                remote_members, remote_event_ids, remote_deletions = _pull_cloud(
                    settings.supabase_url,
                    settings.supabase_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Leitura da nuvem falhou: %s", exc)
                _remember_cloud_failure(exc, action="ler a nuvem")
        else:
            _set_cloud("não configurada", "is-muted", "")

        _apply_pending_unblocks()

        if remote_members is not None and remote_event_ids is not None and settings.cloud_enabled:
            try:
                _push_cloud(
                    settings.supabase_url,
                    settings.supabase_key,
                    remote_members,
                    remote_event_ids,
                    remote_deletions or {},
                )
                _set_cloud("backup em dia", "is-ok", "")
                cloud = _cloud_ok_report(len(remote_members))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Envio para a nuvem falhou: %s", exc)
                _remember_cloud_failure(exc, action="enviar para a nuvem")
                cloud = _cloud_fail_report()
        elif settings.cloud_enabled:
            cloud = _cloud_fail_report()
        else:
            cloud = {
                "ok": False,
                "configured": False,
                "label": "não configurada",
                "css": "is-muted",
                "detail": (
                    "SUPABASE_URL ou SUPABASE_KEY está vazio. "
                    "O backup na nuvem fica desligado."
                ),
            }

        return {"peer": peer, "cloud": cloud}


def refresh_from_peers() -> None:
    """Puxa o outro PC e a nuvem antes de bloquear exames vencidos."""
    run_sync_cycle()


def request_sync() -> None:
    """Pede um envio agora, depois de uma alteração local ou quando a internet voltar."""
    start_sync_worker()
    _wake_event.set()


def start_sync_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker, name="sync-worker", daemon=True)
    _thread.start()


def stop_sync_worker() -> None:
    _stop_event.set()


def _worker() -> None:
    logger.info("Sincronização entre PCs iniciada")
    while not _stop_event.is_set():
        try:
            run_sync_cycle()
        except Exception as exc:
            logger.exception("Erro no ciclo de sincronização")
            if sync_settings().cloud_enabled:
                _remember_cloud_failure(exc, action="sincronizar com a nuvem")
        interval = sync_settings().interval
        deadline = time.monotonic() + interval
        while not _stop_event.is_set():
            if _wake_event.is_set():
                _wake_event.clear()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _stop_event.wait(min(0.4, remaining))


def _exchange_with_peer(settings) -> dict[str, object]:
    if not settings.peer_url:
        _set_peer("não configurado", "is-muted")
        return {
            "ok": False,
            "configured": False,
            "label": "não configurado",
            "css": "is-muted",
            "detail": "SYNC_PEER_URL está vazio. A troca com o outro PC fica desligada.",
        }
    if not settings.token:
        logger.warning("SYNC_PEER_URL definido sem SYNC_TOKEN")
        _set_peer("sem token", "is-bad")
        return {
            "ok": False,
            "configured": False,
            "label": "sem token",
            "css": "is-bad",
            "detail": "SYNC_PEER_URL está preenchido, mas falta o SYNC_TOKEN neste PC.",
        }
    url = settings.peer_url
    try:
        count = _pull_lan(url, settings.token)
    except Exception as exc:  # noqa: BLE001 — o PC local segue sem o outro
        logger.warning("Sync com o outro PC falhou: %s", exc)
        label, css, detail = _peer_failure(exc, url)
        _set_peer(label, css)
        return {
            "ok": False,
            "configured": True,
            "label": label,
            "css": css,
            "detail": detail,
        }
    _set_peer("atualizado", "is-ok")
    if count == 0:
        detail = (
            f"Conectou em {url}. O outro PC respondeu e ainda não tem cadastros. "
            "A ligação está funcionando. No outro computador, clique em "
            "Testar sincronização para conferir o caminho de volta."
        )
    else:
        detail = (
            f"Conectou em {url}. Foram conferidos {count} cadastro(s) do outro PC. "
            "No outro computador, clique em Testar sincronização para conferir o caminho de volta."
        )
    return {
        "ok": True,
        "configured": True,
        "label": "atualizado",
        "css": "is-ok",
        "detail": detail,
    }


def _peer_failure(exc: Exception, url: str) -> tuple[str, str, str]:
    if isinstance(exc, httpx.TimeoutException):
        return (
            "sem contato",
            "is-wait",
            (
                f"O outro PC em {url} não respondeu a tempo. "
                "Confira se o programa está aberto lá e se o firewall libera a porta 5000."
            ),
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            "sem contato",
            "is-wait",
            (
                f"Não foi possível alcançar {url}. "
                "Confira o IP no SYNC_PEER_URL e se os dois PCs estão na mesma rede."
            ),
        )
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 401:
            return (
                "token recusado",
                "is-bad",
                "O outro PC recusou o SYNC_TOKEN. O segredo precisa ser igual nos dois .env.",
            )
        return (
            "sem contato",
            "is-bad",
            f"O outro PC em {url} respondeu com erro {exc.response.status_code}.",
        )
    return ("sem contato", "is-wait", f"Falha ao falar com {url}: {exc}")


def _cloud_ok_report(remote_count: int) -> dict[str, object]:
    if remote_count == 0:
        detail = "Conectou na nuvem e enviou o backup deste PC. A nuvem ainda não tinha cadastros."
    else:
        detail = (
            f"Conectou na nuvem e enviou o backup deste PC. "
            f"A nuvem já tinha {remote_count} cadastro(s)."
        )
    return {
        "ok": True,
        "configured": True,
        "label": "backup em dia",
        "css": "is-ok",
        "detail": detail,
    }


def _cloud_fail_report() -> dict[str, object]:
    status = status_view()
    return {
        "ok": False,
        "configured": True,
        "label": status["cloud"],
        "css": status["cloud_class"],
        "detail": status["cloud_alert"] or status["cloud"],
    }


def _pull_lan(peer_url: str, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=8.0) as client:
        response = client.get(f"{peer_url}/sync/estado", headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("resposta do outro PC sem estado")

        _apply_deletion_list(payload.get("deletions") or [])

        member_count = 0
        for item in payload.get("members") or []:
            if not isinstance(item, dict):
                continue
            remote = _snapshot_from_payload(item)
            if remote is None:
                continue
            member_count += 1

            def fetch(snapshot: MemberSnapshot = remote) -> Optional[bytes]:
                filename = snapshot.photo_filename
                if not filename or not snapshot.photo_sha256:
                    return None
                local = db.get_member_by_registration(snapshot.registration)
                if _local_has_photo(local, snapshot.photo_sha256):
                    return None
                photo = client.get(
                    f"{peer_url}/sync/foto/{quote(filename)}",
                    headers=headers,
                )
                if photo.status_code == 404:
                    return None
                photo.raise_for_status()
                return photo.content

            _apply_remote_member(remote, fetch)

        for item in payload.get("events") or []:
            if isinstance(item, dict):
                _apply_event(item)
        return member_count


def _pull_cloud(
    base_url: str,
    key: str,
) -> tuple[dict[str, MemberSnapshot], set[str], dict[str, datetime]]:
    headers = _cloud_headers(key)
    members: dict[str, MemberSnapshot] = {}
    event_ids: set[str] = set()
    with httpx.Client(timeout=20.0) as client:
        _ensure_photo_bucket(client, base_url, key)
        remote_deletions = _apply_deletion_list(
            _paged(client, f"{base_url}/rest/v1/member_deletions", headers)
        )

        for item in _paged(client, f"{base_url}/rest/v1/members", headers):
            remote = _snapshot_from_payload(item)
            if remote is None:
                continue
            members[remote.registration] = remote

            def fetch(snapshot: MemberSnapshot = remote) -> Optional[bytes]:
                if not snapshot.photo_sha256:
                    return None
                local = db.get_member_by_registration(snapshot.registration)
                if _local_has_photo(local, snapshot.photo_sha256):
                    return None
                photo = client.get(
                    f"{base_url}/storage/v1/object/{PHOTO_BUCKET}/{snapshot.photo_sha256}.jpg",
                    headers=_cloud_headers(key, json_body=False),
                )
                if photo.status_code == 404:
                    return None
                photo.raise_for_status()
                return photo.content

            _apply_remote_member(remote, fetch)

        for item in _paged(client, f"{base_url}/rest/v1/access_events", headers):
            sync_id = str(item.get("sync_id") or "").strip()
            if sync_id:
                event_ids.add(sync_id)
            _apply_event(item)
    return members, event_ids, remote_deletions


def _push_cloud(
    base_url: str,
    key: str,
    remote_members: dict[str, MemberSnapshot],
    remote_event_ids: set[str],
    remote_deletions: dict[str, datetime],
) -> None:
    headers = _cloud_headers(key)
    to_upsert: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        _ensure_photo_bucket(client, base_url, key)
        deletion_rows: list[dict] = []
        for registration, deleted_at in db.list_deletions():
            remote_when = remote_deletions.get(registration)
            if remote_when is None or deleted_at > remote_when:
                deletion_rows.append(
                    {"registration": registration, "deleted_at": _iso(deleted_at)}
                )
            member = db.get_member_by_registration(registration)
            if member is None or member.updated_at <= deleted_at:
                _delete_cloud_member(client, base_url, key, registration)
        for chunk in _chunks(deletion_rows, 100):
            _upsert(
                client,
                f"{base_url}/rest/v1/member_deletions?on_conflict=registration",
                headers,
                chunk,
            )

        for member in db.list_members():
            deleted_at = db.get_deletion(member.registration)
            if deleted_at and deleted_at >= member.updated_at:
                continue
            payload = member_payload(member)
            remote = remote_members.get(member.registration)
            if remote is not None and not _member_differs(payload, remote):
                continue
            digest = payload.get("photo_sha256")
            if digest and member.photo_path and Path(member.photo_path).is_file():
                remote_digest = remote.photo_sha256 if remote else None
                if digest != remote_digest:
                    data = Path(member.photo_path).read_bytes()
                    if hashlib.sha256(data).hexdigest() == digest:
                        _upload_photo(client, base_url, key, digest, data)
            to_upsert.append(payload)
        for chunk in _chunks(to_upsert, 100):
            _upsert(client, f"{base_url}/rest/v1/members?on_conflict=registration", headers, chunk)

        events: list[dict] = []
        for event in db.list_events_for_sync():
            if not event.sync_id or event.sync_id in remote_event_ids:
                continue
            events.append(event_payload(event))
        for chunk in _chunks(events, 100):
            _upsert(
                client,
                f"{base_url}/rest/v1/access_events?on_conflict=sync_id",
                headers,
                chunk,
            )


def _apply_remote_member(
    remote: MemberSnapshot,
    fetch_photo: Callable[[], Optional[bytes]],
) -> None:
    deleted_at = db.get_deletion(remote.registration)
    if deleted_at and deleted_at >= remote.updated_at.replace(microsecond=0):
        db.record_deletion(remote.registration, deleted_at)
        return
    local = db.get_member_by_registration(remote.registration)
    local_snap = _snapshot_from_member(local) if local else None
    merged, needs_unblock = merge_snapshots(local_snap, remote)
    photo_path = local.photo_path if local else None
    current_hash = db.file_sha256(photo_path)
    if merged.photo_sha256 and merged.photo_sha256 != current_hash:
        data = fetch_photo()
        if data and hashlib.sha256(data).hexdigest() == merged.photo_sha256:
            photo_path = str(_write_photo(merged.photo_filename, merged.photo_sha256, data))
        elif current_hash:
            merged = MemberSnapshot(
                name=merged.name,
                registration=merged.registration,
                device_user_id=merged.device_user_id,
                photo_sha256=current_hash,
                photo_filename=Path(photo_path).name if photo_path else merged.photo_filename,
                medical_exam_expires_at=merged.medical_exam_expires_at,
                access_blocked=merged.access_blocked,
                created_at=merged.created_at,
                updated_at=merged.updated_at,
                pool=merged.pool,
            )
    db.upsert_synced_member(
        name=merged.name,
        registration=merged.registration,
        device_user_id=merged.device_user_id,
        photo_path=photo_path,
        photo_sha256=merged.photo_sha256,
        expires_at=merged.medical_exam_expires_at,
        access_blocked=merged.access_blocked,
        created_at=merged.created_at,
        updated_at=merged.updated_at,
        mark_unblock_pending=needs_unblock,
        pool=merged.pool,
    )


def _apply_event(item: dict) -> None:
    sync_id = str(item.get("sync_id") or "").strip()
    if not sync_id:
        return
    external_id = item.get("external_id") or None
    if isinstance(external_id, str):
        external_id = external_id.strip() or None
    try:
        event_at = _parse_iso(item.get("event_at"))
        created_at = _parse_iso(item.get("created_at"))
    except ValueError:
        logger.warning("Evento remoto com data inválida: %s", sync_id)
        return
    db.log_access_event(
        result=str(item.get("result") or "approved"),
        member_name=_optional_text(item.get("member_name")),
        registration=_optional_text(item.get("registration")),
        detail=_optional_text(item.get("detail")),
        source=str(item.get("source") or "sync"),
        event_at=event_at,
        external_id=external_id,
        sync_id=sync_id,
        created_at=created_at,
    )
    if str(item.get("result") or "") == "deleted":
        registration = _optional_text(item.get("registration"))
        when = created_at or event_at
        if registration and when:
            db.record_deletion(registration, when)


def _apply_deletion_list(items: object) -> dict[str, datetime]:
    applied: dict[str, datetime] = {}
    if not isinstance(items, list):
        return applied
    for item in items:
        if not isinstance(item, dict):
            continue
        registration = str(item.get("registration") or "").strip()
        if not registration:
            continue
        try:
            deleted_at = _parse_iso(item.get("deleted_at"))
        except ValueError:
            logger.warning("Exclusão remota com data inválida: %s", registration)
            continue
        if deleted_at is None:
            continue
        db.record_deletion(registration, deleted_at)
        applied[registration] = deleted_at
    return applied


def _delete_cloud_member(client: httpx.Client, base_url: str, key: str, registration: str) -> None:
    response = client.delete(
        f"{base_url}/rest/v1/members",
        params={"registration": f"eq.{registration}"},
        headers=_cloud_headers(key, json_body=False),
    )
    if response.status_code not in {200, 204, 404}:
        response.raise_for_status()


def _apply_pending_unblocks() -> None:
    for member in db.list_unblock_pending():
        if member.access_blocked or member.is_expired:
            db.clear_device_pending(member.id)
            continue
        photo = Path(member.photo_path) if member.photo_path else None
        if photo is not None and not photo.is_file():
            photo = None
        face_url = public_media_url(photo.name) if photo is not None else None
        try:
            created = fleet_for_pool(member.pool).renew_member(
                name=member.name,
                registration=member.registration,
                image_path=photo,
                previous_device_user_id=member.device_user_id,
                face_public_url=face_url,
            )
        except DeviceError as exc:
            logger.error(
                "Não foi possível reabrir %s no facial após a sincronização: %s",
                member.registration,
                exc,
            )
            continue
        if created.device_user_id and created.device_user_id != member.device_user_id:
            db.update_device_user_id(member.id, created.device_user_id)
        db.clear_device_pending(member.id)
        logger.info(
            "Exame reaplicado no facial após sincronização: %s",
            member.registration,
        )


def _snapshot_from_member(member: db.Member) -> MemberSnapshot:
    filename = Path(member.photo_path).name if member.photo_path else None
    return MemberSnapshot(
        name=member.name,
        registration=member.registration,
        device_user_id=member.device_user_id,
        photo_sha256=member.photo_sha256,
        photo_filename=filename,
        medical_exam_expires_at=member.medical_exam_expires_at,
        access_blocked=member.access_blocked,
        created_at=member.created_at,
        updated_at=member.updated_at,
        pool=member.pool,
    )


def _snapshot_from_payload(item: dict) -> Optional[MemberSnapshot]:
    registration = str(item.get("registration") or "").strip()
    name = str(item.get("name") or "").strip()
    if not registration or not name:
        return None
    try:
        expires = _parse_iso(item.get("medical_exam_expires_at"))
        created = _parse_iso(item.get("created_at")) or expires
        updated = _parse_iso(item.get("updated_at")) or created
    except ValueError:
        logger.warning("Cadastro remoto com data inválida: %s", registration)
        return None
    if expires is None or created is None or updated is None:
        return None
    device_user_id = item.get("device_user_id")
    if device_user_id in ("", None):
        parsed_device_id: Optional[int] = None
    else:
        try:
            parsed_device_id = int(device_user_id)
        except (TypeError, ValueError):
            parsed_device_id = None
    photo_sha = _optional_text(item.get("photo_sha256"))
    photo_name = _optional_text(item.get("photo_filename"))
    if photo_name:
        photo_name = Path(photo_name).name
    return MemberSnapshot(
        name=name,
        registration=registration,
        device_user_id=parsed_device_id,
        photo_sha256=photo_sha,
        photo_filename=photo_name,
        medical_exam_expires_at=expires,
        access_blocked=_as_bool(item.get("access_blocked")),
        created_at=created,
        updated_at=updated,
        pool=_optional_text(item.get("pool")),
    )


def _member_differs(payload: dict, remote: MemberSnapshot) -> bool:
    remote_payload = {
        "name": remote.name,
        "device_user_id": remote.device_user_id,
        "photo_sha256": remote.photo_sha256,
        "medical_exam_expires_at": _iso(remote.medical_exam_expires_at),
        "access_blocked": remote.access_blocked,
        "updated_at": _iso(remote.updated_at),
        "pool": remote.pool,
    }
    local_compare = {
        "name": payload.get("name"),
        "device_user_id": payload.get("device_user_id"),
        "photo_sha256": payload.get("photo_sha256"),
        "medical_exam_expires_at": payload.get("medical_exam_expires_at"),
        "access_blocked": bool(payload.get("access_blocked")),
        "updated_at": payload.get("updated_at"),
        "pool": payload.get("pool"),
    }
    return local_compare != remote_payload


def _local_has_photo(member: Optional[db.Member], digest: str) -> bool:
    if member is None or not member.photo_path:
        return False
    return db.file_sha256(member.photo_path) == digest


def _write_photo(filename: Optional[str], digest: str, data: bytes) -> Path:
    from app.config import PHOTOS_DIR, ensure_directories

    ensure_directories()
    safe = Path(filename or "").name
    if not safe or safe in {".", ".."}:
        safe = f"{digest}.jpg"
    dest = PHOTOS_DIR / safe
    if dest.exists() and db.file_sha256(str(dest)) not in {None, digest}:
        dest = PHOTOS_DIR / f"{digest}.jpg"
    dest.write_bytes(data)
    return dest


def _paged(client: httpx.Client, url: str, headers: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        response = client.get(
            url,
            params={"select": "*", "limit": 1000, "offset": offset},
            headers=headers,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError("resposta da nuvem sem lista")
        rows.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 1000:
            return rows
        offset += 1000


def _upsert(client: httpx.Client, url: str, headers: dict[str, str], rows: list[dict]) -> None:
    if not rows:
        return
    prefer = dict(headers)
    prefer["Prefer"] = "resolution=merge-duplicates,return=minimal"
    response = client.post(url, headers=prefer, json=rows)
    response.raise_for_status()


def _upload_photo(client: httpx.Client, base_url: str, key: str, digest: str, data: bytes) -> None:
    response = client.post(
        f"{base_url}/storage/v1/object/{PHOTO_BUCKET}/{digest}.jpg",
        headers={
            **_cloud_headers(key, json_body=False),
            "Content-Type": "image/jpeg",
            "x-upsert": "true",
        },
        content=data,
    )
    response.raise_for_status()


def _ensure_photo_bucket(client: httpx.Client, base_url: str, key: str) -> None:
    global _bucket_ready
    if _bucket_ready:
        return
    response = client.post(
        f"{base_url}/storage/v1/bucket",
        headers=_cloud_headers(key),
        json={"id": PHOTO_BUCKET, "name": PHOTO_BUCKET, "public": False},
    )
    if response.status_code not in {200, 201, 400, 409}:
        response.raise_for_status()
    _bucket_ready = True


def _cloud_headers(key: str, *, json_body: bool = True) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _set_peer(label: str, css: str) -> None:
    with _status_lock:
        _labels["peer"] = label
        _labels["peer_class"] = css


def _set_cloud(label: str, css: str, alert: str = "") -> None:
    with _status_lock:
        _labels["cloud"] = label
        _labels["cloud_class"] = css
        _labels["cloud_alert"] = alert


def _remember_cloud_failure(exc: Exception, *, action: str) -> None:
    if isinstance(exc, httpx.TransportError):
        _set_cloud(
            "sem internet",
            "is-wait",
            (
                f"Sem conexão com a internet ao {action}. "
                "Os dados locais continuam neste PC e sobem assim que a conexão voltar."
            ),
        )
        return
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text
        detail = " ".join(body.split())[:160]
        hint = ""
        if "42501" in body or "permission denied" in body.lower():
            hint = (
                " Confira se SUPABASE_KEY no .env é a chave service_role "
                "(não a anon/public) e rode scripts/supabase_schema.sql no SQL Editor do Supabase."
            )
        _set_cloud(
            "falha na nuvem",
            "is-bad",
            (
                f"A nuvem recusou {action} (erro {exc.response.status_code}). "
                f"{detail}{hint} "
                "Os dados locais continuam neste PC e o envio será tentado de novo."
            ),
        )
        return
    _set_cloud(
        "falha na nuvem",
        "is-bad",
        (
            f"Falha ao {action}: {exc}. "
            "Os dados locais continuam neste PC e o envio será tentado de novo."
        ),
    )


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(timespec="seconds")


def _parse_iso(value: object) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "")
    if "+" in text[10:]:
        text = text.split("+", 1)[0]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "sim"}
    return bool(value)


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
