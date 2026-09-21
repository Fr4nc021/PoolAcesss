from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
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
from app.config import PHOTOS_DIR, VALIDITY_DAYS, ensure_directories
from app.intelbras_client import DeviceError, IntelbrasClient, public_media_url

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
APP_TITLE = "Acesso Piscinas Clube Aliança"


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


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
    """Importa logs do equipamento para o banco local. Retorna quantos novos."""
    try:
        items = IntelbrasClient().list_door_logs(max_items=300)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sync de logs do XPE falhou: %s", exc)
        return 0

    added = 0
    for item in items:
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
        external_id = str(
            item.get("ID")
            or item.get("LogID")
            or f"{registration}-{event_at.isoformat()}-{item.get('Status', '')}"
        )
        result = _normalize_door_result(item)
        inserted = db.log_access_event(
            result=result,
            member_name=str(name) if name else None,
            registration=str(registration) if registration else None,
            detail="Log do equipamento facial",
            source="device",
            event_at=event_at,
            external_id=external_id,
        )
        if inserted:
            added += 1
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


@bp.route("/cadastrar", methods=["GET", "POST"])
def cadastrar():
    if request.method == "GET":
        return render_template(
            "cadastrar.html",
            validity_days=VALIDITY_DAYS,
            app_title=APP_TITLE,
        )

    name = (request.form.get("name") or "").strip()
    photo = request.files.get("photo")

    def fail(message: str, code: int = 400):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), code
        flash(message, "error")
        return redirect(url_for("main.cadastrar"))

    if not name:
        return fail("Informe o nome da pessoa.")

    if not photo or not photo.filename:
        return fail("Selecione uma foto.")

    if not _allowed_file(photo.filename):
        return fail("Formato de foto inválido. Use JPG ou PNG.")

    try:
        photo_path = _save_upload(photo, name)
    except Exception as exc:
        logger.exception("Falha ao salvar foto")
        return fail(f"Não foi possível salvar a foto: {exc}", 500)

    face_url = public_media_url(photo_path.name)
    client = IntelbrasClient()
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
        f"No XPE, abra Controle de Acesso → Usuários e busque pelo nome ou pela matrícula "
        f"{member.registration}."
    )
    if not created.face_ok:
        msg += (
            " Aviso: a face pode não ter sido validada. Confira a foto no aparelho."
        )

    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "message": msg,
                "face_ok": created.face_ok,
                "redirect": url_for("main.membros"),
            }
        )

    flash(msg, "success" if created.face_ok else "error")
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
def renovar(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return redirect(url_for("main.membros"))

    photo_path = Path(member.photo_path) if member.photo_path else None
    face_url = None
    if photo_path and photo_path.exists():
        face_url = public_media_url(photo_path.name)

    client = IntelbrasClient()
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
        return redirect(request.referrer or url_for("main.membros"))

    flash(
        f"Exame médico de {updated.name} renovado até "
        f"{updated.medical_exam_expires_at.strftime('%d/%m/%Y %H:%M')}.",
        "success",
    )
    return redirect(request.referrer or url_for("main.membros"))


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
    )


@bp.post("/adm/membros/<int:member_id>/bloquear")
def adm_bloquear(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return redirect(url_for("main.adm"))

    client = IntelbrasClient()
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
        return redirect(url_for("main.adm"))

    flash(f"Acesso de {member.name} bloqueado.", "success")
    return redirect(url_for("main.adm"))


@bp.post("/adm/membros/<int:member_id>/foto")
def adm_atualizar_foto(member_id: int):
    member = db.get_member(member_id)
    if member is None:
        flash("Membro não encontrado.", "error")
        return redirect(url_for("main.adm"))

    photo = request.files.get("photo")
    if not photo or not photo.filename:
        flash("Selecione uma nova foto.", "error")
        return redirect(url_for("main.adm"))
    if not _allowed_file(photo.filename):
        flash("Formato de foto inválido. Use JPG ou PNG.", "error")
        return redirect(url_for("main.adm"))

    try:
        photo_path = _save_upload(photo, member.name)
    except Exception as exc:
        logger.exception("Falha ao salvar nova foto")
        flash(f"Não foi possível salvar a foto: {exc}", "error")
        return redirect(url_for("main.adm"))

    face_url = public_media_url(photo_path.name)
    client = IntelbrasClient()
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
        flash(f"Não foi possível atualizar a foto no equipamento: {exc}", "error")
        return redirect(url_for("main.adm"))

    flash(f"Foto de {member.name} atualizada.", "success")
    return redirect(url_for("main.adm"))


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
    return redirect(url_for("main.adm"))


@bp.get("/saude")
def saude():
    status = {"app": "ok", "device": None, "error": None, "media_base": None}
    try:
        info = IntelbrasClient().ping()
        model = ((info.get("data") or {}).get("Status") or {}).get("Model")
        status["device"] = model or "conectado"
        status["media_base"] = public_media_url("teste.jpg")
    except Exception as exc:
        status["device"] = "offline"
        status["error"] = str(exc)
    return jsonify(status)
