"""Testa a numeração do facial e o teste de FaceID contra um facial como a térmica.

O facial real conta a foto guardada e o número da lista a partir de 0:
FaceID 12 aparece como RegFaceMeun/11.jpg no export e como
".../Image/RegisterImg/11.jpg" na lista. O ID interno não tem relação com o
FaceID. Ler o número errado mostrava a foto de outra pessoa na conferência.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from test_face_id_conflict import PhotoDevice  # noqa: E402 — monta o ambiente de teste
from test_face_and_block import FakeClient, _photo  # noqa: E402

import app.faceid_probe as probe
import app.intelbras_client as ic


class ThermalDevice(PhotoDevice):
    """Lista com o FaceID em endereço contado a partir de 0 e IDs internos altos."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/user/get":
            items = []
            for user in self.users.values():
                face = int(user["FaceID"] or 0)
                index = face - 1 if face else -1
                items.append({**user, "FaceID": f"https://{self.url[7:]}/Image/RegisterImg/{index}.jpg"})
            return httpx.Response(200, json={"retcode": 0, "data": {"item": items}})
        return super().handler(request)


def _thermal(tmp: Path) -> tuple[ThermalDevice, dict[str, bytes]]:
    device = ThermalDevice("http://termica")
    device.next_id = 1700
    device.next_face = 7
    faces: dict[str, bytes] = {}
    for uid, reg, name, face, size in (
        ("1612", "A1", "Ana", "1", (480, 640)),
        ("2", "J4", "Jose", "4", (500, 640)),
        ("1722", "W6", "Winston", "6", (520, 660)),
    ):
        jpeg = ic.prepare_face_photo(_photo(tmp / f"{name}.jpg", size=size)).read_bytes()
        device.users[uid] = {"ID": uid, "UserID": reg, "Name": name, "FaceID": face}
        device.templates[face] = f"modelo:{name}".encode()
        device.photos[face] = jpeg
        faces[reg] = jpeg
    return device, faces


def test_numbering() -> None:
    assert ic._clean_face_id("https://192.168.15.44/Image/RegisterImg/-1.jpg") == ""
    assert ic._clean_face_id("https://192.168.15.44/Image/RegisterImg/0.jpg") == "1"
    assert ic._clean_face_id("https://192.168.15.44/Image/RegisterImg/11.jpg") == "12"
    assert ic._clean_face_id("0") == "" and ic._clean_face_id("7") == "7"
    assert not ic._face_assigned({"FaceID": "https://x/Image/RegisterImg/-1.jpg"}), "-1 é sem rosto"
    assert ic._face_assigned({"FaceID": "https://x/Image/RegisterImg/0.jpg"}), "0 é o FaceID 1"
    assert ic._photo_face_id("11") == "12" and ic._photo_face_id("capture") == "capture"


def test_export_photo_is_the_users_own(tmp: Path) -> None:
    device, faces = _thermal(tmp)
    users = {u.user_id: u for u in FakeClient(device).export_users(with_photos=True)}
    for reg, jpeg in faces.items():
        assert users[reg].photo == jpeg, f"a foto de {reg} é a dele, não a do FaceID seguinte"
        assert users[reg].has_model


def test_probe_read_only(tmp: Path) -> None:
    device, _faces = _thermal(tmp)
    client = FakeClient(device)
    before = json.dumps(device.users, sort_keys=True)
    report = probe.run_probe(client)
    assert "erro" not in report, report
    assert json.dumps(device.users, sort_keys=True) == before, "sem foto o teste não mexe em nada"
    faces = report["rostos"]
    assert faces["lista_bate_com_export"] is True, faces
    assert all(row["tem_modelo"] and row["tem_foto"] for row in faces["mapa"]), faces["mapa"]
    assert report["antes"]["com_modelo_sem_foto"] == [] and report["antes"]["fotos_sem_usuario"] == []
    assert "cadastro" not in report


def test_probe_with_real_photo(tmp: Path) -> None:
    device, _faces = _thermal(tmp)
    client = FakeClient(device)
    photo = ic.prepare_face_photo(_photo(tmp / "equipe.jpg", size=(540, 700))).read_bytes()
    probe.PHOTOS_DIR = tmp
    report = probe.run_probe(client, photo)
    assert not list(tmp.glob("teste_faceid_*")), "a foto de teste sai do PC"
    assert "erro" not in report and "erro" not in report["cadastro"], report
    assert report["cadastro"]["faceid_dado"] == "7" and report["cadastro"]["ja_era_de"] == []
    assert report["cadastro"]["menor_livre_antes"] == 2
    assert report["afetados"] == [], report["afetados"]
    assert report["usuario_de_teste_apagado"] is True
    assert device._by_userid(probe.PROBE_REGISTRATION) is None


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_numbering()
        test_export_photo_is_the_users_own(root)
        test_probe_read_only(root)
        test_probe_with_real_photo(root)
    print("faceid probe ok")


if __name__ == "__main__":
    main()
