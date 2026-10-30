"""Testa a aba Configurações, a validade mudada no ADM e o OK exame de quem venceu.

Sem facial, sem outro PC e sem nuvem. O .env do projeto não é tocado: o
teste grava num arquivo temporário.
"""

from __future__ import annotations

import os
import random
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
os.environ["SERVER_LAN_IP"] = "192.168.0.20"

from PIL import Image

import app.db as db
import app.expiry as expiry
import app.main as main
import app.routes as routes
import app.settings as app_settings
from app import sync
from app.intelbras_client import CreatedUser

app_settings.reload_env = lambda: None  # noqa: E731

LOCAL = {"X-Acesso-Local": "1", "Accept": "application/json"}


class FakeFleet:
    """Facial de mentira: guarda quem está cadastrado e a foto recebida."""

    def __init__(self) -> None:
        self.users: set[str] = set()
        self.clients = [self]
        self.base_url = "http://facial-teste"
        self.sent: list[dict] = []

    def ping(self) -> dict:
        return {"retcode": 0}

    def block_user(self, *, device_user_id: int, registration: str) -> None:
        self.users.discard(registration)

    def renew_member(self, *, registration: str, **kwargs) -> CreatedUser:
        self.sent.append(kwargs)
        self.users.add(registration)
        return CreatedUser(device_user_id=9, registration=registration)


def _client(app):
    app.config["SERVER_NAME"] = f"127.0.0.1:{config.PORT}"
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    return client


def _app(fleet: FakeFleet):
    routes.fleet_for_pool = lambda _pool: fleet
    expiry.fleet_for_pool = lambda _pool: fleet
    main.start_expiry_worker = lambda: None
    main.start_sync_worker = lambda: None
    routes.request_sync = lambda: None
    return main.create_app()


def _snap(expires: datetime, updated: datetime, set_at: datetime | None) -> sync.MemberSnapshot:
    return sync.MemberSnapshot(
        name="Ana",
        registration="M001",
        device_user_id=1,
        photo_sha256=None,
        photo_filename=None,
        medical_exam_expires_at=expires,
        access_blocked=False,
        created_at=updated,
        updated_at=updated,
        expiry_set_at=set_at,
    )


def test_merge_keeps_shorter_date_set_by_adm() -> None:
    now = datetime.now().replace(microsecond=0)
    old_long = _snap(now + timedelta(days=30), now - timedelta(hours=2), None)
    manual = _snap(now + timedelta(days=2), now, now)
    merged, _ = sync.merge_snapshots(old_long, manual)
    assert merged.medical_exam_expires_at == manual.medical_exam_expires_at, "data do ADM vence, mesmo mais curta"
    assert merged.expiry_set_at == now

    renewed_later = _snap(now + timedelta(days=30), now + timedelta(minutes=5), now + timedelta(minutes=5))
    merged, _ = sync.merge_snapshots(manual, renewed_later)
    assert merged.medical_exam_expires_at == renewed_later.medical_exam_expires_at, "OK exame depois vence"

    legacy_changed_after = _snap(now + timedelta(days=30), now + timedelta(minutes=5), None)
    merged, _ = sync.merge_snapshots(manual, legacy_changed_after)
    assert merged.medical_exam_expires_at == legacy_changed_after.medical_exam_expires_at, (
        "PC sem a coluna que mudou depois: volta a valer a mais longa"
    )

    payload = sync.member_payload(db.create_member(
        name="Payload", registration="M999", device_user_id=1, photo_path=None,
    ))
    assert "expiry_set_at" in payload
    assert sync._snapshot_from_payload({**payload, "expiry_set_at": now.isoformat()}).expiry_set_at == now


def test_settings_write_keeps_comments_and_secrets(tmp: Path) -> None:
    env = tmp / ".env"
    env.write_text(
        "# comentário do instalador\nDEVICE_URL=http://192.168.0.86\nDEVICE_PASSWORD=antiga\nOUTRA=fica\n",
        encoding="utf-8",
    )
    app_settings.env_path = lambda: env
    for key in ("DEVICE_URL", "DEVICE_URL_2", "DEVICE_LOGIN", "SYNC_PEER_URL"):
        os.environ.pop(key, None)
    os.environ["DEVICE_PASSWORD"] = "antiga"

    try:
        app_settings.validate({"DEVICE_URL": "http://192.168.0.10", "DEVICE_LOGIN": "admin", "SERVER_LAN_IP": "192.168.0.20"})
    except app_settings.SettingsError as exc:
        assert ".86" in str(exc)
    else:
        raise AssertionError("facial da piscina normal precisa terminar em .86")

    changed = app_settings.save({
        "DEVICE_URL": "192.168.0.86",
        "DEVICE_URL_2": "http://192.168.0.44/fcgi/do?id=1",
        "DEVICE_LOGIN": "admin",
        "DEVICE_PASSWORD": "",
        "SERVER_LAN_IP": "192.168.0.20",
        "SYNC_PEER_URL": "192.168.0.21",
        "SYNC_TOKEN": "",
        "SYNC_INTERVAL_SECONDS": "20",
    })
    text = env.read_text(encoding="utf-8")
    assert "# comentário do instalador" in text and "OUTRA=fica" in text
    assert "DEVICE_PASSWORD=antiga" in text, "senha em branco mantém a atual"
    assert "DEVICE_URL_2=http://192.168.0.44\n" in text, "guarda só a origem do facial"
    assert f"SYNC_PEER_URL=http://192.168.0.21:{config.PORT}" in text, "porta padrão no outro PC"
    assert "Facial da piscina térmica" in changed
    assert os.environ["DEVICE_URL_2"] == "http://192.168.0.44", "vale sem reiniciar"
    assert config.configured_device_urls() == ["http://192.168.0.86", "http://192.168.0.44"]


def test_config_route_is_local_only(tmp: Path) -> None:
    app = _app(FakeFleet())
    client = _client(app)
    page = client.get("/adm").get_data(as_text=True)
    assert 'data-tab="config"' in page and 'id="config-form"' in page
    assert "antiga" not in page, "senha não vai para a tela"

    form = {
        "DEVICE_URL": "http://192.168.0.86",
        "DEVICE_LOGIN": "admin",
        "SERVER_LAN_IP": "192.168.0.20",
        "DEVICE_PASSWORD": "nova-senha",
    }
    assert client.post("/adm/config", json=form).status_code == 403, "sem o header local é recusado"
    response = client.post("/adm/config", json=form, headers=LOCAL)
    assert response.get_json()["ok"], response.get_json()
    assert "DEVICE_PASSWORD=nova-senha" in (tmp / ".env").read_text(encoding="utf-8")
    bad = client.post("/adm/config", json={**form, "SERVER_LAN_IP": "abc"}, headers=LOCAL)
    assert bad.status_code == 400 and "IPv4" in bad.get_json()["error"]


def test_adm_changes_expiry(tmp: Path) -> None:
    fleet = FakeFleet()
    app = _app(fleet)
    client = _client(app)
    member = db.create_member(
        name="Caio", registration="M002", device_user_id=2, photo_path=None,
        expires_at=datetime.now() + timedelta(days=20),
    )
    fleet.users.add("M002")

    soon = (datetime.now() + timedelta(hours=3)).replace(second=0, microsecond=0)
    client.post(f"/adm/membros/{member.id}/validade", data={"expires_at": soon.strftime("%Y-%m-%dT%H:%M")})
    updated = db.get_member(member.id)
    assert updated.medical_exam_expires_at == soon, "data e hora escolhidas no ADM"
    assert updated.expiry_set_at is not None
    assert db.next_member_expiry(datetime.now()) == soon, "checagem acorda nesse horário"
    wait = expiry._seconds_until_next_check(15 * 60, soon - timedelta(minutes=1), soon)
    assert wait == 65, "acorda 5 s depois do vencimento"

    db.init_db()
    assert db.get_member(member.id).medical_exam_expires_at == soon, "hora do ADM não vira a hora padrão"

    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    client.post(f"/adm/membros/{member.id}/validade", data={"expires_at": past})
    blocked = db.get_member(member.id)
    assert blocked.access_blocked and "M002" not in fleet.users, "data passada apaga do facial na hora"
    assert "M002" in db.last_removal_times()

    page = client.get("/adm").get_data(as_text=True)
    assert "Apagado em" in page and "Sai do Intelbras" in page
    assert "Validade alterada" in page, "aparece nas alterações"

    future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
    client.post(f"/adm/membros/{member.id}/validade", data={"expires_at": future})
    reopened = db.get_member(member.id)
    assert not reopened.access_blocked and "M002" in fleet.users, "data futura devolve a face"


def test_ok_exam_resends_saved_photo(tmp: Path) -> None:
    fleet = FakeFleet()
    app = _app(fleet)
    client = _client(app)
    png = config.PHOTOS_DIR / "duda_antiga.png"
    rng = random.Random(1)
    Image.frombytes("RGB", (400, 400), bytes(rng.randrange(256) for _ in range(400 * 400 * 3))).save(png)
    member = db.create_member(
        name="Duda", registration="M003", device_user_id=3, photo_path=str(png),
        expires_at=datetime.now() - timedelta(days=2),
    )
    db.mark_blocked(member.id)

    client.post(f"/membros/{member.id}/renovar")
    renewed = db.get_member(member.id)
    assert not renewed.access_blocked and not renewed.is_expired
    sent = fleet.sent[-1]
    assert sent["image_path"].suffix == ".jpg", "foto antiga em PNG vai convertida"

    lost = db.create_member(
        name="Edu", registration="M004", device_user_id=4, photo_path=str(tmp / "sumiu.jpg"),
        expires_at=datetime.now() - timedelta(days=2),
    )
    db.mark_blocked(lost.id)
    before = len(fleet.sent)
    client.post(f"/membros/{lost.id}/renovar")
    assert db.get_member(lost.id).access_blocked, "sem foto não renova"
    assert len(fleet.sent) == before, "nem chama o facial"


def main_() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config.PHOTOS_DIR = root / "photos"
        config.PHOTOS_DIR.mkdir()
        routes.PHOTOS_DIR = config.PHOTOS_DIR
        config.DB_PATH = root / "teste.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        test_merge_keeps_shorter_date_set_by_adm()
        test_settings_write_keeps_comments_and_secrets(root)
        test_config_route_is_local_only(root)
        test_adm_changes_expiry(root)
        test_ok_exam_resends_saved_photo(root)
    print("settings and expiry edit ok")


if __name__ == "__main__":
    main_()
