"""Testa o cadastro pelo /fcgi do painel contra um facial como os do clube.

O facial de mentira faz o que os reais fizeram: pela API (user/add + user/set
com FaceUrl) todo rosto novo cai no FaceID do último; pelo /fcgi do painel,
com login por cookie e usuário + foto num POST multipart, cada um recebe o seu.
"""

from __future__ import annotations

import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from test_face_id_conflict import PhotoDevice  # noqa: E402 — monta o ambiente de teste
from test_face_and_block import FakeClient, _photo  # noqa: E402

import app.config as config
import app.db as db
import app.facial_reset as reset
import app.intelbras_client as ic

PASSWORD = "segredo"


class PanelDevice(PhotoDevice):
    def __init__(self, url: str = "http://facial-p") -> None:
        super().__init__(url)
        self.api_faces: list[str] = []
        self.panel_adds: list[dict] = []
        self.sessions = 0

    def _enroll(self, user: dict, jpeg: bytes) -> None:
        # Pela API: o número não avança, repete o do último rosto.
        if self.api_faces or getattr(self, "last_face", None):
            face = getattr(self, "last_face", None) or self.api_faces[-1]
        else:
            face = str(self.next_face)
        self.api_faces.append(face)
        user["FaceID"] = face
        self.templates[face] = b"modelo:" + jpeg[-64:]
        self.photos[face] = jpeg

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/fcgi/do" and params.get("action") == "Encrypt":
            ok = params.get("UserName") == "admin" and params.get("Password") == PASSWORD
            value = f"enc-{PASSWORD}" if ok else "-1"
            return httpx.Response(200, text=f'<input type="hidden" id="hcSingleResult" value="{value}">')
        if path == "/fcgi/do" and params.get_list("id") == ["1"]:
            cookie = request.headers.get("cookie", "")
            ok = f"Password=enc-{PASSWORD}" in cookie and "UserName=admin" in cookie
            self.sessions += ok
            return httpx.Response(200, text=f'<input id="hcLoginStatus" value="{1 if ok else -1}">')
        if path == "/fcgi/do" and params.get_list("id") == ["16", "5"]:
            assert f"Password=enc-{PASSWORD}" in request.headers.get("cookie", ""), "pedido sem sessão do painel"
            body = request.read()
            upload = re.search(rb'name="uploadType"\r\n\r\n(.*?)\r\n--', body, re.S).group(1).decode()
            jpeg = re.search(rb'name="importUserFacePhoto"; filename="[^"]*"\r\nContent-Type: image/jpeg\r\n\r\n(.*?)\r\n--', body, re.S).group(1)
            fields = re.search(r"cUserAdd=(.*)/&$", upload).group(1).split("/")
            user_id, name = unquote(fields[0]), unquote(fields[1])
            self.panel_adds.append({"fields": fields})
            uid = str(self.next_id)
            self.next_id += 1
            face = str(self.next_face)
            self.next_face += 1
            self.users[uid] = {"ID": uid, "UserID": user_id, "Name": name, "FaceID": face}
            self.templates[face] = b"modelo:" + jpeg[-64:]
            self.photos[face] = jpeg
            return httpx.Response(200, text="<html>ok</html>")
        return super().handler(request)


class PanelClient(FakeClient):
    def __init__(self, device: PanelDevice) -> None:
        super().__init__(device)
        self.password = PASSWORD


def _device() -> PanelDevice:
    device = PanelDevice()
    device.next_id = 100
    device.next_face = 1
    return device


def test_panel_gives_each_face_its_own_number(tmp: Path) -> None:
    device = _device()
    client = PanelClient(device)
    first = ic.prepare_face_photo(_photo(tmp / "um.jpg", size=(480, 640)))
    second = ic.prepare_face_photo(_photo(tmp / "dois.jpg", size=(500, 660)))
    a = client.create_user_with_face("José Antônio / Filho", first, registration="AP00000001")
    b = client.create_user_with_face("Daniel", second, registration="AP00000002")
    assert a.face_ok and b.face_ok
    face_a = device._by_userid("AP00000001")["FaceID"]
    face_b = device._by_userid("AP00000002")["FaceID"]
    assert face_a != face_b, "cada rosto com seu número"
    assert device.api_faces == [], "não usou o caminho da API"
    assert device._by_userid("AP00000001")["Name"] == "José Antônio / Filho", "nome com acento e barra chega certo"
    fields = device.panel_adds[0]["fields"]
    assert fields[2:9] == ["", "0", "", "1", "", "0", "0"], f"mesmos campos do painel: {fields}"


def test_wrong_password_is_an_error(tmp: Path) -> None:
    device = _device()
    client = PanelClient(device)
    client.password = "errada"
    photo = ic.prepare_face_photo(_photo(tmp / "senha.jpg"))
    try:
        client.create_user_with_face("Ana", photo, registration="AP00000003")
    except ic.DeviceError as exc:
        assert "senha" in str(exc), exc
    else:
        raise AssertionError("senha errada no painel não pode virar cadastro")
    assert device._by_userid("AP00000003") is None


def test_long_registration_falls_back_to_api(tmp: Path) -> None:
    device = _device()
    client = PanelClient(device)
    photo = ic.prepare_face_photo(_photo(tmp / "longa.jpg"))
    client.create_user_with_face("Longa", photo, registration="AP1234567890")
    assert device.api_faces, "matrícula de 12 caracteres não cabe no painel: vai pela API"


def test_photo_change_recreates_through_panel(tmp: Path) -> None:
    device = _device()
    client = PanelClient(device)
    old = ic.prepare_face_photo(_photo(tmp / "antiga.jpg", size=(480, 640)))
    new = ic.prepare_face_photo(_photo(tmp / "nova.jpg", size=(520, 680)))
    client.create_user_with_face("Bia", old, registration="AP00000004")
    before = device._by_userid("AP00000004")["FaceID"]
    client.update_member_face(name="Bia", registration="AP00000004", image_path=new)
    after = device._by_userid("AP00000004")
    assert after["FaceID"] != before and device.photos[after["FaceID"]] == new.read_bytes()
    assert device.api_faces == [], "foto nova também pelo painel"


def test_reset_completes_through_panel(tmp: Path) -> None:
    config.DB_PATH = tmp / "painel.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()
    device = _device()
    client = PanelClient(device)
    pools = [{"id": "normal", "label": "Piscina normal", "url": device.url}]
    config.registration_pools = lambda: pools  # noqa: E731
    reset.registration_pools = lambda: pools  # noqa: E731
    reset.IntelbrasClient = lambda base_url, **_kw: client  # noqa: E731
    reset.public_media_url = lambda _name: ""  # noqa: E731
    reset.lan_ip_problem = lambda: None  # noqa: E731
    reset.PHOTOS_DIR = tmp / "painel_fotos"
    reset.DATA_DIR = tmp / "painel_dados"
    for index, name in enumerate(("Jose", "Daniel", "Helena", "Paula")):
        photo = ic.prepare_face_photo(_photo(tmp / f"r{index}.jpg", size=(480 + index * 20, 640)))
        db.create_member(
            name=name, registration=f"AP1000000{index}", device_user_id=None, photo_path=str(photo),
            expires_at=datetime.now() + timedelta(days=5), pool="normal",
        )
    ok, message = reset.start()
    assert ok, message
    for thread in threading.enumerate():
        if thread.name == "facial-reset":
            thread.join(timeout=60)
    device_state = reset.status()["devices"][0]
    assert device_state["phase"] == "pronto" and not device_state.get("error"), device_state
    faces = [u["FaceID"] for u in device.users.values()]
    assert len(faces) == 4 and len(set(faces)) == 4, f"os quatro com números diferentes: {faces}"


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        test_panel_gives_each_face_its_own_number(root)
        test_wrong_password_is_an_error(root)
        test_long_registration_falls_back_to_api(root)
        test_photo_change_recreates_through_panel(root)
        test_reset_completes_through_panel(root)
    print("panel registration ok")


if __name__ == "__main__":
    main()
