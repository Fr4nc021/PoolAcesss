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

from app import db
from app.config import (
    PHOTOS_DIR,
    VALIDITY_DAYS,
    ensure_directories,
    normalize_pool_ids,
    pool_label,
    registration_pools,
    sync_settings,
)
from app.intelbras_client import DeviceError, DeviceFleet, fleet_for_pool, public_media_url
from app.netcheck import lan_ip_problem
from app import updater
from app.sync import build_state, request_sync, run_sync_cycle, status_view
from app.tray import schedule_close_prompt
from app.update_core import UpdateError
from app.version import VERSION

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
APP_TITLE = "Acesso Piscinas Clube Aliança"
_ADM_TABS = {"socios", "vencimentos", "acessos"}


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
    return photo_path


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
    """Fotos servidas para o XPE baixar via FaceUrl."""
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

    ip_problem = lan_ip_problem()
    if ip_problem:
        return fail(f"Cadastro não enviado. {ip_problem}", 500)

    try:
        photo_path = _save_upload(photo, name)
    except Exception as exc:
        logger.exception("Falha ao salvar foto")
        return fail(f"Não foi possível salvar a foto: {exc}", 500)

    face_url = public_media_url(photo_path.name)
    if not face_url:
        return fail(
            "SERVER_LAN_IP não está no .env. O facial precisa desse IP para baixar a foto deste PC.",
            500,
        )
    client = fleet_for_pool(pool)
    try:
        created = client.register_member(
            name,
            photo_path,
            face_public_url=face_url,
        )
        expires_at = datetime.now() + timedelta(days=VALIDITY_DAYS)
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
        f"Exame médico válido até "
        f"{member.medical_exam_expires_at.strftime('%d/%m/%Y %H:%M')}. "
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
        validity_days=VALIDITY_DAYS,
        app_title=APP_TITLE,
    )


@bp.post("/membros/<int:member_id>/renovar")
@_blocks_update
def renovar(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return redirect(url_for("main.membros"))

    photo_path = Path(member.photo_path) if member.photo_path else None
    face_url = None
    if photo_path and photo_path.exists():
        face_url = public_media_url(photo_path.name)

    client = fleet_for_pool(member.pool)
    try:
        created = client.renew_member(
            name=member.name,
            registration=member.registration,
            image_path=photo_path,
            previous_device_user_id=member.device_user_id,
            face_public_url=face_url,
        )
        new_expires = datetime.now() + timedelta(days=VALIDITY_DAYS)
        updated = db.renew_member(member_id, expires_at=new_expires)
        if updated and created.device_user_id != member.device_user_id:
            db.update_device_user_id(member_id, created.device_user_id)
            updated = db.get_member(member_id)
    except DeviceError as exc:
        logger.exception("Falha ao renovar no equipamento")
        flash(f"Não foi possível renovar no equipamento: {exc}", "error")
        return _redirect_after_member_action(url_for("main.membros"))

    request_sync()
    flash(
        f"Exame médico de {updated.name} renovado até "
        f"{updated.medical_exam_expires_at.strftime('%d/%m/%Y %H:%M')}. "
        "Foto confirmada no Intelbras.",
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
        approved_events=db.list_access_events(limit=40, result="approved"),
        denied_events=db.list_access_events(limit=40, result="denied"),
        blocked_events=db.list_access_events(limit=40, result="blocked"),
        change_events=db.list_access_events(
            limit=40,
            results=["registered", "renewed", "photo_updated", "deleted"],
        ),
    )


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

    ip_problem = lan_ip_problem()
    if ip_problem and not member.access_blocked and not member.is_expired:
        flash(f"Foto não enviada. {ip_problem}", "error")
        return _redirect_adm("socios")

    try:
        photo_path = _save_upload(photo, member.name)
    except Exception as exc:
        logger.exception("Falha ao salvar nova foto")
        flash(f"Não foi possível salvar a foto: {exc}", "error")
        return _redirect_adm("socios")

    face_url = public_media_url(photo_path.name)
    if not face_url and not member.access_blocked and not member.is_expired:
        if photo_path.exists():
            photo_path.unlink(missing_ok=True)
        flash(
            "SERVER_LAN_IP não está no .env. O facial precisa desse IP para receber a foto.",
            "error",
        )
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
                face_public_url=face_url,
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
