"""Testa a troca de piscinas de um sócio, sem facial e sem nuvem."""

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
import app.main as main
import app.routes as routes
from app.intelbras_client import CreatedUser, DeviceError

# URL do facial -> matrículas cadastradas nele.
DEVICES: dict[str, set[str]] = {}
OFFLINE: set[str] = set()


class FakeClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def renew_member(self, *, registration: str, **_kwargs) -> CreatedUser:
        if self.base_url in OFFLINE:
            raise DeviceError("facial desligado")
        DEVICES.setdefault(self.base_url, set()).add(registration)
        return CreatedUser(device_user_id=3, registration=registration)

    def delete_user(self, *, registration: str, **_kwargs) -> None:
        if self.base_url in OFFLINE:
            raise DeviceError("facial desligado")
        DEVICES.setdefault(self.base_url, set()).discard(registration)


def _setup():
    routes.IntelbrasClient = FakeClient
    main.start_expiry_worker = lambda: None
    main.start_sync_worker = lambda: None
    app = main.create_app()
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    pools = {item["id"]: item["url"] for item in config.registration_pools()}
    return client, pools["normal"], pools["termica"]


def _member(registration: str, pool: str | None, photo: Path, **kwargs) -> db.Member:
    return db.create_member(
        name=f"Sócio {registration}",
        registration=registration,
        device_user_id=1,
        photo_path=str(photo),
        pool=pool,
        **kwargs,
    )


def test_add_and_remove_pool(photo: Path) -> None:
    client, normal, termica = _setup()
    member = _member("P001", "normal", photo)
    DEVICES.setdefault(normal, set()).add("P001")
    before = db.get_member(member.id)

    client.post(f"/adm/membros/{member.id}/piscinas", data={"pool": ["normal", "termica"]})
    after = db.get_member(member.id)
    assert after.pool == "normal,termica"
    assert "P001" in DEVICES[termica] and "P001" in DEVICES[normal]
    assert after.registration == before.registration
    assert after.medical_exam_expires_at == before.medical_exam_expires_at

    client.post(f"/adm/membros/{member.id}/piscinas", data={"pool": ["termica"]})
    assert db.get_member(member.id).pool == "termica"
    assert "P001" not in DEVICES[normal] and "P001" in DEVICES[termica]

    events = db.list_access_events(limit=5, results=["pool_updated"])
    assert len(events) == 2 and events[0].result_label == "Piscinas alteradas"


def test_blocked_member_only_saves_choice(photo: Path) -> None:
    client, _normal, termica = _setup()
    member = _member("P002", "normal", photo, expires_at=datetime.now() - timedelta(days=1))
    db.mark_blocked(member.id)
    client.post(f"/adm/membros/{member.id}/piscinas", data={"pool": ["normal", "termica"]})
    assert db.get_member(member.id).pool == "normal,termica"
    assert "P002" not in DEVICES.get(termica, set()), "bloqueado não volta ao facial"


def test_offline_device_keeps_old_pool(photo: Path) -> None:
    client, normal, termica = _setup()
    member = _member("P003", None, photo)
    DEVICES.setdefault(normal, set()).add("P003")
    DEVICES.setdefault(termica, set()).add("P003")

    OFFLINE.add(normal)
    client.post(f"/adm/membros/{member.id}/piscinas", data={"pool": ["termica"]})
    assert db.get_member(member.id).pool == "normal,termica", "continua na normal se não saiu do facial"
    OFFLINE.clear()

    OFFLINE.add(termica)
    member2 = _member("P004", "normal", photo)
    client.post(f"/adm/membros/{member2.id}/piscinas", data={"pool": ["normal", "termica"]})
    assert db.get_member(member2.id).pool == "normal", "não grava se não entrou no facial"
    OFFLINE.clear()


def test_requires_a_pool(photo: Path) -> None:
    client, _normal, _termica = _setup()
    member = _member("P005", "normal", photo)
    client.post(f"/adm/membros/{member.id}/piscinas", data={})
    assert db.get_member(member.id).pool == "normal"


def main_() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config.PHOTOS_DIR = root / "photos"
        config.PHOTOS_DIR.mkdir()
        config.DB_PATH = root / "teste.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        photo = config.PHOTOS_DIR / "rosto.jpg"
        photo.write_bytes(b"jpeg")
        test_add_and_remove_pool(photo)
        test_blocked_member_only_saves_choice(photo)
        test_offline_device_keeps_old_pool(photo)
        test_requires_a_pool(photo)
    print("pool change ok")


if __name__ == "__main__":
    main_()
