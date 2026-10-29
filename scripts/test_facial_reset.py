"""Testa o "Refazer faciais do zero" contra faciais de mentira.

1. Facial que funciona: sócios voltam com a foto do sistema, funcionário que
   só existe no facial volta com a foto do facial, registro antigo de sócio e
   vencido saem, quem não tem foto em lugar nenhum não é mexido.
2. Facial que repete o número do último rosto: para no primeiro repetido,
   refaz quem perdeu o rosto e não começa o próximo facial.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_face_id_conflict import PhotoDevice  # noqa: E402 — monta o ambiente de teste
from test_face_and_block import FakeClient, _photo  # noqa: E402

import app.config as config
import app.db as db
import app.facial_reset as reset
import app.intelbras_client as ic


class StuckDevice(PhotoDevice):
    """Dá a todo rosto novo o número do último rosto dado."""

    def _enroll(self, user: dict, jpeg: bytes) -> None:
        if getattr(self, "last_face", None) is None:
            self.last_face = str(self.next_face)
        user["FaceID"] = self.last_face
        self.templates[self.last_face] = b"modelo:" + jpeg[-64:]
        self.photos[self.last_face] = jpeg


def _setup(tmp: Path, name: str, devices: list[PhotoDevice]) -> None:
    config.DB_PATH = tmp / f"{name}.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()
    clients = {device.url: FakeClient(device) for device in devices}
    pools = [
        {"id": pool_id, "label": label, "url": device.url}
        for (pool_id, label), device in zip((("normal", "Piscina normal"), ("termica", "Piscina térmica")), devices)
    ]
    config.registration_pools = lambda: pools  # noqa: E731
    reset.registration_pools = lambda: pools  # noqa: E731
    reset.IntelbrasClient = lambda base_url, **_kw: clients[base_url]  # noqa: E731
    reset.public_media_url = lambda _name: ""  # noqa: E731 — vai em base64 no facial de mentira
    reset.lan_ip_problem = lambda: None  # noqa: E731
    reset.PHOTOS_DIR = tmp / f"{name}_fotos"
    reset.DATA_DIR = tmp / f"{name}_dados"


def _run() -> dict:
    ok, message = reset.start()
    assert ok, message
    for thread in threading.enumerate():
        if thread.name == "facial-reset":
            thread.join(timeout=60)
    state = reset.status()
    assert not state["running"], state
    return state


def _member(name: str, reg: str, photo, pool: str, expires=None) -> db.Member:
    return db.create_member(
        name=name, registration=reg, device_user_id=None,
        photo_path=str(photo) if photo else None,
        expires_at=expires or datetime.now() + timedelta(days=10), pool=pool,
    )


def test_reset_rebuilds_the_facial(tmp: Path) -> None:
    normal, termica = PhotoDevice("http://normal"), PhotoDevice("http://termica")
    for device in (normal, termica):
        device.next_id = 100
        device.next_face = 1
    _setup(tmp, "ok", [normal, termica])

    paula = ic.prepare_face_photo(_photo(tmp / "paula.jpg", size=(480, 640)))
    jose = ic.prepare_face_photo(_photo(tmp / "jose.jpg", size=(500, 650)))
    jorge_photo = ic.prepare_face_photo(_photo(tmp / "jorge.jpg", size=(520, 660))).read_bytes()
    winston_photo = ic.prepare_face_photo(_photo(tmp / "winston.jpg", size=(530, 670))).read_bytes()
    _member("Paula", "AP1", paula, "termica")
    _member("JOSE ANTONIO FRANCIO", "AP2", jose, "termica")
    _member("Winston", "AP3", None, "termica")  # sem foto no PC: usa a do facial
    _member("Sem Foto", "AP4", None, "termica")  # sem foto em lugar nenhum
    _member("Vencido", "AP5", jose, "termica", expires=datetime.now() - timedelta(days=1))

    users = {
        "1": ("AP1", "Paula", "374", b"x"),
        "2": ("00564940097", "JOSÉ ANTONIO FRANCIO", "4", b"y"),  # registro antigo do sócio AP2
        "3": ("01301689041", "Jorge Luis Berlato", "3", jorge_photo),  # funcionário
        "4": ("AP3", "Winston", "5", winston_photo),
        "5": ("AP5", "Vencido", "6", b"z"),
        "6": ("APFIDTESTE", "TESTE FACEID (apagar)", "0", None),
        "7": ("X9", "Cartao Sem Rosto", "0", None),
    }
    for uid, (reg, name, face, photo) in users.items():
        termica.users[uid] = {"ID": uid, "UserID": reg, "Name": name, "FaceID": face}
        if photo:
            termica.templates[face] = b"modelo"
            termica.photos[face] = photo

    state = _run()
    assert not state["error"], state
    dn, dt = state["devices"]
    assert dn["phase"] == "pronto" and dt["phase"] == "pronto", state
    regs = {u["UserID"] for u in termica.users.values()}
    assert regs == {"AP1", "AP2", "AP3", "01301689041", "X9"}, regs
    faces = [u["FaceID"] for u in termica.users.values() if u["UserID"] != "X9"]
    assert len(set(faces)) == len(faces), f"cada um com seu número: {faces}"
    assert termica.photos[termica._by_userid("01301689041")["FaceID"]] == jorge_photo, "funcionário volta com a foto do facial"
    assert termica.photos[termica._by_userid("AP3")["FaceID"]] == winston_photo, "sem foto no PC usa a do facial"
    assert {u["name"] for u in dt["untouched"]} == {"Sem Foto", "Cartao Sem Rosto"}, dt["untouched"]
    assert set(dt["removed"]) == {"JOSÉ ANTONIO FRANCIO", "Vencido", "TESTE FACEID (apagar)"}, dt["removed"]
    assert Path(dt["backup"]).is_file(), "cópia do facial guardada"
    assert not list(reset.PHOTOS_DIR.glob("refazer_*")), "fotos temporárias apagadas"


def test_reset_stops_on_repeated_face(tmp: Path) -> None:
    normal, termica = StuckDevice("http://normal"), PhotoDevice("http://termica")
    for device in (normal, termica):
        device.next_id = 100
        device.next_face = 1
    _setup(tmp, "preso", [normal, termica])
    for index, name in enumerate(("Daniel", "Helena", "Paula")):
        photo = ic.prepare_face_photo(_photo(tmp / f"{name}.jpg", size=(480 + index * 20, 640)))
        _member(name, f"N{index}", photo, "normal")
    termica.users["1"] = {"ID": "1", "UserID": "T1", "Name": "Lorenza", "FaceID": "9"}
    termica.templates["9"] = b"modelo:lorenza"
    termica.photos["9"] = b"foto:lorenza"

    state = _run()
    dn, dt = state["devices"]
    assert "repetiu" in dn["error"], dn
    assert dn["repaired"] == ["Daniel"], dn
    assert dn["missing"] == ["Helena", "Paula"], dn
    assert normal._by_userid("N0") is not None and normal._by_userid("N1") is None
    assert dt["phase"] == "esperando", "a térmica não é mexida depois do problema na normal"
    assert termica._by_userid("T1")["FaceID"] == "9"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_reset_rebuilds_the_facial(root)
        test_reset_stops_on_repeated_face(root)
    print("facial reset ok")


if __name__ == "__main__":
    main()
