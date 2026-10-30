from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from app import db, exam_test
from app import settings as app_settings
from app.config import (
    PHOTOS_DIR,
    EXPIRY_HOUR,
    PORT,
    VALIDITY_DAYS,
    configured_device_urls,
    ensure_directories,
    exam_expires_from,
    normalize_pool_ids,
    pool_label,
    registration_pools,
    selected_pools,
    sync_settings,
)
from app.intelbras_client import (
    DeviceError,
    DeviceFleet,
    IntelbrasClient,
    face_photo,
    fleet_for_pool,
    prepare_face_photo,
    public_media_url,
)
from app.netcheck import lan_ip_problem, local_ipv4_addresses, suggested_ip
from app import updater
from app.sync import build_state, request_sync, run_sync_cycle, status_view
from app.tray import schedule_close_prompt
from app.update_core import UpdateError
from app.version import VERSION

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
APP_TITLE = "Acesso Piscinas Clube Aliança"
_ADM_TABS = {"socios", "vencimentos", "acessos", "teste", "config"}
_WEEKDAYS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


@bp.app_template_filter("data_hora")
def data_hora(value: Optional[datetime]) -> str:
    """Data exata com o dia da semana: sex, 27/11/2026 22:00."""
    if value is None:
        return "—"
    return f"{_WEEKDAYS[value.weekday()]}, {value.strftime('%d/%m/%Y %H:%M')}"


def _blocks_update(view):
    """Cadastro, renovação, foto, bloqueio e exclusão seguram a instalação."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.method == "GET":
            return view(*args, **kwargs)
        try:
            with updater.busy_operation():
                return view(*args, **kwargs)
        except updater.Busy as exc:
            if _wants_json():
                return jsonify({"ok": False, "error": str(exc)}), 503
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("main.index"))

    return wrapper


def _local_admin_request() -> bool:
    """Só o navegador deste PC instala ou procura atualização.

    Confere o IP, o Host (contra DNS rebinding), a Origin e um header que
    um formulário de outro site não consegue mandar sem preflight.
    """
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return False
    from app.config import PORT

    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if request.host not in allowed_hosts:
        return False
    origin = request.headers.get("Origin")
    if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
        return False
    return request.headers.get("X-Acesso-Local") == "1"


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _redirect_adm(tab: str = "socios"):
    if tab not in _ADM_TABS:
        tab = "socios"
    return redirect(url_for("main.adm") + f"#{tab}")


def _redirect_after_member_action(default):
    tab = (request.form.get("return_tab") or "").strip()
    if tab in _ADM_TABS:
        return _redirect_adm(tab)
    return redirect(request.referrer or default)


def _remove_member_photo(photo_path: Optional[str]) -> None:
    if not photo_path:
        return
    path = Path(photo_path)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.warning("Não foi possível apagar a foto %s", path)


def _wants_json() -> bool:
    return (
        request.args.get("format") == "json"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


def _save_upload(photo, name_hint: str) -> Path:
    ensure_directories()
    ext = Path(photo.filename).suffix.lower() or ".jpg"
    safe_stem = secure_filename(name_hint)[:40] or "membro"
    filename = f"{safe_stem}_{uuid.uuid4().hex[:8]}{ext}"
    photo_path = PHOTOS_DIR / filename
    photo.save(photo_path)
    # Este arquivo vai para o facial: ele já precisa ser o JPEG final, não o PNG enviado.
    try:
        return prepare_face_photo(photo_path)
    except DeviceError:
        photo_path.unlink(missing_ok=True)
        raise


def _parse_device_log_time(value: object) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _normalize_door_result(item: dict) -> str:
    raw = str(
        item.get("Status")
        or item.get("Result")
        or item.get("OpenDoor")
        or item.get("CaptureType")
        or ""
    ).lower()
    if any(token in raw for token in ("fail", "deny", "denied", "0", "reject", "invalid")):
        return "denied"
    if any(token in raw for token in ("success", "ok", "1", "open", "pass", "allow")):
        return "approved"
    # Campos numéricos comuns no XPE
    for key in ("Status", "Result", "OpenDoor"):
        val = item.get(key)
        if val in (1, "1", True):
            return "approved"
        if val in (0, "0", False):
            return "denied"
    return "approved"


def _sync_door_logs() -> int:
    """Importa logs de todos os faciais para o banco local. Retorna quantos novos."""
    try:
        items = DeviceFleet().list_door_logs(max_items=300)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sync de logs do XPE falhou: %s", exc)
        return 0

    added = 0
    for prefix, item in items:
        if not isinstance(item, dict):
            continue
        name = (
            item.get("Name")
            or item.get("UserName")
            or item.get("EmployeeName")
            or None
        )
        registration = (
            item.get("UserID")
            or item.get("EmployeeID")
            or item.get("Card")
            or None
        )
        event_at = (
            _parse_device_log_time(item.get("Time"))
            or _parse_device_log_time(item.get("LogTime"))
            or _parse_device_log_time(item.get("DateTime"))
            or datetime.now()
        )
        raw_external_id = str(
            item.get("ID")
            or item.get("LogID")
            or f"{registration}-{event_at.isoformat()}-{item.get('Status', '')}"
        )
        external_id = f"{prefix}{raw_external_id}"
        result = _normalize_door_result(item)
        detail = "Log do equipamento facial"
        if prefix:
            detail = f"Log do equipamento {prefix.rstrip(':')}"
        inserted = db.log_access_event(
            result=result,
            member_name=str(name) if name else None,
            registration=str(registration) if registration else None,
            detail=detail,
            source="device",
            event_at=event_at,
            external_id=external_id,
        )
        if inserted:
            added += 1
    if added:
        request_sync()
    return added


@bp.get("/")
def index():
    return redirect(url_for("main.cadastrar"))


@bp.get("/media/<path:filename>")
def media(filename: str):
    """Fotos dos sócios, para as telas do sistema."""
    ensure_directories()
    safe = Path(filename).name
    return send_from_directory(PHOTOS_DIR, safe)


def _sync_authorized() -> bool:
    token = sync_settings().token
    if not token:
        return False
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = (request.headers.get("X-Sync-Token") or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, token)


@bp.get("/sync/estado")
def sync_estado():
    if not _sync_authorized():
        return jsonify({"ok": False, "error": "não autorizado"}), 401
    return jsonify(build_state())


@bp.get("/sync/foto/<path:filename>")
def sync_foto(filename: str):
    if not _sync_authorized():
        return jsonify({"ok": False, "error": "não autorizado"}), 401
    safe = Path(filename).name
    if not safe or safe != filename or safe in {".", ".."}:
        return jsonify({"ok": False, "error": "arquivo inválido"}), 400
    ensure_directories()
    if not (PHOTOS_DIR / safe).is_file():
        return jsonify({"ok": False, "error": "foto não encontrada"}), 404
    return send_from_directory(PHOTOS_DIR, safe)


@bp.route("/cadastrar", methods=["GET", "POST"])
@_blocks_update
def cadastrar():
    if request.method == "GET":
        return render_template(
            "cadastrar.html",
            validity_days=VALIDITY_DAYS,
        expiry_hour=EXPIRY_HOUR,
            app_title=APP_TITLE,
            pools=registration_pools(),
        )

    name = (request.form.get("name") or "").strip()
    pool = normalize_pool_ids(request.form.getlist("pool"))
    photo = request.files.get("photo")

    def fail(message: str, code: int = 400):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), code
        flash(message, "error")
        return redirect(url_for("main.cadastrar"))

    if not name:
        return fail("Informe o nome da pessoa.")

    if pool is None:
        return fail("Selecione ao menos uma piscina: normal, térmica ou as duas.")

    if not photo or not photo.filename:
        return fail("Selecione uma foto.")

    if not _allowed_file(photo.filename):
        return fail("Formato de foto inválido. Use JPG ou PNG.")

    try:
        photo_path = _save_upload(photo, name)
    except Exception as exc:
        logger.exception("Falha ao salvar foto")
        return fail(f"Não foi possível salvar a foto: {exc}", 500)

    client = fleet_for_pool(pool)
    try:
        created = client.register_member(name, photo_path)
        expires_at = exam_expires_from(datetime.now())
        member = db.create_member(
            name=name,
            registration=created.registration,
            device_user_id=created.device_user_id,
            photo_path=str(photo_path),
            expires_at=expires_at,
            pool=pool,
        )
    except DeviceError as exc:
        logger.exception("Falha no cadastro facial")
        if photo_path.exists():
            photo_path.unlink(missing_ok=True)
        return fail(f"Não foi possível cadastrar no equipamento: {exc}", 502)
    except Exception as exc:
        logger.exception("Erro inesperado no cadastro")
        if photo_path.exists():
            photo_path.unlink(missing_ok=True)
        return fail(f"Erro inesperado: {exc}", 500)

    msg = (
        f"{member.name} cadastrado(a) (matrícula {member.registration}). "
        f"Exame médico válido até {data_hora(member.medical_exam_expires_at)}; "
        "nesse horário a face é apagada do Intelbras. "
        f"Foto confirmada no Intelbras ({pool_label(pool)})."
    )

    request_sync()

    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "message": msg,
                "face_ok": True,
                "redirect": url_for("main.membros"),
            }
        )

    flash(msg, "success")
    return redirect(url_for("main.membros"))


@bp.get("/membros")
def membros():
    members = db.list_members()
    return render_template(
        "membros.html",
        members=members,
        removed_at=db.last_removal_times(),
        validity_days=VALIDITY_DAYS,
        expiry_hour=EXPIRY_HOUR,
        app_title=APP_TITLE,
    )


@bp.post("/membros/<int:member_id>/renovar")
@_blocks_update
def renovar(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return redirect(url_for("main.membros"))

    # Bloqueado já saiu do facial: o OK exame recria o usuário com a foto salva neste PC.
    photo_path = Path(member.photo_path) if member.photo_path else None
    if photo_path is not None and not photo_path.is_file():
        photo_path = None
    if photo_path is None and member.access_blocked:
        flash(
            f"A foto de {member.name} não está neste PC, então o facial não tem como receber o rosto. "
            "Use o botão Foto para enviar uma nova. O exame não foi renovado.",
            "error",
        )
        return _redirect_after_member_action(url_for("main.membros"))
    if photo_path is not None:
        try:
            # Foto antiga em PNG: o facial recebe o JPEG convertido, não o arquivo original.
            photo_path = face_photo(photo_path)
        except DeviceError as exc:
            flash(f"A foto salva de {member.name} não abriu: {exc} Use o botão Foto.", "error")
            return _redirect_after_member_action(url_for("main.membros"))

    client = fleet_for_pool(member.pool)
    try:
        created = client.renew_member(
            name=member.name,
            registration=member.registration,
            image_path=photo_path,
            previous_device_user_id=member.device_user_id,
        )
        new_expires = exam_expires_from(datetime.now())
        updated = db.renew_member(member_id, expires_at=new_expires)
        if updated and created.device_user_id != member.device_user_id:
            db.update_device_user_id(member_id, created.device_user_id)
            updated = db.get_member(member_id)
    except DeviceError as exc:
        logger.exception("Falha ao renovar no equipamento")
        flash(f"Não foi possível renovar no equipamento: {exc}", "error")
        return _redirect_after_member_action(url_for("main.membros"))

    request_sync()
    from app.expiry import reschedule

    reschedule()
    photo_note = (
        "Foto salva reenviada e confirmada no Intelbras."
        if member.access_blocked
        else "Foto confirmada no Intelbras."
    )
    flash(
        f"Exame médico de {updated.name} renovado até {data_hora(updated.medical_exam_expires_at)}; "
        f"nesse horário a face é apagada do Intelbras. {photo_note}",
        "success",
    )
    return _redirect_after_member_action(url_for("main.membros"))


@bp.get("/sync/painel")
def sync_painel():
    """Estado da nuvem para o aviso da aba ADM. Não inclui chaves nem dados de sócios."""
    return jsonify(status_view())


@bp.post("/adm/testar-sync")
def adm_testar_sync():
    """Roda a troca agora e devolve o resultado do outro PC e da nuvem."""
    try:
        report = run_sync_cycle()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Teste de sincronização falhou")
        return jsonify({"ok": False, "error": f"Não foi possível testar: {exc}"}), 500
    return jsonify({"ok": True, **report})


@bp.get("/adm")
def adm():
    # Sync sob demanda via botão — evita travar a página se o XPE estiver offline
    sync_note = request.args.get("sync_note")
    is_local = request.remote_addr in {"127.0.0.1", "::1"}

    now = datetime.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    total_members = db.count_members()
    accesses_week = db.count_access_events_since(week_ago, result="approved")
    accesses_month = db.count_access_events_since(month_ago, result="approved")
    denied_week = db.count_access_events_since(week_ago, result="denied")
    blocked_count = len(db.list_blocked_members())

    return render_template(
        "adm.html",
        app_title=APP_TITLE,
        validity_days=VALIDITY_DAYS,
        expiry_hour=EXPIRY_HOUR,
        sync_note=sync_note,
        stats={
            "total_members": total_members,
            "accesses_week": accesses_week,
            "accesses_month": accesses_month,
            "denied_week": denied_week,
            "blocked_count": blocked_count,
            "expired_count": len(db.list_expired_members()),
            "expiring_count": len(db.list_expiring_soon(7)),
        },
        expired_members=db.list_expired_members(),
        expiring_members=db.list_expiring_soon(7),
        blocked_members=db.list_blocked_members(),
        all_members=db.list_members(),
        pools=registration_pools(),
        approved_events=db.list_access_events(limit=40, result="approved"),
        denied_events=db.list_access_events(limit=40, result="denied"),
        blocked_events=db.list_access_events(limit=40, result="blocked"),
        change_events=db.list_access_events(
            limit=40,
            results=["registered", "renewed", "photo_updated", "pool_updated", "expiry_changed", "deleted"],
        ),
        test_members=_test_members(),
        now_input=now.strftime("%Y-%m-%dT%H:%M"),
        removed_at=db.last_removal_times(),
        settings=app_settings.current() if is_local else None,
        net=_network_info() if is_local else None,
    )


def _network_info() -> dict:
    """IP deste PC e endereço para colocar no outro PC, para a aba Configurações."""
    import socket

    ips = sorted(local_ipv4_addresses())
    configured = (app_settings.current().get("SERVER_LAN_IP") or "").strip()
    return {
        "hostname": socket.gethostname(),
        "ips": ips,
        "suggested": suggested_ip(frozenset(ips)) if ips else "",
        "problem": lan_ip_problem(),
        "self_url": f"http://{configured}:{PORT}" if configured else "",
        "port": PORT,
    }


def _test_members() -> list[dict]:
    rows = []
    for registration, original in exam_test.held().items():
        member = db.get_member_by_registration(registration)
        if member is None:
            exam_test.release(registration)
            continue
        rows.append({"member": member, "original": original})
    rows.sort(key=lambda row: row["member"].name.lower())
    return rows


@bp.post("/adm/membros/<int:member_id>/bloquear")
@_blocks_update
def adm_bloquear(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return _redirect_adm("socios")

    client = fleet_for_pool(member.pool)
    try:
        if member.device_user_id is not None or member.registration:
            client.block_user(
                device_user_id=member.device_user_id or 0,
                registration=member.registration,
            )
        db.mark_blocked(member_id)
    except DeviceError as exc:
        logger.exception("Falha ao bloquear no equipamento")
        flash(f"Não foi possível bloquear no equipamento: {exc}", "error")
        return _redirect_adm("socios")

    request_sync()
    flash(f"Acesso de {member.name} bloqueado.", "success")
    return _redirect_adm("socios")


@bp.post("/adm/membros/<int:member_id>/foto")
@_blocks_update
def adm_atualizar_foto(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return _redirect_adm("socios")

    photo = request.files.get("photo")
    if not photo or not photo.filename:
        flash("Selecione uma nova foto.", "error")
        return _redirect_adm("socios")
    if not _allowed_file(photo.filename):
        flash("Formato de foto inválido. Use JPG ou PNG.", "error")
        return _redirect_adm("socios")

    try:
        photo_path = _save_upload(photo, member.name)
    except Exception as exc:
        logger.exception("Falha ao salvar nova foto")
        flash(f"Não foi possível salvar a foto: {exc}", "error")
        return _redirect_adm("socios")

    client = fleet_for_pool(member.pool)
    try:
        if member.access_blocked or member.is_expired:
            db.update_member_photo(member_id, str(photo_path))
        else:
            created = client.update_member_face(
                name=member.name,
                registration=member.registration,
                image_path=photo_path,
            )
            db.update_member_photo(member_id, str(photo_path))
            db.update_device_user_id(member_id, created.device_user_id)
        db.log_access_event(
            result="photo_updated",
            member_name=member.name,
            registration=member.registration,
            detail="Foto atualizada pelo ADM",
        )
    except DeviceError as exc:
        logger.exception("Falha ao atualizar foto no equipamento")
        if photo_path.exists():
            photo_path.unlink(missing_ok=True)
        flash(str(exc), "error")
        return _redirect_adm("socios")

    request_sync()
    flash(f"Foto de {member.name} confirmada no Intelbras.", "success")
    return _redirect_adm("socios")


def _pool_urls(pool: Optional[str]) -> dict[str, str]:
    """URL do facial -> nome da piscina. Sem piscina gravada vale as duas."""
    chosen = selected_pools(pool) or registration_pools()
    return {item["url"]: item["label"] for item in chosen}


@bp.post("/adm/conferir-fotos")
def adm_conferir_fotos():
    """Passa sócio a sócio comparando o banco deste PC com o que cada facial guardou."""
    from app.face_audit import run_audit

    try:
        return jsonify(run_audit())
    except Exception as exc:  # noqa: BLE001 — a tela mostra o erro em vez de quebrar
        logger.exception("Conferência das fotos falhou")
        return jsonify({"ok": False, "error": f"Falha na conferência: {exc}"}), 500


@bp.post("/adm/corrigir-fotos")
def adm_corrigir_fotos():
    """Corrige em segundo plano tudo que a conferência acha e tem uma resposta certa."""
    from app import face_fix

    ok, message = face_fix.start()
    return jsonify({"ok": ok, "message": message, "error": None if ok else message}), (202 if ok else 409)


@bp.get("/adm/corrigir-fotos")
def adm_corrigir_fotos_status():
    from app import face_fix

    return jsonify({"ok": True, **face_fix.status()})


def _member_ready_for_facial(member: Optional[db.Member]):
    """Resposta de erro se o sócio não pode receber o rosto agora; None se pode."""
    if member is None:
        return jsonify({"ok": False, "error": "Membro não encontrado."}), 404
    if member.access_blocked or member.is_expired:
        return jsonify({"ok": False, "error": f"O exame de {member.name} está vencido: renove antes."}), 400
    return None


def _recreate(member: db.Member, photo: Path, detail: str) -> tuple[list[str], Optional[str]]:
    """Recria o sócio nos faciais da piscina dele e registra o evento se algum deu certo."""
    from app.face_fix import recreate_member

    done, error = recreate_member(member, photo)
    if done:
        db.log_access_event(
            result="photo_updated",
            member_name=member.name,
            registration=member.registration,
            detail=detail,
        )
    return done, error


def _recreate_json(member: db.Member, done: list[str], error: Optional[str]):
    if done:
        request_sync()
    if error:
        feito = f" Já refeito em: {', '.join(done)}." if done else ""
        return jsonify({"ok": False, "error": f"{error}{feito}"}), 502
    return jsonify({"ok": True, "message": f"{member.name} refeito em: {', '.join(done)}."})


@bp.post("/adm/membros/<int:member_id>/refazer-facial")
@_blocks_update
def adm_refazer_facial(member_id: int):
    """Apaga o sócio dos faciais da piscina dele e cadastra de novo com a foto salva.

    Recriado, o usuário ganha um FaceID novo: desfaz o rosto preso no FaceID de outro.
    """
    member = db.get_member(member_id)
    refused = _member_ready_for_facial(member)
    if refused:
        return refused
    photo = Path(member.photo_path) if member.photo_path else None
    if photo is None or not photo.is_file():
        return jsonify({
            "ok": False,
            "error": f"A foto de {member.name} não está neste PC. Use o botão Foto na aba Sócios.",
        }), 400
    try:
        photo = face_photo(photo)
    except DeviceError as exc:
        return jsonify({"ok": False, "error": f"A foto salva não abriu: {exc}"}), 400
    done, error = _recreate(member, photo, "Refeito no facial com a foto do sistema")
    return _recreate_json(member, done, error)


@bp.post("/adm/membros/<int:member_id>/usar-foto-intelbras")
@_blocks_update
def adm_usar_foto_intelbras(member_id: int):
    """Usa a foto que um facial guardou como foto do sócio e recria ele com ela.

    A foto é lida antes de apagar qualquer coisa. Ela passa a ser a foto do
    sistema também, para os dois PCs e a nuvem.
    """
    member = db.get_member(member_id)
    refused = _member_ready_for_facial(member)
    if refused:
        return refused
    url = str((request.get_json(silent=True) or {}).get("url") or "")
    if url not in {pool["url"] for pool in registration_pools()}:
        return jsonify({"ok": False, "error": "Facial desconhecido."}), 400
    users = IntelbrasClient(base_url=url).export_users(with_photos=True)
    if users is None:
        return jsonify({"ok": False, "error": "Não foi possível ler a foto do facial. Confira se ele está ligado."}), 502
    user = next((u for u in users if u.user_id == member.registration), None)
    if user is None or not user.photo:
        return jsonify({"ok": False, "error": f"O facial não tem foto guardada para {member.name}."}), 400

    ensure_directories()
    stem = secure_filename(member.name)[:40] or "membro"
    raw_path = PHOTOS_DIR / f"{stem}_{uuid.uuid4().hex[:8]}.jpg"
    raw_path.write_bytes(user.photo)
    try:
        photo = prepare_face_photo(raw_path)
    except DeviceError as exc:
        raw_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": f"A foto do facial não abriu: {exc}"}), 400

    done, error = _recreate(member, photo, "Foto do Intelbras passou a ser a foto do sistema")
    if done:
        # A foto antiga não é apagada do PC.
        db.update_member_photo(member_id, str(photo))
    else:
        photo.unlink(missing_ok=True)
    return _recreate_json(member, done, error)


@bp.post("/adm/membros/<int:member_id>/piscinas")
@_blocks_update
def adm_piscinas(member_id: int):
    """Troca as piscinas do sócio sem mudar matrícula, foto nem exame."""
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return _redirect_adm("socios")

    pool = normalize_pool_ids(request.form.getlist("pool"))
    if pool is None:
        flash("Selecione ao menos uma piscina: normal, térmica ou as duas.", "error")
        return _redirect_adm("socios")

    old_urls = _pool_urls(member.pool)
    new_urls = _pool_urls(pool)
    added = [url for url in new_urls if url not in old_urls]
    removed = [url for url in old_urls if url not in new_urls]
    if not added and not removed:
        flash(f"{member.name} já está em: {pool_label(pool)}.", "success")
        return _redirect_adm("socios")

    # Bloqueado ou vencido não tem face nos faciais: só grava a escolha.
    active = not member.access_blocked and not member.is_expired
    if active and added:
        photo = Path(member.photo_path) if member.photo_path else None
        if photo is None or not photo.is_file():
            flash(
                f"A foto de {member.name} não está neste PC. Troque a foto antes de incluir outra piscina.",
                "error",
            )
            return _redirect_adm("socios")
        try:
            for url in added:
                IntelbrasClient(base_url=url).renew_member(
                    name=member.name,
                    registration=member.registration,
                    image_path=photo,
                )
        except DeviceError as exc:
            logger.exception("Falha ao incluir sócio em outra piscina")
            flash(f"Não foi possível incluir em {new_urls[url]}: {exc}", "error")
            return _redirect_adm("socios")

    failed: list[str] = []
    for url in removed:
        try:
            IntelbrasClient(base_url=url).delete_user(registration=member.registration)
        except DeviceError as exc:
            logger.error("Falha ao tirar %s de %s: %s", member.registration, url, exc)
            failed.append(old_urls[url])
    if failed:
        # Quem não saiu do facial continua com a piscina dele no cadastro.
        kept = [item["id"] for item in registration_pools() if item["label"] in failed]
        pool = normalize_pool_ids(pool.split(",") + kept) or pool

    db.set_member_pool(member_id, pool)
    db.log_access_event(
        result="pool_updated",
        member_name=member.name,
        registration=member.registration,
        detail=f"{pool_label(member.pool)} → {pool_label(pool)}",
    )
    request_sync()
    if failed:
        flash(
            f"Não foi possível tirar {member.name} de {', '.join(failed)}. "
            f"Cadastro ficou em: {pool_label(pool)}. Tente de novo com o facial ligado.",
            "error",
        )
    else:
        flash(f"Piscinas de {member.name} alteradas para: {pool_label(pool)}.", "success")
    return _redirect_adm("socios")


@bp.post("/adm/membros/<int:member_id>/validade")
@_blocks_update
def adm_validade(member_id: int):
    """ADM muda data e hora do vencimento. Vale nos dois PCs, mesmo mais curta."""
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return _redirect_adm("socios")
    tab = (request.form.get("return_tab") or "socios").strip()
    if exam_test.is_held(member.registration):
        flash(f"{member.name} está na aba Teste. Encerre o teste antes de mudar a validade.", "error")
        return _redirect_adm(tab)
    try:
        expires_at = datetime.strptime(request.form.get("expires_at") or "", "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Informe a data e a hora do vencimento.", "error")
        return _redirect_adm(tab)
    old = member.medical_exam_expires_at
    if expires_at == old:
        flash(f"A validade de {member.name} já é {data_hora(old)}.", "success")
        return _redirect_adm(tab)

    reopen = member.access_blocked and expires_at > datetime.now()
    if reopen:
        try:
            _reopen_on_device(member)
        except DeviceError as exc:
            logger.exception("Falha ao devolver a face ao mudar a validade")
            flash(
                f"Validade não alterada: não foi possível devolver {member.name} ao facial ({exc}).",
                "error",
            )
            return _redirect_adm(tab)
    updated = db.set_exam_expiry(member.id, expires_at, unblock=reopen, manual=True) or member
    db.log_access_event(
        result="expiry_changed",
        member_name=member.name,
        registration=member.registration,
        detail=f"{old.strftime('%d/%m/%Y %H:%M')} → {expires_at.strftime('%d/%m/%Y %H:%M')} (ADM)",
    )
    when = data_hora(expires_at)
    category = "success"
    if updated.is_expired and not updated.access_blocked:
        from app.expiry import block_member_on_devices

        if block_member_on_devices(updated, fleet_for_pool(updated.pool)):
            message = f"Validade de {member.name} alterada para {when}. Já venceu: a face foi apagada do Intelbras."
        else:
            category = "error"
            message = (
                f"Validade de {member.name} alterada para {when}, mas o facial não apagou o sócio. "
                "A checagem tenta de novo a cada 15 minutos."
            )
    elif updated.access_blocked:
        message = f"Validade de {member.name} alterada para {when}. O sócio continua bloqueado."
    elif reopen:
        message = (
            f"Validade de {member.name} alterada para {when}. A face voltou para o Intelbras "
            "e será apagada nesse horário."
        )
    else:
        message = f"Validade de {member.name} alterada para {when}. A face será apagada do Intelbras nesse horário."
    from app.expiry import reschedule

    reschedule()
    request_sync()
    flash(message, category)
    return _redirect_adm(tab)


def _local_only():
    if _local_admin_request():
        return None
    return jsonify({"ok": False, "error": "Abra as Configurações no PC onde o programa está aberto."}), 403


@bp.post("/adm/config")
def adm_config_salvar():
    """Grava as configurações da aba Configurações no env.txt/.env deste PC."""
    forbidden = _local_only()
    if forbidden:
        return forbidden
    data = request.get_json(silent=True) or {}
    form = {key: str(data.get(key) or "") for key in app_settings.FIELDS}
    try:
        changed = app_settings.save(form)
    except app_settings.SettingsError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        logger.exception("Falha ao gravar as configurações")
        return jsonify({"ok": False, "error": f"Não foi possível gravar o arquivo: {exc}"}), 500
    if changed:
        logger.info("Configurações alteradas: %s", ", ".join(changed))
        request_sync()
        flash("Configurações salvas: " + ", ".join(changed) + ".", "success")
    else:
        flash("Nada mudou nas configurações.", "success")
    return jsonify({"ok": True, "changed": changed})


@bp.post("/adm/config/testar-faciais")
def adm_config_testar_faciais():
    """Testa os faciais com o que está na tela (senha em branco usa a salva)."""
    forbidden = _local_only()
    if forbidden:
        return forbidden
    data = request.get_json(silent=True) or {}
    login = str(data.get("login") or "").strip() or None
    password = str(data.get("password") or "") or None
    urls = [str(item) for item in (data.get("urls") or []) if str(item).strip()]
    rows = []
    for raw in (urls or configured_device_urls())[:4]:
        try:
            url = app_settings._http_url(raw, "Facial")
        except app_settings.SettingsError as exc:
            rows.append({"url": raw, "ok": False, "error": str(exc)})
            continue
        row = DeviceFleet([IntelbrasClient(base_url=url, login=login, password=password, timeout=8)]).status()[0]
        if not row.get("ok") and "401" in str(row.get("error")):
            row["error"] = "Usuário ou senha dos faciais errados."
        rows.append(row)
    return jsonify({"ok": True, "devices": rows})


@bp.post("/adm/config/testar-ip")
def adm_config_testar_ip():
    """Confere se o programa responde no IP que o facial usa para baixar as fotos."""
    forbidden = _local_only()
    if forbidden:
        return forbidden
    import httpx

    problem = lan_ip_problem()
    url = public_media_url("x").rsplit("/media/", 1)[0]
    if not url:
        return jsonify({"ok": False, "error": problem or "IP deste PC não configurado."})
    try:
        response = httpx.get(f"{url}/saude", params={"rapido": "1"}, timeout=5)
        reachable = response.status_code == 200
    except httpx.HTTPError as exc:
        reachable = False
        problem = problem or f"O programa não respondeu em {url}: {exc}"
    return jsonify({"ok": reachable and not problem, "url": url, "error": problem or None})


@bp.post("/adm/sincronizar-logs")
def adm_sincronizar_logs():
    try:
        added = _sync_door_logs()
        flash(
            f"Sincronização concluída. {added} novo(s) registro(s).",
            "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Falha ao sincronizar logs: {exc}", "error")
    return _redirect_adm("acessos")


@bp.post("/adm/membros/<int:member_id>/excluir")
@_blocks_update
def adm_excluir(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return _redirect_adm("socios")

    client = fleet_for_pool(member.pool)
    try:
        client.delete_user(
            registration=member.registration,
            device_user_id=member.device_user_id,
        )
    except DeviceError as exc:
        logger.exception("Falha ao excluir no equipamento")
        flash(
            f"Não foi possível excluir no equipamento: {exc}. "
            "O cadastro foi mantido.",
            "error",
        )
        return _redirect_adm("socios")

    db.log_access_event(
        result="deleted",
        member_name=member.name,
        registration=member.registration,
        detail="Cadastro excluído pelo ADM",
    )
    photo_path = member.photo_path
    if not db.delete_member(member_id):
        flash("Não foi possível apagar o cadastro local.", "error")
        return _redirect_adm("socios")

    _remove_member_photo(photo_path)
    request_sync()
    flash(f"Cadastro de {member.name} excluído.", "success")
    return _redirect_adm("socios")


@bp.post("/fechar-aba")
def fechar_aba():
    """A aba do navegador fechou. A confirmação aparece se nada reabrir."""
    schedule_close_prompt()
    return ("", 204)


@bp.post("/adm/testar-validade")
def adm_testar_validade():
    """Teste do formato de data de validade nos faciais. Só pelo próprio PC."""
    if not _local_admin_request():
        return jsonify({"ok": False, "error": "Rode o teste no PC onde o programa está aberto."}), 403
    from app.validity_probe import run_probe

    reports = [run_probe(client) for client in DeviceFleet().clients]
    return jsonify({"ok": True, "version": VERSION, "relatorios": reports})


@bp.post("/adm/refazer-faciais")
def adm_refazer_faciais():
    """Apaga e recadastra os faciais do zero, em segundo plano. Só pelo próprio PC."""
    if not _local_admin_request():
        return jsonify({"ok": False, "error": "Rode no PC onde o programa está aberto."}), 403
    from app import facial_reset

    ok, message = facial_reset.start()
    return jsonify({"ok": ok, "message": message, "error": None if ok else message}), (202 if ok else 409)


@bp.get("/adm/refazer-faciais")
def adm_refazer_faciais_status():
    from app import facial_reset

    return jsonify({"ok": True, **facial_reset.status()})


@bp.post("/adm/testar-faceid")
@_blocks_update
def adm_testar_faceid():
    """Teste de como cada facial escolhe o FaceID. Só pelo próprio PC."""
    if not _local_admin_request():
        return jsonify({"ok": False, "error": "Rode o teste no PC onde o programa está aberto."}), 403
    from concurrent.futures import ThreadPoolExecutor

    from app import faceid_probe

    photo = None
    upload = request.files.get("photo")
    if upload and upload.filename:
        try:
            photo = faceid_probe.prepared_photo(upload.read())
        except DeviceError as exc:
            return jsonify({"ok": False, "error": f"A foto de teste não abriu: {exc}"}), 400
    clients = DeviceFleet().clients
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as executor:
        reports = list(executor.map(lambda client: faceid_probe.run_probe(client, photo), clients))
    return jsonify({"ok": True, "version": VERSION, "relatorios": reports})


def _test_forbidden():
    if _local_admin_request():
        return None
    return jsonify({"ok": False, "error": "Use a aba Teste no PC onde o programa está aberto."}), 403


def _reopen_on_device(member: db.Member) -> None:
    """Devolve a face ao facial de quem foi bloqueado (teste ou validade nova no ADM)."""
    photo = Path(member.photo_path) if member.photo_path else None
    if photo is not None and not photo.is_file():
        photo = None
    if photo is not None:
        photo = face_photo(photo)
    created = fleet_for_pool(member.pool).renew_member(
        name=member.name,
        registration=member.registration,
        image_path=photo,
        previous_device_user_id=member.device_user_id,
    )
    if created.device_user_id and created.device_user_id != member.device_user_id:
        db.update_device_user_id(member.id, created.device_user_id)


def _set_test_expiry(member: db.Member, expires_at: datetime) -> db.Member:
    """Grava a data. Se o sócio estava bloqueado e a data nova ainda vale, libera no facial."""
    reopen = member.access_blocked and expires_at > datetime.now()
    if reopen:
        _reopen_on_device(member)
    updated = db.set_exam_expiry(member.id, expires_at, unblock=reopen)
    return updated or member


@bp.post("/adm/teste/data")
@_blocks_update
def adm_teste_data():
    """Muda a data do exame de um sócio para testar o vencimento."""
    forbidden = _test_forbidden()
    if forbidden:
        return forbidden
    data = request.get_json(silent=True) or {}
    try:
        member_id = int(data.get("member_id"))
        expires_at = datetime.strptime(str(data.get("expires_at") or ""), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Escolha o sócio e a data do exame."}), 400
    member = db.get_member(member_id)
    if member is None:
        return jsonify({"ok": False, "error": "Sócio não encontrado."}), 404

    exam_test.hold(member.registration, member.medical_exam_expires_at)
    try:
        updated = _set_test_expiry(member, expires_at)
    except DeviceError as exc:
        logger.exception("Falha ao liberar no facial durante o teste")
        return jsonify({"ok": False, "error": f"Não foi possível liberar no facial: {exc}"}), 502
    logger.info(
        "Teste de vencimento: %s com exame até %s",
        member.registration,
        expires_at.isoformat(timespec="minutes"),
    )
    from app.expiry import reschedule

    reschedule()
    when = updated.medical_exam_expires_at.strftime("%d/%m/%Y %H:%M")
    if updated.is_expired and not updated.access_blocked:
        # Data vencida bloqueia na hora, sem esperar a checagem de 15 minutos.
        from app.expiry import block_member_on_devices

        if not block_member_on_devices(updated, fleet_for_pool(updated.pool)):
            flash(
                f"Teste: exame de {updated.name} venceu em {when}, mas o facial não apagou o "
                "sócio. Veja se os faciais estão ligados e use “Rodar checagem agora”.",
                "error",
            )
            return jsonify({"ok": True, "blocked": False})
        request_sync()
        flash(
            f"Teste: exame de {updated.name} venceu em {when}. Sócio apagado do facial: "
            "o leitor já deve negar o acesso.",
            "success",
        )
        return jsonify({"ok": True, "blocked": True})
    flash(f"Teste: exame de {updated.name} vale até {when}.", "success")
    return jsonify({"ok": True})


@bp.post("/adm/teste/checar")
@_blocks_update
def adm_teste_checar():
    """Roda agora a checagem que bloqueia vencidos (normalmente a cada 15 min)."""
    forbidden = _test_forbidden()
    if forbidden:
        return forbidden
    from app.expiry import sync_expired_members

    blocked = sync_expired_members()
    if blocked:
        flash(f"Checagem feita: {blocked} sócio(s) bloqueado(s) por exame vencido.", "success")
    else:
        flash("Checagem feita: nenhum sócio novo para bloquear.", "success")
    return jsonify({"ok": True, "blocked": blocked})


@bp.post("/adm/teste/<int:member_id>/encerrar")
@_blocks_update
def adm_teste_encerrar(member_id: int):
    """Volta a data original do exame e solta o sócio para a sincronização."""
    forbidden = _test_forbidden()
    if forbidden:
        return forbidden
    member = db.get_member(member_id)
    if member is None:
        return jsonify({"ok": False, "error": "Sócio não encontrado."}), 404
    original = exam_test.held().get(member.registration)
    if original is None:
        return jsonify({"ok": False, "error": "Esse sócio não está em teste."}), 400
    try:
        updated = _set_test_expiry(member, original)
    except DeviceError as exc:
        logger.exception("Falha ao liberar no facial ao encerrar o teste")
        return jsonify({"ok": False, "error": f"Não foi possível liberar no facial: {exc}"}), 502
    exam_test.release(member.registration)
    request_sync()
    flash(
        f"Teste encerrado: exame de {updated.name} voltou para "
        f"{updated.medical_exam_expires_at.strftime('%d/%m/%Y %H:%M')}.",
        "success",
    )
    return jsonify({"ok": True})


@bp.get("/atualizacao/estado")
def atualizacao_estado():
    """Versão e estado da atualização. Não tem dados de sócios nem chaves."""
    data = updater.status()
    data["local"] = request.remote_addr in {"127.0.0.1", "::1"}
    data["last_result"] = updater.last_result()
    return jsonify(data)


@bp.post("/atualizacao/verificar")
def atualizacao_verificar():
    if not _local_admin_request():
        return jsonify({"ok": False, "error": "Só pelo próprio PC."}), 403
    return jsonify({"ok": True, **updater.check_now()})


@bp.post("/atualizacao/instalar")
def atualizacao_instalar():
    if not _local_admin_request():
        return jsonify({"ok": False, "error": "Instale pelo próprio PC."}), 403
    try:
        updater.install()
    except UpdateError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao iniciar a instalação")
        return jsonify({"ok": False, "error": f"Não foi possível instalar: {exc}"}), 500
    return jsonify({"ok": True, "message": "Instalando. O programa volta sozinho em instantes."})


@bp.get("/saude")
def saude():
    if request.args.get("rapido"):
        # O atualizador usa esta forma: responde sem consultar os faciais.
        return jsonify({"app": "ok", "version": VERSION})
    ip_problem = lan_ip_problem()
    status = {
        "app": "ok",
        "device": None,
        "devices": [],
        "error": None,
        "media_base": None,
        "server_lan_ip_ok": not ip_problem,
        "server_lan_ip_problem": ip_problem or None,
        "version": VERSION,
    }
    try:
        devices = DeviceFleet().status()
        status["devices"] = devices
        status["media_base"] = public_media_url("teste.jpg")
        if not status["server_lan_ip_ok"]:
            status["error"] = (
                (status.get("error") or "")
                + ("; " if status.get("error") else "")
                + ip_problem
            ).strip("; ")
        online = [item for item in devices if item.get("ok")]
        if online and len(online) == len(devices):
            status["device"] = ", ".join(
                str(item.get("model") or item.get("url")) for item in online
            )
        elif online:
            status["device"] = "parcial"
            offline = [item for item in devices if not item.get("ok")]
            status["error"] = "; ".join(
                f"{item.get('url')}: {item.get('error')}" for item in offline
            )
        else:
            status["device"] = "offline"
            status["error"] = "; ".join(
                f"{item.get('url')}: {item.get('error')}" for item in devices
            )
    except Exception as exc:
        status["device"] = "offline"
        status["error"] = str(exc)
    return jsonify(status)
