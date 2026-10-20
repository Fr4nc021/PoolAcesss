from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from app import db
from app.config import EXPIRY_CHECK_MINUTES, next_expiry_time
from app.intelbras_client import DeviceError, DeviceFleet, IntelbrasClient, fleet_for_pool

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def sync_expired_members(client: Optional[IntelbrasClient] = None) -> int:
    """Remove dos faciais membros com exame médico vencido."""
    try:
        from app.sync import refresh_from_peers

        refresh_from_peers()
    except Exception:
        logger.exception("Falha ao atualizar cadastros antes de bloquear vencidos")
    expired = db.list_expired_unblocked()
    if not expired:
        return 0

    blocked = 0
    ready: dict[str, DeviceFleet] = {}
    offline: set[str] = set()

    for member in expired:
        if client is not None:
            fleet = DeviceFleet([client])
            key = client.base_url or ""
        else:
            fleet = fleet_for_pool(member.pool)
            key = ",".join(item.base_url or "" for item in fleet.clients)
        if key in offline:
            continue
        if key not in ready:
            unreachable = [item for item in fleet.status() if not item.get("ok")]
            if unreachable:
                offline.add(key)
                logger.error(
                    "Não foi possível autenticar no facial para bloquear vencidos: %s",
                    "; ".join(
                        f"{item.get('url')}: {item.get('error')}" for item in unreachable
                    ),
                )
                continue
            ready[key] = fleet
        fleet = ready[key]
        try:
            fleet.block_user(
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
            logger.error("Falha ao bloquear member_id=%s: %s", member.id, exc)

    if blocked:
        from app.sync import request_sync

        request_sync()
    return blocked


def _seconds_until_next_check(interval: int, now: Optional[datetime] = None) -> float:
    """Checa no intervalo normal, mas também logo depois da hora do vencimento."""
    now = now or datetime.now()
    until_expiry = (next_expiry_time(now) - now).total_seconds() + 5
    return max(1.0, min(float(interval), until_expiry))


def _worker() -> None:
    interval = max(1, EXPIRY_CHECK_MINUTES) * 60
    logger.info("Checagem de vencimentos iniciada (intervalo=%ss)", interval)
    try:
        sync_expired_members()
    except Exception:
        logger.exception("Erro na checagem inicial de vencimentos")

    while not _stop_event.wait(_seconds_until_next_check(interval)):
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
