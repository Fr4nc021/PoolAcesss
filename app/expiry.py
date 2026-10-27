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
_wake_event = threading.Event()
_thread: Optional[threading.Thread] = None


def block_member_on_devices(
    member: db.Member,
    fleet: DeviceFleet,
    reachable: Optional[dict[str, bool]] = None,
) -> bool:
    """Apaga o sócio de cada facial da piscina e só marca bloqueado se saiu de todos.

    Um facial desligado não impede o bloqueio nos outros; o sócio continua
    na lista de vencidos e a próxima checagem tenta o que faltou.
    """
    reachable = {} if reachable is None else reachable
    complete = True
    for device in fleet.clients:
        url = device.base_url or ""
        if url not in reachable:
            try:
                device.ping()
                reachable[url] = True
            except DeviceError as exc:
                reachable[url] = False
                logger.error("Facial %s fora do ar para bloquear vencidos: %s", url, exc)
        if not reachable[url]:
            complete = False
            continue
        try:
            device.block_user(
                device_user_id=member.device_user_id or 0,
                registration=member.registration,
            )
        except DeviceError as exc:
            complete = False
            logger.error("Falha ao bloquear member_id=%s em %s: %s", member.id, url, exc)
    if complete:
        db.mark_blocked(member.id)
    return complete


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
    reachable: dict[str, bool] = {}
    for member in expired:
        fleet = DeviceFleet([client]) if client is not None else fleet_for_pool(member.pool)
        if block_member_on_devices(member, fleet, reachable):
            blocked += 1
            logger.info(
                "Acesso removido por exame vencido: member_id=%s registration=%s",
                member.id,
                member.registration,
            )

    if blocked:
        from app.sync import request_sync

        request_sync()
    return blocked


def _seconds_until_next_check(
    interval: int,
    now: Optional[datetime] = None,
    next_member: Optional[datetime] = None,
) -> float:
    """Checa no intervalo normal, mas também logo depois da hora do vencimento
    e logo depois do próximo vencimento de um sócio (data mudada no ADM)."""
    now = now or datetime.now()
    targets = [next_expiry_time(now)]
    if next_member is not None and next_member > now:
        targets.append(next_member)
    until_expiry = (min(targets) - now).total_seconds() + 5
    return max(1.0, min(float(interval), until_expiry))


def reschedule() -> None:
    """Uma data de exame mudou: recalcula quando a checagem acorda."""
    _wake_event.set()


def _next_wait(interval: int) -> float:
    now = datetime.now()
    try:
        next_member = db.next_member_expiry(now)
    except Exception as exc:  # noqa: BLE001 — sem banco, usa só o intervalo e a hora padrão
        logger.warning("Não foi possível ler o próximo vencimento: %s", exc)
        next_member = None
    return _seconds_until_next_check(interval, now, next_member)


def _worker() -> None:
    interval = max(1, EXPIRY_CHECK_MINUTES) * 60
    logger.info("Checagem de vencimentos iniciada (intervalo=%ss)", interval)
    try:
        sync_expired_members()
    except Exception:
        logger.exception("Erro na checagem inicial de vencimentos")

    while not _stop_event.is_set():
        if _wake_event.wait(_next_wait(interval)):
            # Data mudou: só recalcula a espera. A rota já bloqueou o que venceu.
            _wake_event.clear()
            continue
        if _stop_event.is_set():
            break
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
    _wake_event.set()
