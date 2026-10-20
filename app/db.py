from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from app.config import DB_PATH, VALIDITY_DAYS, ensure_directories, pool_label


def _connect() -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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
        _ensure_column(conn, "members", "photo_sha256", "TEXT")
        _ensure_column(conn, "members", "device_pending", "TEXT")
        _ensure_column(conn, "members", "pool", "TEXT")
        _ensure_column(conn, "access_events", "sync_id", "TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_access_events_sync
            ON access_events(sync_id)
            WHERE sync_id IS NOT NULL
            """
        )
        _backfill_photo_hashes(conn)
        _backfill_event_sync_ids(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS member_deletions (
                registration TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            )
            """
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def file_sha256(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backfill_photo_hashes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, photo_path FROM members
        WHERE photo_sha256 IS NULL AND photo_path IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        digest = file_sha256(row["photo_path"])
        if digest:
            conn.execute(
                "UPDATE members SET photo_sha256 = ? WHERE id = ?",
                (digest, row["id"]),
            )


def _backfill_event_sync_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM access_events WHERE sync_id IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE access_events SET sync_id = ? WHERE id = ?",
            (str(uuid.uuid4()), row["id"]),
        )


@dataclass
class Member:
    id: int
    name: str
    registration: str
    device_user_id: Optional[int]
    photo_path: Optional[str]
    photo_sha256: Optional[str]
    medical_exam_expires_at: datetime
    access_blocked: bool
    created_at: datetime
    updated_at: datetime
    device_pending: Optional[str] = None
    pool: Optional[str] = None

    @property
    def pool_label(self) -> str:
        return pool_label(self.pool)

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
    sync_id: Optional[str] = None
    external_id: Optional[str] = None
    created_at: Optional[datetime] = None

    @property
    def result_label(self) -> str:
        mapping = {
            "approved": "Aprovado",
            "denied": "Negado",
            "blocked": "Bloqueado",
            "renewed": "Renovado",
            "registered": "Cadastrado",
            "photo_updated": "Foto atualizada",
            "deleted": "Excluído",
            "deleted": "Excluído",
        }
        return mapping.get(self.result, self.result)

    @property
    def result_class(self) -> str:
        if self.result in {"approved", "renewed", "registered", "photo_updated"}:
            return "status-ok"
        if self.result in {"denied", "blocked", "deleted"}:
            return "status-expired"
        return "status-warning"


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_member(row: sqlite3.Row) -> Member:
    keys = row.keys()
    return Member(
        id=row["id"],
        name=row["name"],
        registration=row["registration"],
        device_user_id=row["device_user_id"],
        photo_path=row["photo_path"],
        photo_sha256=row["photo_sha256"] if "photo_sha256" in keys else None,
        medical_exam_expires_at=_parse_dt(row["medical_exam_expires_at"]),
        access_blocked=bool(row["access_blocked"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        device_pending=row["device_pending"] if "device_pending" in keys else None,
        pool=row["pool"] if "pool" in keys else None,
    )


def _row_to_event(row: sqlite3.Row) -> AccessEvent:
    keys = row.keys()
    created = row["created_at"] if "created_at" in keys else None
    return AccessEvent(
        id=row["id"],
        event_at=_parse_dt(row["event_at"]),
        member_name=row["member_name"],
        registration=row["registration"],
        result=row["result"],
        source=row["source"],
        detail=row["detail"],
        sync_id=row["sync_id"] if "sync_id" in keys else None,
        external_id=row["external_id"] if "external_id" in keys else None,
        created_at=_parse_dt(created) if created else None,
    )


def create_member(
    *,
    name: str,
    registration: str,
    device_user_id: int,
    photo_path: Optional[str],
    expires_at: Optional[datetime] = None,
    pool: Optional[str] = None,
) -> Member:
    now = datetime.now()
    expires = expires_at or (now + timedelta(days=VALIDITY_DAYS))
    digest = file_sha256(photo_path)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO members (
                name, registration, device_user_id, photo_path, photo_sha256,
                medical_exam_expires_at, access_blocked, created_at, updated_at,
                pool
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                name.strip(),
                registration,
                device_user_id,
                photo_path,
                digest,
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                pool,
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


def get_member_by_registration(registration: str) -> Optional[Member]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM members WHERE registration = ?", (registration,)
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
    digest = file_sha256(photo_path)
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET photo_path = ?, photo_sha256 = ?, updated_at = ?
            WHERE id = ?
            """,
            (photo_path, digest, now.isoformat(timespec="seconds"), member_id),
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


def delete_member(member_id: int) -> bool:
    now = datetime.now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT registration FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            return False
        _record_deletion(conn, row["registration"], now)
        cur = conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
        return cur.rowcount > 0


def record_deletion(registration: str, deleted_at: datetime) -> None:
    """Guarda a exclusão e tira o sócio local se a exclusão for a versão mais nova."""
    photo: Optional[str] = None
    with get_connection() as conn:
        _record_deletion(conn, registration, deleted_at)
        current = conn.execute(
            "SELECT photo_path, updated_at FROM members WHERE registration = ?",
            (registration,),
        ).fetchone()
        if current is None:
            return
        if _parse_dt(current["updated_at"]) <= deleted_at.replace(microsecond=0):
            photo = current["photo_path"]
            conn.execute(
                "DELETE FROM members WHERE registration = ?",
                (registration,),
            )
    if photo:
        path = Path(photo)
        if path.is_file():
            path.unlink(missing_ok=True)


def clear_deletion(registration: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM member_deletions WHERE registration = ?",
            (registration,),
        )


def get_deletion(registration: str) -> Optional[datetime]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM member_deletions WHERE registration = ?",
            (registration,),
        ).fetchone()
    return _parse_dt(row["deleted_at"]) if row else None


def list_deletions() -> list[tuple[str, datetime]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT registration, deleted_at FROM member_deletions ORDER BY registration"
        ).fetchall()
    return [(row["registration"], _parse_dt(row["deleted_at"])) for row in rows]


def _record_deletion(conn: sqlite3.Connection, registration: str, deleted_at: datetime) -> None:
    stamp = deleted_at.replace(microsecond=0).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT deleted_at FROM member_deletions WHERE registration = ?",
        (registration,),
    ).fetchone()
    if row is not None and _parse_dt(row["deleted_at"]) >= deleted_at.replace(microsecond=0):
        return
    conn.execute(
        """
        INSERT INTO member_deletions (registration, deleted_at)
        VALUES (?, ?)
        ON CONFLICT(registration) DO UPDATE SET deleted_at = excluded.deleted_at
        """,
        (registration, stamp),
    )


def set_exam_expiry(
    member_id: int,
    expires_at: datetime,
    *,
    unblock: bool = False,
) -> Optional[Member]:
    """Grava a data do exame sem registrar renovação. Usado pela aba Teste."""
    now = datetime.now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET medical_exam_expires_at = ?,
                access_blocked = CASE WHEN ? THEN 0 ELSE access_blocked END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                expires_at.isoformat(timespec="seconds"),
                1 if unblock else 0,
                now.isoformat(timespec="seconds"),
                member_id,
            ),
        )
        if cur.rowcount == 0:
            return None
    return get_member(member_id)


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
    sync_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> bool:
    """Registra evento. Retorna False se external_id ou sync_id já existia."""
    now = created_at or datetime.now()
    when = event_at or now
    event_sync_id = sync_id or str(uuid.uuid4())
    with get_connection() as conn:
        if external_id:
            existing = conn.execute(
                "SELECT id FROM access_events WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            if existing:
                return False
        existing_sync = conn.execute(
            "SELECT id FROM access_events WHERE sync_id = ?",
            (event_sync_id,),
        ).fetchone()
        if existing_sync:
            return False
        conn.execute(
            """
            INSERT INTO access_events (
                event_at, member_name, registration, result,
                source, detail, external_id, created_at, sync_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event_sync_id,
            ),
        )
    return True


def list_events_for_sync() -> list[AccessEvent]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM access_events
            WHERE sync_id IS NOT NULL
            ORDER BY event_at ASC, id ASC
            """
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def upsert_synced_member(
    *,
    name: str,
    registration: str,
    device_user_id: Optional[int],
    photo_path: Optional[str],
    photo_sha256: Optional[str],
    expires_at: datetime,
    access_blocked: bool,
    created_at: datetime,
    updated_at: datetime,
    mark_unblock_pending: bool,
    pool: Optional[str] = None,
) -> Member:
    """Grava a junção pela matrícula sem criar evento nem falar com o facial."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, device_pending FROM members WHERE registration = ?",
            (registration,),
        ).fetchone()
        pending = None
        if row is not None and row["device_pending"]:
            pending = row["device_pending"]
        if mark_unblock_pending:
            pending = "unblock"
        conn.execute(
            "DELETE FROM member_deletions WHERE registration = ?",
            (registration,),
        )
        values = (
            name.strip(),
            device_user_id,
            photo_path,
            photo_sha256,
            expires_at.isoformat(timespec="seconds"),
            1 if access_blocked else 0,
            created_at.isoformat(timespec="seconds"),
            updated_at.isoformat(timespec="seconds"),
            pending,
            pool,
            registration,
        )
        if row is None:
            conn.execute(
                """
                INSERT INTO members (
                    name, registration, device_user_id, photo_path, photo_sha256,
                    medical_exam_expires_at, access_blocked, created_at, updated_at,
                    device_pending, pool
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    registration,
                    device_user_id,
                    photo_path,
                    photo_sha256,
                    expires_at.isoformat(timespec="seconds"),
                    1 if access_blocked else 0,
                    created_at.isoformat(timespec="seconds"),
                    updated_at.isoformat(timespec="seconds"),
                    pending,
                    pool,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE members
                SET name = ?,
                    device_user_id = ?,
                    photo_path = ?,
                    photo_sha256 = ?,
                    medical_exam_expires_at = ?,
                    access_blocked = ?,
                    created_at = ?,
                    updated_at = ?,
                    device_pending = ?,
                    pool = ?
                WHERE registration = ?
                """,
                values,
            )
    member = get_member_by_registration(registration)
    assert member is not None
    return member


def list_unblock_pending() -> list[Member]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM members
            WHERE device_pending = 'unblock'
            ORDER BY registration
            """
        ).fetchall()
    return [_row_to_member(row) for row in rows]


def clear_device_pending(member_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE members SET device_pending = NULL WHERE id = ?",
            (member_id,),
        )


def set_photo_sha256(member_id: int, digest: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE members SET photo_sha256 = ? WHERE id = ?",
            (digest, member_id),
        )


def list_access_events(
    *,
    limit: int = 100,
    result: Optional[str] = None,
    results: Optional[list[str]] = None,
    since: Optional[datetime] = None,
) -> list[AccessEvent]:
    clauses: list[str] = []
    params: list[object] = []
    if result:
        clauses.append("result = ?")
        params.append(result)
    elif results:
        placeholders = ", ".join("?" for _ in results)
        clauses.append(f"result IN ({placeholders})")
        params.extend(results)
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
