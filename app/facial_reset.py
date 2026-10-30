"""Refaz um facial do zero: apaga os usuários e cadastra de novo, um por um.

O facial da térmica ficou com a contagem de rostos bagunçada (FaceIDs até
374 com 16 pessoas e fotos soltas) e passou a dar a todo rosto novo o número
do último cadastrado. Limpar e recadastrar zera essa contagem.

Para cada facial, a normal primeiro (tem menos gente):
1. guarda uma cópia do export em data/backups, para recuperar se precisar;
2. monta a lista de quem volta:
   - sócio em dia daquela piscina, com a foto do sistema (sem foto no PC,
     usa a que está no facial; sem nenhuma, fica como está e não é apagado);
   - quem só existe no facial (funcionários), com a foto do próprio facial,
     menos os registros antigos de quem já é sócio no sistema;
3. apaga esses usuários e quem venceu ou não é desta piscina;
4. cadastra um por um. Se o facial repetir o número de rosto, refaz quem
   perdeu o rosto, para tudo e mostra quem ficou sem cadastro.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app import db, updater
from app.config import DATA_DIR, PHOTOS_DIR, ensure_directories, registration_pools, selected_pools
from app.intelbras_client import (
    DeviceError,
    ExportUser,
    FaceIDConflict,
    IntelbrasClient,
    face_photo,
)

logger = logging.getLogger(__name__)

# Usuários de teste dos botões da ADM: nunca voltam.
TEST_REGISTRATIONS = {"APFIDTESTE", "APVALTESTE"}

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False}


def status() -> dict[str, Any]:
    with _lock:
        return {**_state, "devices": [dict(item) for item in _state.get("devices", [])]}


def _plain(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split()).casefold()


def _in_pool(member: db.Member, pool_id: str) -> bool:
    chosen = selected_pools(member.pool) or registration_pools()
    return pool_id in {item["id"] for item in chosen}


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0].replace(":", "_")


def plan_device(pool: dict[str, str], users: list[ExportUser], members: list[db.Member]) -> dict[str, Any]:
    """Quem volta, quem só sai e quem não é mexido neste facial."""
    on_device = {u.user_id: u for u in users if u.user_id}
    by_registration = {m.registration: m for m in members}
    active = [
        m for m in members
        if _in_pool(m, pool["id"]) and not m.access_blocked and not m.is_expired
    ]
    active_regs = {m.registration for m in active}
    active_names = {_plain(m.name) for m in active}

    back: list[dict[str, Any]] = []
    untouched: list[dict[str, str]] = []
    for member in active:
        photo = Path(member.photo_path) if member.photo_path else None
        device_user = on_device.get(member.registration)
        if photo is not None and photo.is_file():
            back.append({"registration": member.registration, "name": member.name, "member_id": member.id, "path": photo})
        elif device_user is not None and device_user.photo:
            back.append({"registration": member.registration, "name": member.name, "member_id": member.id, "photo": device_user.photo})
        else:
            untouched.append({"name": member.name, "motivo": "sem foto no sistema nem no facial: use o botão Foto"})

    remove: list[dict[str, str]] = []
    for user in users:
        reg = user.user_id
        if not reg or reg in active_regs:
            continue
        if reg in TEST_REGISTRATIONS or reg in by_registration:
            # Teste, ou sócio vencido/de outra piscina: o sistema manda, sai do facial.
            remove.append({"registration": reg, "name": user.name})
        elif _plain(user.name) in active_names:
            # Registro antigo, feito direto no aparelho, de quem é sócio no sistema.
            remove.append({"registration": reg, "name": user.name})
        elif user.photo:
            back.append({"registration": reg, "name": user.name, "member_id": None, "photo": user.photo})
        else:
            untouched.append({"name": user.name, "motivo": "só existe no facial e não tem foto: não foi mexido"})
    return {"back": back, "remove": remove, "untouched": untouched}


def _set(device: dict[str, Any], **values: Any) -> None:
    with _lock:
        device.update(values)


def _register(client: IntelbrasClient, item: dict[str, Any]) -> None:
    """Cadastra um usuário pelo painel, com a foto do sistema ou a guardada no facial."""
    temp: Optional[Path] = None
    if "path" in item:
        photo = face_photo(item["path"])
    else:
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        temp = PHOTOS_DIR / f"refazer_{uuid.uuid4().hex[:8]}.jpg"
        temp.write_bytes(item["photo"])
        photo = face_photo(temp)
    try:
        client.create_user_with_face(item["name"], photo, registration=item["registration"])
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
            if photo != temp:
                photo.unlink(missing_ok=True)


def _reset_device(pool: dict[str, str], device: dict[str, Any], members: list[db.Member]) -> bool:
    """Refaz um facial. Falso se parou no meio (o próximo facial não começa)."""
    client = IntelbrasClient(base_url=pool["url"])
    _set(device, phase="lendo")
    client.ping()
    ensure_directories()
    backups = DATA_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"facial_{_host(pool['url'])}_{datetime.now():%Y%m%d_%H%M%S}.tgz"
    backup.write_bytes(client._export_archive())
    users = client.export_users(with_photos=True)
    if users is None:
        raise DeviceError("Não foi possível ler os usuários do facial. Nada foi apagado.")
    plan = plan_device(pool, users, members)
    _set(device, backup=str(backup), total=len(plan["back"]), untouched=plan["untouched"])

    _set(device, phase="apagando")
    on_device = {u.user_id for u in users}
    for item in plan["back"] + plan["remove"]:
        if item["registration"] in on_device:
            client.delete_user(registration=item["registration"])
    _set(device, removed=[item["name"] for item in plan["remove"]])

    _set(device, phase="cadastrando")
    done: list[dict[str, Any]] = []
    for index, item in enumerate(plan["back"]):
        _set(device, current=item["name"])
        try:
            _register(client, item)
        except FaceIDConflict as exc:
            # O facial repetiu o número do último rosto. Ao sair, o cadastro novo
            # levou o rosto do outro junto: refaz esse e para.
            repaired = []
            for other in [d for d in done if d["registration"] in exc.others]:
                try:
                    client.delete_user(registration=other["registration"])
                    _register(client, other)
                    repaired.append(other["name"])
                except DeviceError as again:
                    logger.error("Não deu para refazer %s: %s", other["registration"], again)
            missing = [item["name"]] + [rest["name"] for rest in plan["back"][index + 1:]]
            _set(
                device,
                error=str(exc),
                repaired=repaired,
                missing=missing,
                current=None,
            )
            return False
        except (DeviceError, OSError) as exc:
            logger.error("Falha ao recadastrar %s em %s: %s", item["registration"], pool["url"], exc)
            with _lock:
                device.setdefault("failed", []).append({"name": item["name"], "erro": str(exc)})
        else:
            done.append(item)
            if item.get("member_id"):
                db.log_access_event(
                    result="photo_updated",
                    member_name=item["name"],
                    registration=item["registration"],
                    detail=f"Recadastrado do zero no facial ({pool['label']})",
                )
        _set(device, done=index + 1)
    _set(device, current=None, phase="pronto")
    return True


def _run() -> None:
    try:
        with updater.busy_operation():
            members = db.list_members()
            for pool, device in zip(registration_pools(), status_devices()):
                try:
                    finished = _reset_device(pool, device, members)
                except DeviceError as exc:
                    _set(device, error=str(exc), phase="erro")
                    finished = False
                if not finished:
                    # Não mexe no próximo facial depois de um problema neste.
                    break
    except updater.Busy as exc:
        with _lock:
            _state["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refazer faciais falhou")
        with _lock:
            _state["error"] = f"Parou: {exc}"
    finally:
        with _lock:
            _state["running"] = False
            _state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        from app.sync import request_sync

        request_sync()


def status_devices() -> list[dict[str, Any]]:
    with _lock:
        return _state["devices"]


def start() -> tuple[bool, str]:
    from app import face_fix

    if face_fix.status().get("running"):
        return False, "Espere o \"Corrigir tudo\" terminar."
    with _lock:
        if _state.get("running"):
            return False, "Já está refazendo os faciais."
        _state.clear()
        _state.update(
            {
                "running": True,
                "error": None,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "devices": [
                    {"label": pool["label"], "url": pool["url"], "phase": "esperando", "total": 0, "done": 0}
                    for pool in registration_pools()
                ],
            }
        )
    threading.Thread(target=_run, name="facial-reset", daemon=True).start()
    return True, "Refazendo os faciais."
