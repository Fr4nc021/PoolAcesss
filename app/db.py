from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Optional

from app.config import DB_PATH, VALIDITY_DAYS, ensure_directories


def _connect() -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                registration TEXT NOT NULL UNIQUE,
                device_user_id INTEGER,
                photo_path TEXT,
                medical_exam_expires_at TEXT NOT NULL,
                access_blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_members_expires
            ON members(medical_exam_expires_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_at TEXT NOT NULL,
                member_name TEXT,
                registration TEXT,
                result TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'local',
                detail TEXT,
                external_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_events_at
            ON access_events(event_at DESC)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_access_events_external
            ON access_events(external_id)
            WHERE external_id IS NOT NULL
            """
        )


@dataclass
class Member:
    id: int
    name: str
    registration: str
    device_user_id: Optional[int]
    photo_path: Optional[str]
    medical_exam_expires_at: datetime
    access_blocked: bool
    created_at: datetime
    updated_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now() >= self.medical_exam_expires_at

    @property
    def days_remaining(self) -> int:
        delta = self.medical_exam_expires_at - datetime.now()
        return max(0, delta.days)

    @property
    def status_label(self) -> str:
        if self.access_blocked and self.is_expired:
            return "Bloqueado — exame vencido"
        if self.access_blocked:
            return "Acesso bloqueado"
        if self.is_expired:
            return "Exame vencido — renovar"
        if self.days_remaining <= 7:
            return f"Vence em {self.days_remaining} dia(s)"
        return "Exame válido"

    @property
    def status_class(self) -> str:
        if self.access_blocked or self.is_expired:
            return "status-expired"
        if self.days_remaining <= 7:
            return "status-warning"
        return "status-ok"

    @property
    def exam_filter(self) -> str:
        if self.is_expired or self.access_blocked:
            return "vencido"
        return "valido"


@dataclass
class AccessEvent:
    id: int
    event_at: datetime
    member_name: Optional[str]
    registration: Optional[str]
    result: str
    source: str
    detail: Optional[str]

    @property
    def result_label(self) -> str:
        mapping = {
            "approved": "Aprovado",
            "denied": "Negado",
            "blocked": "Bloqueado",
            "renewed": "Renovado",
            "registered": "Cadastrado",
            "photo_updated": "Foto atualizada",
        }
        return mapping.get(self.result, self.result)

    @property
    def result_class(self) -> str:
        if self.result in {"approved", "renewed", "registered", "photo_updated"}:
            return "status-ok"
        if self.result in {"denied", "blocked"}:
            return "status-expired"
        return "status-warning"


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_member(row: sqlite3.Row) -> Member:
    return Member(
        id=row["id"],
        name=row["name"],
        registration=row["registration"],
        device_user_id=row["device_user_id"],
        photo_path=row["photo_path"],
        medical_exam_expires_at=_parse_dt(row["medical_exam_expires_at"]),
        access_blocked=bool(row["access_blocked"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> AccessEvent:
    return AccessEvent(
        id=row["id"],
        event_at=_parse_dt(row["event_at"]),
        member_name=row["member_name"],
        registration=row["registration"],
        result=row["result"],
        source=row["source"],
        detail=row["detail"],
    )


def create_member(
    *,
    name: str,
    registration: str,
    device_user_id: int,
    photo_path: Optional[str],
    expires_at: Optional[datetime] = None,
) -> Member:
    now = datetime.now()
    expires = expires_at or (now + timedelta(days=VALIDITY_DAYS))
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO members (
                name, registration, device_user_id, photo_path,
                medical_exam_expires_at, access_blocked, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                name.strip(),
                registration,
                device_user_id,
                photo_path,
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        member_id = cur.lastrowid
    member = get_member(member_id)
    assert member is not None
    log_access_event(
        result="registered",
        member_name=member.name,
        registration=member.registration,
        detail="Cadastro facial com exame médico",
    )
    return member


def get_member(member_id: int) -> Optional[Member]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM members WHERE id = ?", (member_id,)
        ).fetchone()
    return _row_to_member(row) if row else None


def list_members() -> list[Member]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            ORDER BY medical_exam_expires_at ASC, name COLLATE NOCASE ASC
            """
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def count_members() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM members").fetchone()
    return int(row["c"]) if row else 0


def list_blocked_members() -> list[Member]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            WHERE access_blocked = 1
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def list_expiring_soon(days: int = 7) -> list[Member]:
    now = datetime.now()
    limit = now + timedelta(days=days)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            WHERE access_blocked = 0
              AND medical_exam_expires_at >= ?
              AND medical_exam_expires_at <= ?
            ORDER BY medical_exam_expires_at ASC
            """,
            (
                now.isoformat(timespec="seconds"),
                limit.isoformat(timespec="seconds"),
            ),
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def list_expired_members() -> list[Member]:
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            WHERE medical_exam_expires_at < ?
            ORDER BY medical_exam_expires_at ASC
            """,
            (now,),
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def update_member_photo(member_id: int, photo_path: str) -> Optional[Member]:
    now = datetime.now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET photo_path = ?, updated_at = ?
            WHERE id = ?
            """,
            (photo_path, now.isoformat(timespec="seconds"), member_id),
        )
        if cur.rowcount == 0:
            return None
    return get_member(member_id)


def update_device_user_id(member_id: int, device_user_id: int) -> None:
    now = datetime.now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE members
            SET device_user_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (device_user_id, now.isoformat(timespec="seconds"), member_id),
        )


def renew_member(member_id: int, expires_at: Optional[datetime] = None) -> Optional[Member]:
    now = datetime.now()
    expires = expires_at or (now + timedelta(days=VALIDITY_DAYS))
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET medical_exam_expires_at = ?,
                access_blocked = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                member_id,
            ),
        )
        if cur.rowcount == 0:
            return None
    member = get_member(member_id)
    if member:
        log_access_event(
            result="renewed",
            member_name=member.name,
            registration=member.registration,
            detail="OK exame médico",
        )
    return member


def mark_blocked(member_id: int) -> None:
    now = datetime.now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE members
            SET access_blocked = 1, updated_at = ?
            WHERE id = ?
            """,
            (now.isoformat(timespec="seconds"), member_id),
        )
    member = get_member(member_id)
    if member:
        log_access_event(
            result="blocked",
            member_name=member.name,
            registration=member.registration,
            detail="Acesso bloqueado no equipamento",
        )


def list_expired_unblocked() -> list[Member]:
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            WHERE medical_exam_expires_at < ?
              AND access_blocked = 0
              AND device_user_id IS NOT NULL
            """,
            (now,),
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def log_access_event(
    *,
    result: str,
    member_name: Optional[str] = None,
    registration: Optional[str] = None,
    detail: Optional[str] = None,
    source: str = "local",
    event_at: Optional[datetime] = None,
    external_id: Optional[str] = None,
) -> bool:
    """Registra evento. Retorna False se external_id já existia."""
    now = datetime.now()
    when = event_at or now
    with get_connection() as conn:
        if external_id:
            existing = conn.execute(
                "SELECT id FROM access_events WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            if existing:
                return False
        conn.execute(
            """
            INSERT INTO access_events (
                event_at, member_name, registration, result,
                source, detail, external_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when.isoformat(timespec="seconds"),
                member_name,
                registration,
                result,
                source,
                detail,
                external_id,
                now.isoformat(timespec="seconds"),
            ),
        )
    return True


def list_access_events(
    *,
    limit: int = 100,
    result: Optional[str] = None,
    since: Optional[datetime] = None,
) -> list[AccessEvent]:
    clauses: list[str] = []
    params: list[object] = []
    if result:
        clauses.append("result = ?")
        params.append(result)
    if since:
        clauses.append("event_at >= ?")
        params.append(since.isoformat(timespec="seconds"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM access_events
            {where}
            ORDER BY event_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def count_access_events_since(since: datetime, result: Optional[str] = None) -> int:
    clauses = ["event_at >= ?"]
    params: list[object] = [since.isoformat(timespec="seconds")]
    if result:
        clauses.append("result = ?")
        params.append(result)
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM access_events
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()
    return int(row["c"]) if row else 0
