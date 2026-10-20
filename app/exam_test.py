"""Sócios em teste de vencimento na aba Teste da ADM.

A sincronização fica com a data de exame mais longa, então uma data
puxada para trás voltaria em segundos. Quem está em teste fica só neste
PC: não sobe para o outro PC nem para a nuvem e não é sobrescrito por
eles. A data original fica guardada aqui até o teste ser encerrado.
Fechar o programa solta todos; a próxima sincronização devolve a data
mais longa e reabre o facial.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
_held: dict[str, datetime] = {}


def hold(registration: str, original_expires_at: datetime) -> None:
    """Retém o sócio. A data original é a do primeiro ajuste."""
    with _lock:
        _held.setdefault(registration, original_expires_at)


def release(registration: str) -> Optional[datetime]:
    with _lock:
        return _held.pop(registration, None)


def is_held(registration: str) -> bool:
    with _lock:
        return registration in _held


def held() -> dict[str, datetime]:
    with _lock:
        return dict(_held)
