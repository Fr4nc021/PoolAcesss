"""Testa o FaceID repetido e a conferência de fotos contra um facial de mentira.

Caso real: a sócia renovou e o rosto dela ficou no mesmo FaceID de outro
sócio. O facial passou a liberar a entrada dela no nome dele.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from test_face_and_block import FakeClient, FakeDevice, _photo  # noqa: E402 — monta o ambiente de teste

import app.config as config
import app.db as db
import app.face_audit as face_audit
import app.intelbras_client as ic


class PhotoDevice(FakeDevice):
    """Facial de mentira que também guarda a foto de cada FaceID, como o export real."""

    def __init__(self, url: str = "http://facial-a") -> None:
        super().__init__(url)
        self.photos: dict[str, bytes] = {}
        self.export_works = True

    def _enroll(self, user: dict, jpeg: bytes) -> None:
        super()._enroll(user, jpeg)
        self.photos[user["FaceID"]] = jpeg

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/user/export" and not self.export_works:
            return httpx.Response(500)
        return super().handler(request)

    def _export(self) -> bytes:
        import io
        import tarfile

        base = super()._export()
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(base), mode="r:gz") as src, tarfile.open(fileobj=out, mode="w:gz") as dst:
            for item in src.getmembers():
                dst.addfile(item, src.extractfile(item))
            for face_id, data in self.photos.items():
                info = tarfile.TarInfo(f"faceIDs/RegFaceMeun/{face_id}.jpg")
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
        return out.getvalue()


def _serve(device: FakeDevice, photo: Path, name: str) -> str:
    url = f"http://pc/media/{name}"
    device.served_photos[url] = photo.read_bytes()
    return url


def test_conflict_on_register_is_rolled_back(tmp: Path) -> None:
    device = PhotoDevice()
    client = FakeClient(device)
    homem = ic.prepare_face_photo(_photo(tmp / "homem.jpg"))
    client.register_member("Homem", homem, registration="H1", face_public_url=_serve(device, homem, "homem.jpg"), image_ready=True)
    face_homem = device._by_userid("H1")["FaceID"]

    # O facial reaproveita o FaceID do Homem para o rosto da Sócia.
    device.next_face = int(face_homem)
    socia = ic.prepare_face_photo(_photo(tmp / "socia.jpg", size=(500, 640)))
    try:
        client.register_member("Socia", socia, registration="S1", face_public_url=_serve(device, socia, "socia.jpg"), image_ready=True)
    except ic.FaceIDConflict as exc:
        assert "Homem" in str(exc), exc
    else:
        raise AssertionError("rosto no FaceID de outro sócio não pode virar cadastro ok")
    assert device._by_userid("S1") is None, "cadastro com FaceID repetido sai do facial"
    assert device._by_userid("H1")["FaceID"] == face_homem, "o outro sócio fica como estava"


def test_renew_recreates_shared_face(tmp: Path) -> None:
    device = PhotoDevice()
    client = FakeClient(device)
    socia = ic.prepare_face_photo(_photo(tmp / "socia2.jpg", size=(520, 640)))
    url = _serve(device, socia, "socia2.jpg")
    device.users["1"] = {"ID": "1", "UserID": "H2", "Name": "Homem", "FaceID": "50"}
    device.users["2"] = {"ID": "2", "UserID": "S2", "Name": "Socia", "FaceID": "50"}
    device.templates["50"] = b"modelo:homem"
    device.next_id = 10
    assert client.face_shared_with("S2") == ["Homem"]

    device.next_face = 300
    client.renew_member(name="Socia", registration="S2", image_path=socia, face_public_url=url)
    renewed = device._by_userid("S2")
    assert renewed["FaceID"] == "300", "renovação recria quem divide FaceID e ganha um novo"
    assert client.face_shared_with("S2") == []
    assert device._by_userid("H2")["FaceID"] == "50"


def test_next_face_id_needs_export() -> None:
    device = PhotoDevice()
    client = FakeClient(device)
    # A lista da API veio cortada: só o export mostra o FaceID 80 em uso.
    device.users["1"] = {"ID": "1", "UserID": "A", "Name": "A", "FaceID": "80"}
    original = device.handler

    def short_list(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/user/get":
            return httpx.Response(200, json={"retcode": 0, "data": {"item": []}})
        return original(request)

    device.handler = short_list  # type: ignore[method-assign]
    assert client._next_face_id() == 81, "FaceID livre vem do export, não da lista cortada"
    device.export_works = False
    try:
        client._next_face_id()
    except ic.DeviceError:
        pass
    else:
        raise AssertionError("sem export não escolhe FaceID")


def test_audit_finds_each_problem(tmp: Path) -> None:
    config.DB_PATH = tmp / "auditoria.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()
    device = PhotoDevice("http://facial-t")
    client = FakeClient(device)
    pools = [{"id": "termica", "label": "Piscina térmica", "url": device.url}]
    config.registration_pools = lambda: pools  # noqa: E731
    face_audit.registration_pools = lambda: pools  # noqa: E731
    face_audit.IntelbrasClient = lambda base_url: client  # noqa: E731

    later = datetime.now() + timedelta(days=10)

    def member(name: str, reg: str, photo: Path, **kw) -> db.Member:
        return db.create_member(name=name, registration=reg, device_user_id=None, photo_path=str(photo), expires_at=kw.get("expires", later), pool="termica")

    ok_photo = ic.prepare_face_photo(_photo(tmp / "ok.jpg"))
    client.register_member("Certo", ok_photo, registration="OK1", face_public_url=_serve(device, ok_photo, "ok.jpg"), image_ready=True)
    member("Certo", "OK1", ok_photo)

    member("Faltando", "F1", ok_photo)

    device.users["10"] = {"ID": "10", "UserID": "H3", "Name": "Homem", "FaceID": "70"}
    device.users["11"] = {"ID": "11", "UserID": "S3", "Name": "Socia", "FaceID": "70"}
    device.templates["70"] = b"modelo"
    member("Socia", "S3", ok_photo)

    member("Vencido", "V3", ok_photo, expires=datetime.now() - timedelta(days=1))
    device.users["12"] = {"ID": "12", "UserID": "V3", "Name": "Vencido", "FaceID": "0"}

    report = face_audit.run_audit()
    section = report["devices"][0]
    assert section["ok"], section
    rows = {row["registration"]: row for row in section["rows"]}
    assert rows["OK1"]["status"] == "ok", rows["OK1"]
    assert rows["OK1"]["our_photo"] and rows["OK1"]["device_photo"], "mostra as duas fotos"
    assert any("Não está no facial" in p for p in rows["F1"]["problems"])
    assert any("Mesmo FaceID 70 de Homem" in p for p in rows["S3"]["problems"]), rows["S3"]
    assert any("vencido" in p for p in rows["V3"]["problems"]), rows["V3"]
    orphans = {o["registration"]: o for o in section["orphans"]}
    assert orphans["H3"]["shared_with"] == ["Socia"], "o outro sócio aparece como sobrando no facial"
    assert section["rows"][0]["status"] == "erro", "problemas vêm primeiro"


def test_photo_difference() -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    def picture(flip: bool) -> bytes:
        img = Image.new("RGB", (300, 400), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        if flip:
            draw.rectangle((0, 200, 300, 400), fill=(20, 20, 20))
            draw.ellipse((200, 20, 290, 120), fill=(250, 250, 250))
        else:
            draw.rectangle((0, 0, 150, 400), fill=(20, 20, 20))
            draw.ellipse((40, 250, 120, 390), fill=(90, 90, 90))
        out = BytesIO()
        img.save(out, format="JPEG", quality=92)
        return out.getvalue()

    a = picture(False)
    smaller = BytesIO()
    with Image.open(BytesIO(a)) as img:
        img.resize((150, 200)).save(smaller, format="JPEG", quality=60)
    assert face_audit.photo_difference(a, smaller.getvalue()) < face_audit.PHOTO_DIFF_WARN
    assert face_audit.photo_difference(a, picture(True)) >= face_audit.PHOTO_DIFF_WARN
    assert face_audit.photo_difference(a, None) is None


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_conflict_on_register_is_rolled_back(root)
        test_renew_recreates_shared_face(root)
        test_next_face_id_needs_export()
        test_audit_finds_each_problem(root)
        test_photo_difference()
    print("face id conflict ok")


if __name__ == "__main__":
    main()
