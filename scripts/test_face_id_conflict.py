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
        response = super().handler(request)
        if request.url.path == "/api/user/set" and request.headers.get("content-type", "").startswith("application/json"):
            import json

            item = json.loads(request.content)["data"]["item"][0]
            user = self.users.get(str(item.get("ID")))
            if user and item.get("Name"):
                user["Name"] = item["Name"]
        return response

    def _export(self) -> bytes:
        import io
        import tarfile

        base = super()._export()
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(base), mode="r:gz") as src, tarfile.open(fileobj=out, mode="w:gz") as dst:
            for item in src.getmembers():
                dst.addfile(item, src.extractfile(item))
            for face_id, data in self.photos.items():
                # Como o facial real: o arquivo conta a partir de 0.
                info = tarfile.TarInfo(f"faceIDs/RegFaceMeun/{int(face_id) - 1}.jpg")
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
        return out.getvalue()


def test_conflict_on_register_is_rolled_back(tmp: Path) -> None:
    device = PhotoDevice()
    client = FakeClient(device)
    homem = ic.prepare_face_photo(_photo(tmp / "homem.jpg"))
    client.register_member("Homem", homem, registration="H1", image_ready=True)
    face_homem = device._by_userid("H1")["FaceID"]

    # O facial reaproveita o FaceID do Homem para o rosto da Sócia.
    device.next_face = int(face_homem)
    socia = ic.prepare_face_photo(_photo(tmp / "socia.jpg", size=(500, 640)))
    try:
        client.register_member("Socia", socia, registration="S1", image_ready=True)
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
    device.users["1"] = {"ID": "1", "UserID": "H2", "Name": "Homem", "FaceID": "50"}
    device.users["2"] = {"ID": "2", "UserID": "S2", "Name": "Socia", "FaceID": "50"}
    device.templates["50"] = b"modelo:homem"
    device.next_id = 10
    assert client.face_shared_with("S2") == ["Homem"]

    device.next_face = 300
    client.renew_member(name="Socia", registration="S2", image_path=socia)
    renewed = device._by_userid("S2")
    assert renewed["FaceID"] == "300", "renovação recria quem divide FaceID e ganha um novo"
    assert client.face_shared_with("S2") == []
    assert device._by_userid("H2")["FaceID"] == "50"


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
    client.register_member("Certo", ok_photo, registration="OK1", image_ready=True)
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


def _fix_env(tmp: Path, name: str) -> tuple[PhotoDevice, FakeClient]:
    import app.face_fix as face_fix
    import app.routes as routes

    config.DB_PATH = tmp / f"{name}.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()
    device = PhotoDevice("http://facial-t")
    device.next_id = 100
    client = FakeClient(device)
    pools = [{"id": "termica", "label": "Piscina térmica", "url": device.url}]
    config.registration_pools = lambda: pools  # noqa: E731
    for module in (face_audit, face_fix, routes):
        module.registration_pools = lambda: pools  # noqa: E731
        module.IntelbrasClient = lambda base_url, **_kw: client  # noqa: E731
    # Sem FaceUrl: a foto vai em base64, como quando o facial não alcança o PC.
    face_fix.public_media_url = lambda _name: ""  # noqa: E731
    routes.public_media_url = lambda _name: ""  # noqa: E731
    face_fix.lan_ip_problem = lambda: None  # noqa: E731
    routes.lan_ip_problem = lambda: None  # noqa: E731
    return device, client


def test_fix_all_safe(tmp: Path) -> None:
    import threading

    import app.face_fix as face_fix

    device, client = _fix_env(tmp, "corrigir")
    later = datetime.now() + timedelta(days=10)

    def member(name: str, reg: str, photo, expires=later) -> db.Member:
        return db.create_member(
            name=name, registration=reg, device_user_id=None,
            photo_path=str(photo) if photo else None, expires_at=expires, pool="termica",
        )

    homem = ic.prepare_face_photo(_photo(tmp / "fix_homem.jpg", size=(480, 640)))
    socia = ic.prepare_face_photo(_photo(tmp / "fix_socia.jpg", size=(500, 660)))
    member("Homem", "H9", homem)
    member("Socia", "S9", socia)
    device.users["1"] = {"ID": "1", "UserID": "H9", "Name": "Homem", "FaceID": "70"}
    device.users["2"] = {"ID": "2", "UserID": "S9", "Name": "Socia", "FaceID": "70"}
    device.templates["70"] = b"modelo"

    member("Faltando", "F9", homem)
    member("Vencido", "V9", homem, expires=datetime.now() - timedelta(days=1))
    device.users["3"] = {"ID": "3", "UserID": "V9", "Name": "Vencido", "FaceID": "0"}
    member("Nome Certo", "N9", homem)
    device.users["4"] = {"ID": "4", "UserID": "N9", "Name": "Nome Errado", "FaceID": "71"}
    device.templates["71"] = b"modelo71"
    member("Sem Foto", "P9", None)
    device.users["5"] = {"ID": "5", "UserID": "X9", "Name": "Funcionario", "FaceID": "72"}
    device.templates["72"] = b"modelo72"
    device.next_face = 500

    ok, message = face_fix.start()
    assert ok, message
    for thread in threading.enumerate():
        if thread.name == "face-fix":
            thread.join(timeout=30)
    state = face_fix.status()
    assert not state["running"] and not state["error"], state
    results = {item["name"]: item for item in state["results"]}
    assert all(item["ok"] for item in state["results"]), state["results"]
    assert results["Homem"]["action"] == results["Socia"]["action"] == "recriar"
    faces = {device._by_userid(reg)["FaceID"] for reg in ("H9", "S9")}
    assert len(faces) == 2 and "70" not in faces, "cada um ganhou um FaceID novo"
    assert device._by_userid("F9") is not None, "quem faltava foi cadastrado"
    assert device._by_userid("V9") is None, "vencido saiu do facial"
    assert device._by_userid("N9")["Name"] == "Nome Certo"
    pending = " ".join(f"{item['name']}: {item['message']}" for item in state["pending"])
    assert "Sem Foto" in pending and "Funcionario" in pending, pending
    assert device._by_userid("X9") is not None, "usuário sem cadastro não é apagado sozinho"

    again, _ = face_fix.plan(face_audit.run_audit(thumbnails=False))
    assert not again, f"depois da correção não sobra nada seguro para corrigir: {again}"


def test_conflict_repairs_the_other_member(tmp: Path) -> None:
    import app.face_fix as face_fix

    device, client = _fix_env(tmp, "reparo")
    later = datetime.now() + timedelta(days=10)
    flavio_photo = ic.prepare_face_photo(_photo(tmp / "rep_flavio.jpg", size=(480, 640)))
    paula_photo = ic.prepare_face_photo(_photo(tmp / "rep_paula.jpg", size=(500, 660)))
    client.register_member("Flavio", flavio_photo, registration="FL1", image_ready=True)
    db.create_member(name="Flavio", registration="FL1", device_user_id=None, photo_path=str(flavio_photo), expires_at=later, pool="termica")
    paula = db.create_member(name="Paula", registration="PA1", device_user_id=None, photo_path=str(paula_photo), expires_at=later, pool="termica")
    flavio_face = device._by_userid("FL1")["FaceID"]

    # O facial junta a foto da Paula no FaceID do Flavio.
    device.next_face = int(flavio_face)
    try:
        face_fix.recreate_on_device(client, paula, paula_photo)
    except ic.FaceIDConflict as exc:
        assert exc.others == ["FL1"], exc.others
        assert "Flavio foi refeito" in str(exc), str(exc)
    else:
        raise AssertionError("FaceID repetido precisa dar erro")
    assert device._by_userid("PA1") is None, "Paula sai do facial"
    flavio = device._by_userid("FL1")
    assert flavio and flavio["FaceID"] != flavio_face and flavio["FaceID"] in device.templates, "Flavio refeito com rosto próprio"


def test_use_device_photo(tmp: Path) -> None:
    from app.main import create_app

    device, client = _fix_env(tmp, "usar_foto")
    photo = ic.prepare_face_photo(_photo(tmp / "sistema.jpg", size=(480, 640)))
    created = client.register_member("Clara", photo, registration="C1", image_ready=True)
    member = db.create_member(
        name="Clara", registration="C1", device_user_id=created.device_user_id,
        photo_path=str(photo), expires_at=datetime.now() + timedelta(days=5), pool="termica",
    )
    # A foto boa é a que está no facial; a do sistema é outra.
    device_photo = ic.prepare_face_photo(_photo(tmp / "facial.jpg", size=(520, 700)))
    device.photos[device._by_userid("C1")["FaceID"]] = device_photo.read_bytes()

    app = create_app()
    response = app.test_client().post(
        f"/adm/membros/{member.id}/usar-foto-intelbras",
        json={"url": device.url},
        headers={"Accept": "application/json"},
    )
    data = response.get_json()
    assert response.status_code == 200 and data["ok"], data
    updated = db.get_member(member.id)
    assert updated.photo_path != str(photo), "a foto do facial virou a foto do sistema"
    assert Path(updated.photo_path).is_file()
    assert photo.is_file(), "a foto antiga não é apagada"

    bad = app.test_client().post(
        f"/adm/membros/{member.id}/usar-foto-intelbras",
        json={"url": "http://outro"},
        headers={"Accept": "application/json"},
    )
    assert bad.status_code == 400, "só aceita os faciais configurados"


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
        test_audit_finds_each_problem(root)
        test_fix_all_safe(root)
        test_conflict_repairs_the_other_member(root)
        test_use_device_photo(root)
        test_photo_difference()
    print("face id conflict ok")


if __name__ == "__main__":
    main()
