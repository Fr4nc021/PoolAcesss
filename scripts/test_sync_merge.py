"""Testa a junção de cadastros e a troca local, sem facial e sem nuvem."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SYNC_TOKEN", "token-teste")

import app.config as config
import app.db as db
from app.sync import (
    MemberSnapshot,
    _apply_event,
    _apply_remote_member,
    _snapshot_from_payload,
    event_payload,
    member_payload,
    merge_snapshots,
)

config.reload_env = lambda: None  # noqa: E731
os.environ["SYNC_PEER_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["SYNC_TOKEN"] = "token-teste"


def _snap(
    *,
    name: str = "Ana",
    registration: str = "AP00000001",
    expires: datetime,
    updated: datetime,
    blocked: bool = False,
    photo_sha256: str | None = None,
    created: datetime | None = None,
) -> MemberSnapshot:
    return MemberSnapshot(
        name=name,
        registration=registration,
        device_user_id=10,
        photo_sha256=photo_sha256,
        photo_filename=f"{photo_sha256}.jpg" if photo_sha256 else None,
        medical_exam_expires_at=expires,
        access_blocked=blocked,
        created_at=created or updated,
        updated_at=updated,
    )


def test_renewal_clears_stale_block() -> None:
    now = datetime.now().replace(microsecond=0)
    local = _snap(expires=now - timedelta(days=1), updated=now, blocked=True)
    remote = _snap(expires=now + timedelta(days=30), updated=now - timedelta(hours=1), blocked=False)
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.medical_exam_expires_at == remote.medical_exam_expires_at
    assert merged.access_blocked is False
    assert needs_unblock is True


def test_manual_block_with_same_expiry_wins() -> None:
    now = datetime.now().replace(microsecond=0)
    expires = now + timedelta(days=10)
    local = _snap(expires=expires, updated=now - timedelta(hours=2), blocked=False)
    remote = _snap(expires=expires, updated=now, blocked=True, name="Ana Bloqueada")
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.access_blocked is True
    assert merged.name == "Ana Bloqueada"
    assert needs_unblock is False


def test_local_renewal_survives_newer_stale_block() -> None:
    now = datetime.now().replace(microsecond=0)
    local = _snap(expires=now + timedelta(days=30), updated=now - timedelta(minutes=5), blocked=False)
    remote = _snap(expires=now - timedelta(days=1), updated=now, blocked=True)
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.medical_exam_expires_at == local.medical_exam_expires_at
    assert merged.access_blocked is False
    assert needs_unblock is False


def test_newer_photo_wins() -> None:
    now = datetime.now().replace(microsecond=0)
    expires = now + timedelta(days=10)
    local = _snap(expires=expires, updated=now - timedelta(hours=1), photo_sha256="aaa")
    remote = _snap(expires=expires, updated=now, photo_sha256="bbb")
    merged, _needs = merge_snapshots(local, remote)
    assert merged.photo_sha256 == "bbb"


def test_two_databases_share_member_photo_and_event() -> None:
    now = datetime.now().replace(microsecond=0)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        photos = root / "photos"
        photos.mkdir()
        config.DATA_DIR = root
        config.PHOTOS_DIR = photos
        config.DB_PATH = root / "a.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

        photo = photos / "ana.jpg"
        photo.write_bytes(b"foto-ana")
        created = db.create_member(
            name="Ana",
            registration="AP00000001",
            device_user_id=4,
            photo_path=str(photo),
            expires_at=now + timedelta(days=15),
        )
        payload = member_payload(created)
        events = [event_payload(event) for event in db.list_events_for_sync()]
        assert events

        config.DB_PATH = root / "b.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        remote = _snapshot_from_payload(payload)
        assert remote is not None
        _apply_remote_member(remote, lambda: photo.read_bytes())
        for event in events:
            _apply_event(event)
            _apply_event(event)

        copied = db.get_member_by_registration("AP00000001")
        assert copied is not None
        assert copied.name == "Ana"
        assert copied.medical_exam_expires_at == created.medical_exam_expires_at
        assert copied.photo_sha256 == db.file_sha256(str(photo))
        assert copied.photo_path is not None
        assert Path(copied.photo_path).read_bytes() == b"foto-ana"
        assert len(db.list_events_for_sync()) == len(events)

        db.DB_PATH = root / "a.db"
        config.DB_PATH = db.DB_PATH
        origin = db.get_member_by_registration("AP00000001")
        assert origin is not None
        assert db.delete_member(origin.id)
        deletions = [
            {"registration": registration, "deleted_at": deleted_at.isoformat(timespec="seconds")}
            for registration, deleted_at in db.list_deletions()
        ]
        assert deletions

        db.DB_PATH = root / "b.db"
        config.DB_PATH = db.DB_PATH
        from app.sync import _apply_deletion_list

        _apply_deletion_list(deletions)
        assert db.get_member_by_registration("AP00000001") is None
        _apply_remote_member(remote, lambda: photo.read_bytes())
        assert db.get_member_by_registration("AP00000001") is None


def test_http_estado_requires_token() -> None:
    from app.expiry import stop_expiry_worker
    from app.main import create_app
    from app.sync import stop_sync_worker

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config.DATA_DIR = root
        config.PHOTOS_DIR = root / "photos"
        config.DB_PATH = root / "http.db"
        db.DB_PATH = config.DB_PATH
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        denied = client.get("/sync/estado")
        assert denied.status_code == 401
        allowed = client.get(
            "/sync/estado",
            headers={"Authorization": "Bearer token-teste"},
        )
        assert allowed.status_code == 200
        body = allowed.get_json()
        assert body["members"] == []
        assert "events" in body
        page = client.get("/cadastrar")
        try:
            assert page.status_code == 200
            html = page.get_data(as_text=True)
            assert "Outro PC:" in html
            assert "Nuvem:" in html
            assert "não configurado" in html
            assert "não configurada" in html
        finally:
            stop_sync_worker()
            stop_expiry_worker()
            from app import expiry, sync

            if sync._thread is not None:
                sync._thread.join(timeout=3)
            if expiry._thread is not None:
                expiry._thread.join(timeout=3)


def main() -> None:
    test_renewal_clears_stale_block()
    test_manual_block_with_same_expiry_wins()
    test_local_renewal_survives_newer_stale_block()
    test_newer_photo_wins()
    test_two_databases_share_member_photo_and_event()
    test_http_estado_requires_token()
    print("sync ok")


if __name__ == "__main__":
    main()
