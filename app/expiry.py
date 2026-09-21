from __future__ import annotations

import logging
import threading
from typing import Optional

from app import db
from app.config import EXPIRY_CHECK_MINUTES
from app.intelbras_client import DeviceError, IntelbrasClient

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def sync_expired_members(client: Optional[IntelbrasClient] = None) -> int:
    """Remove do equipamento membros com exame médico vencido."""
    expired = db.list_expired_unblocked()
    if not expired:
        return 0

    device = client or IntelbrasClient()
    blocked = 0

    try:
        device.ping()
    except DeviceError as exc:
        logger.error("Não foi possível autenticar no equipamento para bloquear vencidos: %s", exc)
        return 0

    for member in expired:
        try:
            device.block_user(
                device_user_id=member.device_user_id or 0,
                registration=member.registration,
            )
            db.mark_blocked(member.id)
            blocked += 1
            logger.info(
                "Acesso removido por exame vencido: member_id=%s registration=%s",
                member.id,
                member.registration,
            )
        except DeviceError as exc:
            # Se já não existe no aparelho, ainda marcamos como bloqueado localmente
            msg = str(exc).lower()
            if "failed" in msg or "não" in msg or "not" in msg:
                db.mark_blocked(member.id)
                blocked += 1
                logger.warning(
                    "Membro %s marcado bloqueado (aparelho: %s)",
                    member.id,
                    exc,
                )
            else:
                logger.error("Falha ao bloquear member_id=%s: %s", member.id, exc)

    return blocked


def _worker() -> None:
    interval = max(1, EXPIRY_CHECK_MINUTES) * 60
    logger.info("Checagem de vencimentos iniciada (intervalo=%ss)", interval)
    try:
        sync_expired_members()
    except Exception:
        logger.exception("Erro na checagem inicial de vencimentos")

    while not _stop_event.wait(interval):
        try:
            sync_expired_members()
        except Exception:
            logger.exception("Erro na checagem periódica de vencimentos")


def start_expiry_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker, name="expiry-worker", daemon=True)
    _thread.start()


def stop_expiry_worker() -> None:
    _stop_event.set()
