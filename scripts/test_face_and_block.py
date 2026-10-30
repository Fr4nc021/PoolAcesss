"""Testa cadastro pelo painel, confirmação do rosto e exclusão contra um facial de mentira.

O facial de mentira imita o que o export real mostra: quem é reconhecido
tem a pasta faceIDs/<FaceID>-<nome>/ com o modelo do rosto. Uma foto que
não abre fica com FaceID mas sem modelo, que é o caso "usuário desconhecido".
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from PIL import Image

import app.config as config

config.reload_env = lambda: None  # noqa: E731
os.environ["SYNC_PEER_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""

import app.db as db
import app.expiry as expiry
import app.intelbras_client as ic

time.sleep = lambda _seconds: None  # noqa: E731 — sem esperas no teste


def _post_decode(text: str) -> str:
    """O inverso do PostEncode, como o facial lê o texto."""
    codes = {"A": "`", "B": "&", "C": "=", "D": " ", "E": "\r\n", "F": "'", "G": "%", "H": "/", "I": "$", "J": "#", "K": "+"}
    return re.sub(r"`([A-K])", lambda m: codes[m.group(1)], text)


class FakeDevice:
    """Facial XPE de mentira com /api/user/* e export."""

    def __init__(self, url: str = "http://facial-a") -> None:
        self.url = url
        self.users: dict[str, dict] = {}  # ID -> usuário
        self.templates: dict[str, bytes] = {}  # FaceID -> modelo
        self.next_id = 1
        self.next_face = 100
        self.delete_by_userid_works = True
        self.delete_works = True
        self.online = True
        self.served_photos: dict[str, bytes] = {}
        self.panel_password = ""
        self.faces_ok = True  # False: o facial guarda a foto mas não gera o modelo do rosto
        self.panel_adds: list[list[str]] = []
        self.sessions: list[str] = []
        # True: abre a sessão só sem usuário e senha no SubmitData (como parece a térmica).
        self.session_without_login = False
        # True: manda o SessionId no Set-Cookie, sem o campo hcSessionIdNow na página.
        self.session_in_header = False

    def _by_userid(self, user_id: str):
        return next((u for u in self.users.values() if u["UserID"] == user_id), None)

    def _enroll(self, user: dict, jpeg: bytes) -> None:
        user["FaceID"] = str(self.next_face)
        self.next_face += 1
        try:
            with Image.open(io.BytesIO(jpeg)) as img:
                img.load()
        except Exception:  # noqa: BLE001 — foto ruim: fica FaceID sem modelo
            return
        if not self.faces_ok:
            return
        self.templates[user["FaceID"]] = b"modelo:" + jpeg[-64:]

    def _panel(self, request: httpx.Request) -> httpx.Response:
        """O /fcgi do painel: login por cookie e usuário + foto num POST multipart."""
        import re
        from urllib.parse import unquote_plus

        params = request.url.params
        cookie = request.headers.get("cookie", "")
        logged = bool(self.sessions) and any(f"SessionId={sid}" in cookie for sid in self.sessions)
        if params.get("action") == "Encrypt":
            ok = params.get("UserName") == "admin" and params.get("Password") == self.panel_password
            value = f"enc-{self.panel_password}" if ok else "-1"
            return httpx.Response(200, text=f"<form name='hiddenValForm_Div'> <input id=hcSingleResult type=hidden value='{value}'> </form>")
        if params.get_list("id") == ["1"]:
            # Como o facial: a página de Status com o número da sessão escondido,
            # ou a tela de login de novo (com o hcLoginStatus) se a senha não bate.
            form = dict(part.split("=", 1) for part in request.read().decode().split("&") if "=" in part)
            submit = unquote_plus(form.get("SubmitData", ""))
            with_login = f"Password=enc-{self.panel_password}" in submit and "UserName=admin" in submit
            if with_login if not self.session_without_login else "UserName=" not in submit:
                sid = f"S{len(self.sessions) + 1:04d}"
                self.sessions.append(sid)
                if self.session_in_header:
                    return httpx.Response(
                        200,
                        headers={"Set-Cookie": f"SessionId={sid}; path=/"},
                        text="<html><input id=hcLoginStatus type=hidden value='-1'></html>",
                    )
                return httpx.Response(
                    200,
                    text=(
                        "<html><input id=hcLoginStatus type=hidden value='-1'>"
                        f"<form name='hiddenValForm'><input id=\"hcSessionIdNow\" type=\"hidden\" value=\"{sid}\"></form></html>"
                    ),
                )
            return httpx.Response(200, text="<html><input id=hcLoginStatus type=hidden value='-1'></html>")
        if params.get_list("id") == ["16", "5"]:
            assert logged, "cadastro sem a sessão do painel"
            body = request.read()
            upload = re.search(rb'name="uploadType"\r\n\r\n(.*?)\r\n--', body, re.S).group(1).decode()
            jpeg = re.search(
                rb'name="importUserFacePhoto"; filename="[^"]*"\r\nContent-Type: image/jpeg\r\n\r\n(.*?)\r\n--',
                body,
                re.S,
            ).group(1)
            fields = re.search(r"cUserAdd=(.*)/&$", upload).group(1).split("/")
            self.panel_adds.append(fields)
            uid = str(self.next_id)
            self.next_id += 1
            user = {"ID": uid, "UserID": _post_decode(fields[0]), "Name": _post_decode(fields[1]), "FaceID": "0"}
            self.users[uid] = user
            self._enroll(user, jpeg)
            return httpx.Response(200, text="<html>ok</html>")
        return httpx.Response(404)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("desligado", request=request)
        path = request.url.path
        if path == "/fcgi/do":
            return self._panel(request)
        if path == "/api/system/info":
            return httpx.Response(200, json={"retcode": 0, "data": {}})
        if path == "/api/user/get":
            return httpx.Response(200, json={"retcode": 0, "data": {"item": list(self.users.values())}})
        if path == "/api/user/add":
            item = json.loads(request.content)["data"]["item"][0]
            uid = str(self.next_id)
            self.next_id += 1
            self.users[uid] = {"ID": uid, "UserID": item["UserID"], "Name": item["Name"], "FaceID": "0"}
            return httpx.Response(200, json={"retcode": 0})
        if path == "/api/user/set":
            if not request.headers.get("content-type", "").startswith("application/json"):
                return httpx.Response(200, json={"retcode": -1, "message": "error param"})
            item = json.loads(request.content)["data"]["item"][0]
            user = self.users.get(str(item.get("ID")))
            face_url = item.get("FaceUrl")
            if user and face_url:
                if face_url.startswith("http://"):
                    jpeg = self.served_photos.get(face_url, b"")
                elif face_url.startswith("data:"):
                    return httpx.Response(200, json={"retcode": -1, "message": "unsupport"})
                else:
                    import base64

                    jpeg = base64.b64decode(face_url)
                self._enroll(user, jpeg)
            return httpx.Response(200, json={"retcode": 0})
        if path == "/api/user/del":
            item = json.loads(request.content)["data"]["item"][0]
            if not self.delete_works:
                return httpx.Response(200, json={"retcode": 0, "message": "OK"})
            if "ID" in item:
                self.users.pop(str(item["ID"]), None)
            elif self.delete_by_userid_works:
                user = self._by_userid(item["UserID"])
                if user:
                    self.users.pop(user["ID"], None)
            return httpx.Response(200, json={"retcode": 0, "message": "OK"})
        if path == "/api/user/export":
            return httpx.Response(200, content=self._export())
        if path.startswith("/Image/RegisterImg/"):
            return httpx.Response(404)
        return httpx.Response(404)

    def _export(self) -> bytes:
        root = ET.Element("UserData")
        for user in self.users.values():
            ET.SubElement(root, "Key", {"ID": user["ID"], "FaceID": user["FaceID"], "UserID": user["UserID"], "Name": user["Name"]})
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            def add(name: str, data: bytes) -> None:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(datetime.now().timestamp())  # igual ao real: hora do export
                tar.addfile(info, io.BytesIO(data))

            add("faceIDs/userdata.xml", ET.tostring(root))
            for user in self.users.values():
                model = self.templates.get(user["FaceID"])
                if model:
                    for part in range(2):
                        add(f"faceIDs/{user['FaceID']}-{user['Name']}/42-part{part}", model)
        return out.getvalue()


class FakeClient(ic.IntelbrasClient):
    def __init__(self, device: FakeDevice) -> None:
        super().__init__(base_url=device.url, login="admin", password="")
        self.device = device

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.device.handler), auth=("admin", ""))

    def _panel_http(self) -> httpx.Client:
        # Facial de mentira sem /fcgi responde 404: o cadastro segue pela API.
        return httpx.Client(transport=httpx.MockTransport(self.device.handler), follow_redirects=True)


def _photo(path: Path, *, size=(480, 640), fmt="JPEG", exif_rotate: bool = False) -> Path:
    img = Image.new("RGB", size, (190, 160, 140))
    for x in range(0, size[0], 7):  # textura para o JPEG não ficar minúsculo
        img.putpixel((x, x % size[1]), (10, 20, 30))
    kwargs = {}
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6
        kwargs["exif"] = exif
    img.save(path, format=fmt, **kwargs)
    return path


def test_registration_needs_face_model(tmp: Path) -> None:
    device = FakeDevice()
    client = FakeClient(device)
    photo = ic.prepare_face_photo(_photo(tmp / "ana.jpg"))

    # O facial guarda a foto mas não gera o modelo do rosto: na porta, "desconhecido".
    device.faces_ok = False
    try:
        client.register_member("Ana", photo, registration="R1", image_ready=True)
    except ic.DeviceError as exc:
        assert str(exc) == ic.FACE_NOT_ACCEPTED_MSG, exc
    else:
        raise AssertionError("foto sem modelo do rosto não pode virar cadastro ok")
    assert device._by_userid("R1") is None, "cadastro incompleto sai do facial"

    device.faces_ok = True
    created = client.register_member("Ana", photo, registration="R1", image_ready=True)
    assert created.face_ok
    user = device._by_userid("R1")
    assert user and user["FaceID"] in device.templates, "modelo do rosto gravado"
    assert device.panel_adds[-1][:2] == ["R1", "Ana"], "cadastrado pelo painel"


def test_new_photo_must_make_a_model(tmp: Path) -> None:
    device = FakeDevice()
    client = FakeClient(device)
    first = ic.prepare_face_photo(_photo(tmp / "bia1.jpg"))
    client.register_member("Bia", first, registration="R2", image_ready=True)

    # Foto nova que o facial não aceita: a troca falha e não fica cadastro sem rosto.
    second = ic.prepare_face_photo(_photo(tmp / "bia2.jpg", size=(500, 600)))
    device.faces_ok = False
    try:
        client.update_member_face(name="Bia", registration="R2", image_path=second)
    except ic.DeviceError:
        pass
    else:
        raise AssertionError("foto nova sem modelo do rosto não pode virar troca ok")
    assert device._by_userid("R2") is None


def test_renew_keeps_existing_model(tmp: Path) -> None:
    device = FakeDevice()
    client = FakeClient(device)
    photo = ic.prepare_face_photo(_photo(tmp / "caio.jpg"))
    client.register_member("Caio", photo, registration="R3", image_ready=True)
    face_before = device._by_userid("R3")["FaceID"]
    adds = len(device.panel_adds)
    client.renew_member(name="Caio", registration="R3", image_path=photo)
    assert device._by_userid("R3")["FaceID"] == face_before, "renovação não reenvia rosto que já vale"
    assert len(device.panel_adds) == adds


def test_delete_is_verified() -> None:
    device = FakeDevice()
    client = FakeClient(device)
    device.users["9"] = {"ID": "9", "UserID": "R9", "Name": "Duda", "FaceID": "5"}

    # Responde OK para UserID mas não apaga: tem que tentar pelo ID e conferir.
    device.delete_by_userid_works = False
    client.block_user(device_user_id=123, registration="R9")
    assert device._by_userid("R9") is None, "bloqueio apagou do facial"

    device.users["10"] = {"ID": "10", "UserID": "R10", "Name": "Edu", "FaceID": "6"}
    device.delete_works = False
    try:
        client.block_user(device_user_id=10, registration="R10")
    except ic.DeviceError as exc:
        assert "não apagou" in str(exc), exc
    else:
        raise AssertionError("facial que diz OK sem apagar precisa dar erro")

    client.block_user(device_user_id=0, registration="NAO-EXISTE")  # já fora: não é erro


def test_expiry_blocks_reachable_devices(tmp: Path) -> None:
    config.DB_PATH = tmp / "teste.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()
    a, b = FakeDevice("http://facial-a"), FakeDevice("http://facial-b")
    for device in (a, b):
        device.users["1"] = {"ID": "1", "UserID": "V1", "Name": "Fabi", "FaceID": "7"}
    member = db.create_member(
        name="Fabi",
        registration="V1",
        device_user_id=None,  # sem ID local também precisa ser bloqueado
        photo_path=None,
        expires_at=datetime.now() - timedelta(minutes=1),
    )
    fleet = ic.DeviceFleet([FakeClient(a), FakeClient(b)])
    expiry.fleet_for_pool = lambda _pool: fleet

    b.online = False
    assert expiry.sync_expired_members() == 0
    assert a._by_userid("V1") is None, "facial ligado já bloqueou"
    assert not db.get_member(member.id).access_blocked, "falta o facial desligado"

    b.online = True
    assert expiry.sync_expired_members() == 1
    assert b._by_userid("V1") is None
    assert db.get_member(member.id).access_blocked


def test_photo_file_is_stable(tmp: Path) -> None:
    ready = ic.prepare_face_photo(_photo(tmp / "pronta.jpg"))
    before = (ready.read_bytes(), ready.stat().st_mtime_ns)
    for _ in range(3):
        assert ic._ensure_jpeg_file(ready) == ready
    assert (ready.read_bytes(), ready.stat().st_mtime_ns) == before, "foto pronta não é regravada"

    png = _photo(tmp / "enviada.png", fmt="PNG")
    jpeg = ic.prepare_face_photo(png)
    assert jpeg.suffix == ".jpg" and not png.exists(), "FaceUrl aponta para o JPEG, não o PNG"
    with Image.open(jpeg) as img:
        assert img.format == "JPEG"

    rotated = ic.prepare_face_photo(_photo(tmp / "celular.jpg", size=(640, 480), exif_rotate=True))
    with Image.open(rotated) as img:
        assert img.size == (480, 640), "foto de celular deitada fica em pé"
        assert img.getexif().get(0x0112, 1) == 1

    broken = tmp / "quebrada.jpg"
    broken.write_bytes(ready.read_bytes()[:800])
    try:
        ic.prepare_face_photo(broken)
    except ic.DeviceError:
        pass
    else:
        raise AssertionError("foto cortada é recusada no cadastro")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_registration_needs_face_model(root)
        test_new_photo_must_make_a_model(root)
        test_renew_keeps_existing_model(root)
        test_delete_is_verified()
        test_expiry_blocks_reachable_devices(root)
        test_photo_file_is_stable(root)
    print("face and block ok")


if __name__ == "__main__":
    main()
