"""Testa a aba Teste da ADM: data de exame, bloqueio e volta, sem facial e sem nuvem."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config

config.reload_env = lambda: None  # noqa: E731
os.environ["SYNC_PEER_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["SYNC_TOKEN"] = "token-teste"

import app.db as db
import app.expiry as expiry
import app.main as main
import app.routes as routes
from app import exam_test, sync
from app.intelbras_client import CreatedUser


class FakeFleet:
    """Facial de mentira: guarda quem está cadastrado."""

    def __init__(self) -> None:
        self.users: set[str] = set()
        self.clients = [self]
        self.base_url = "http://facial-teste"

    def status(self) -> list[dict]:
        return [{"ok": True, "url": self.base_url}]

    def block_user(self, *, device_user_id: int, registration: str) -> None:
        self.users.discard(registration)

    def renew_member(self, *, registration: str, **_kwargs) -> CreatedUser:
        self.users.add(registration)
        return CreatedUser(device_user_id=7, registration=registration)


LOCAL = {"X-Acesso-Local": "1", "Accept": "application/json"}


def _client(app, addr: str = "127.0.0.1"):
    app.config["SERVER_NAME"] = f"127.0.0.1:{config.PORT}"
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = addr
    return client


def test_tab_blocks_and_restores() -> None:
    fleet = FakeFleet()
    routes.fleet_for_pool = lambda _pool: fleet
    expiry.fleet_for_pool = lambda _pool: fleet
    main.start_expiry_worker = lambda: None
    main.start_sync_worker = lambda: None
    app = main.create_app()

    original = (datetime.now() + timedelta(days=20)).replace(second=0, microsecond=0)
    member = db.create_member(
        name="Ana Teste",
        registration="T001",
        device_user_id=7,
        photo_path=None,
        expires_at=original,
    )
    fleet.users.add("T001")
    client = _client(app)

    page = client.get("/adm").get_data(as_text=True)
    assert 'data-tab="teste"' in page, "aba aparece no PC local"
    remote_page = _client(app, "192.168.0.9").get("/adm").get_data(as_text=True)
    assert 'data-tab="teste"' not in remote_page, "aba some para outro PC da rede"

    denied = client.post("/adm/teste/data", json={"member_id": member.id, "expires_at": "2026-01-01T10:00"})
    assert denied.status_code == 403, "sem o header local é recusado"

    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    response = client.post("/adm/teste/data", json={"member_id": member.id, "expires_at": past}, headers=LOCAL)
    assert response.get_json()["ok"], response.get_json()
    assert exam_test.is_held("T001")
    assert db.get_member(member.id).is_expired

    state = sync.build_state()
    assert all(item["registration"] != "T001" for item in state["members"]), "não vai para o outro PC"
    longer = sync.MemberSnapshot(
        name="Ana Teste",
        registration="T001",
        device_user_id=7,
        photo_sha256=None,
        photo_filename=None,
        medical_exam_expires_at=original,
        access_blocked=False,
        created_at=datetime.now(),
        updated_at=datetime.now() + timedelta(minutes=5),
    )
    sync._apply_remote_member(longer, lambda: None)
    assert db.get_member(member.id).is_expired, "outro PC não desfaz a data de teste"

    response = client.post("/adm/teste/checar", headers=LOCAL)
    assert response.get_json()["blocked"] == 1
    assert db.get_member(member.id).access_blocked
    assert "T001" not in fleet.users, "face saiu do facial"

    page = client.get("/adm").get_data(as_text=True)
    assert original.strftime("%d/%m/%Y %H:%M") in page, "lista mostra a data original"

    response = client.post(f"/adm/teste/{member.id}/encerrar", headers=LOCAL)
    assert response.get_json()["ok"], response.get_json()
    restored = db.get_member(member.id)
    assert restored.medical_exam_expires_at == original
    assert not restored.access_blocked
    assert "T001" in fleet.users, "face voltou para o facial"
    assert not exam_test.is_held("T001")


def test_future_date_reopens_blocked_member() -> None:
    fleet = FakeFleet()
    routes.fleet_for_pool = lambda _pool: fleet
    app = main.create_app()
    member = db.create_member(
        name="Bia Teste",
        registration="T002",
        device_user_id=8,
        photo_path=None,
        expires_at=datetime.now() - timedelta(days=1),
    )
    db.mark_blocked(member.id)
    client = _client(app)

    future = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
    response = client.post("/adm/teste/data", json={"member_id": member.id, "expires_at": future}, headers=LOCAL)
    assert response.get_json()["ok"], response.get_json()
    updated = db.get_member(member.id)
    assert not updated.access_blocked and not updated.is_expired
    assert "T002" in fleet.users

    client.post(f"/adm/teste/{member.id}/encerrar", headers=LOCAL)
    assert db.get_member(member.id).is_expired, "volta para a data vencida original"
    assert not exam_test.is_held("T002")


def main_() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config.PHOTOS_DIR = root / "photos"
        config.PHOTOS_DIR.mkdir()
        config.DB_PATH = root / "teste.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        test_tab_blocks_and_restores()
        test_future_date_reopens_blocked_member()
    print("exam test ok")


if __name__ == "__main__":
    main_()
