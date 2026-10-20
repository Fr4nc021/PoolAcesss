"""Testa o vencimento na hora padrão (EXPIRY_HOUR), sem facial e sem nuvem."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config
import app.db as db
from app.config import EXPIRY_HOUR, VALIDITY_DAYS, exam_expires_from, next_expiry_time
from app.expiry import _seconds_until_next_check


def test_exam_expires_at_fixed_hour() -> None:
    for start in (datetime(2026, 10, 16, 8, 5), datetime(2026, 10, 16, 23, 40)):
        expires = exam_expires_from(start)
        assert (expires.date() - start.date()).days == VALIDITY_DAYS
        assert (expires.hour, expires.minute, expires.second) == (EXPIRY_HOUR, 0, 0)


def test_next_expiry_time() -> None:
    before = datetime(2026, 11, 15, EXPIRY_HOUR - 1, 30)
    assert next_expiry_time(before) == datetime(2026, 11, 15, EXPIRY_HOUR)
    at = datetime(2026, 11, 15, EXPIRY_HOUR)
    assert next_expiry_time(at) == datetime(2026, 11, 16, EXPIRY_HOUR)


def test_check_wakes_right_after_expiry_hour() -> None:
    interval = 15 * 60
    near = datetime(2026, 11, 15, EXPIRY_HOUR - 1, 58)
    assert _seconds_until_next_check(interval, near) == 125, "acorda 5 s depois da hora"
    far = datetime(2026, 11, 15, 10, 0)
    assert _seconds_until_next_check(interval, far) == interval


def test_new_and_renewed_members_use_fixed_hour() -> None:
    member = db.create_member(name="Ana", registration="H001", device_user_id=1, photo_path=None)
    assert member.medical_exam_expires_at.hour == EXPIRY_HOUR
    assert member.medical_exam_expires_at.minute == 0
    renewed = db.renew_member(member.id)
    assert renewed is not None and renewed.medical_exam_expires_at.hour == EXPIRY_HOUR


def test_startup_moves_existing_times_later() -> None:
    early = db.create_member(
        name="Bia", registration="H002", device_user_id=2, photo_path=None,
        expires_at=datetime(2026, 11, 15, 14, 30),
    )
    late = db.create_member(
        name="Caio", registration="H003", device_user_id=3, photo_path=None,
        expires_at=datetime(2026, 11, 15, 23, 10),
    )
    blocked = db.create_member(
        name="Duda", registration="H004", device_user_id=4, photo_path=None,
        expires_at=datetime(2026, 1, 10, 9, 0),
    )
    db.mark_blocked(blocked.id)
    db.init_db()
    assert db.get_member(early.id).medical_exam_expires_at == datetime(2026, 11, 15, EXPIRY_HOUR)
    assert db.get_member(late.id).medical_exam_expires_at == datetime(2026, 11, 15, 23, 10), "nunca adianta"
    assert db.get_member(blocked.id).medical_exam_expires_at == datetime(2026, 1, 10, 9, 0)


def main() -> None:
    test_exam_expires_at_fixed_hour()
    test_next_expiry_time()
    test_check_wakes_right_after_expiry_hour()
    with tempfile.TemporaryDirectory() as tmp:
        config.DB_PATH = Path(tmp) / "teste.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        test_new_and_renewed_members_use_fixed_hour()
        test_startup_moves_existing_times_later()
    print("expiry hour ok")


if __name__ == "__main__":
    main()
