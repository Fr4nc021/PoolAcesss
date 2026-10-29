"""Testa o cadastro em ID livre e o teste de FaceID contra um facial como a térmica.

O facial de mentira faz o que a térmica mostrou: dá ao usuário novo o menor
ID interno livre e grava o rosto no número desse ID. Os sócios antigos têm
ID diferente do FaceID, então o menor ID livre cai no FaceID de outro sócio.
A lista da API traz o FaceID como endereço, como o facial real.
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


class SlotDevice(PhotoDevice):
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/user/get":
            items = [
                {**u, "FaceID": f"https://{self.url[7:]}/Image/RegisterImg/{u['FaceID'] if u['FaceID'] not in ('', '0') else -1}.jpg"}
                for u in self.users.values()
            ]
            return httpx.Response(200, json={"retcode": 0, "data": {"item": items}})
        if path == "/api/user/add":
            item = json.loads(request.content)["data"]["item"][0]
            if self._by_userid(item["UserID"]):
                return httpx.Response(200, json={"retcode": -1, "message": "user exist"})
            used = {int(uid) for uid in self.users}
            uid = str(next(n for n in range(1, len(used) + 2) if n not in used))
            self.users[uid] = {"ID": uid, "UserID": item["UserID"], "Name": item["Name"], "FaceID": "0"}
            return httpx.Response(200, json={"retcode": 0, "data": {"item": [{"ID": -1, "UserID": item["UserID"]}]}})
        return super().handler(request)

    def _enroll(self, user: dict, jpeg: bytes) -> None:
        # O rosto vai para o número do ID: o FaceID de quem já estava nele é sobrescrito.
        user["FaceID"] = user["ID"]
        self.templates[user["ID"]] = b"modelo:" + jpeg[-64:]
        self.photos[user["ID"]] = jpeg

    def _export(self) -> bytes:
        return super()._export()


def _thermal() -> SlotDevice:
    device = SlotDevice("http://termica")
    # IDs 1-3 usados; FaceIDs antigos 4 (Jose) e 5 (Winston) são os próximos IDs livres.
    for uid, reg, name, face in (("1", "A1", "Ana", "1"), ("2", "J4", "Jose", "4"), ("3", "W5", "Winston", "5")):
        device.users[uid] = {"ID": uid, "UserID": reg, "Name": name, "FaceID": face}
        device.templates[face] = f"modelo:{name}".encode()
        device.photos[face] = f"foto:{name}".encode()
    return device


def test_clean_face_id() -> None:
    assert ic._clean_face_id("https://192.168.15.44/Image/RegisterImg/-1.jpg") == ""
    assert ic._clean_face_id("https://192.168.15.44/Image/RegisterImg/12.jpg") == "12"
    assert ic._clean_face_id("0") == "" and ic._clean_face_id("7") == "7"
    assert not ic._face_assigned({"FaceID": "https://x/Image/RegisterImg/-1.jpg"}), "endereço -1 é sem rosto"


def test_new_member_skips_ids_that_are_faces(tmp: Path) -> None:
    device = _thermal()
    client = FakeClient(device)
    before = dict(device.templates)
    photo = ic.prepare_face_photo(_photo(tmp / "paula.jpg"))
    created = client.create_user_with_face("Paula", photo, registration="P1")
    assert str(created.device_user_id) not in {"4", "5"}, created
    assert device._by_userid("P1")["FaceID"] == str(created.device_user_id)
    for face in ("1", "4", "5"):
        assert device.templates[face] == before[face], f"rosto do FaceID {face} não muda"
    reserves = sorted(u["ID"] for u in device.users.values() if ic.is_reserve(u["UserID"]))
    assert reserves == ["4", "5"], f"reservas seguram os IDs que são rosto de outro: {reserves}"
    assert all(u["FaceID"] == "0" for u in device.users.values() if ic.is_reserve(u["UserID"])), "reserva sem rosto"


def test_photo_update_on_colliding_id_recreates(tmp: Path) -> None:
    device = _thermal()
    # Flavio está no ID 4, que é o FaceID do Jose: foto nova nele trocaria o rosto do Jose.
    device.users["9"] = {"ID": "9", "UserID": "F9", "Name": "Flavio", "FaceID": "9"}
    device.templates["9"] = b"modelo:flavio"
    device.users["4"] = device.users.pop("9")
    device.users["4"]["ID"] = "4"
    client = FakeClient(device)
    jose = device.templates["4"]
    photo = ic.prepare_face_photo(_photo(tmp / "flavio.jpg", size=(500, 650)))
    client.update_member_face(name="Flavio", registration="F9", image_path=photo)
    flavio = device._by_userid("F9")
    assert flavio["ID"] not in {"4", "5"} and flavio["FaceID"] == flavio["ID"], flavio
    assert device.templates["4"] == jose, "rosto do Jose fica"


def test_probe_read_only_and_real_photo(tmp: Path) -> None:
    device = _thermal()
    device.users["6"] = {"ID": "4", "UserID": "F9", "Name": "Flavio", "FaceID": "9"}
    device.templates["9"] = b"modelo:flavio"
    device.users["4"] = device.users.pop("6")
    client = FakeClient(device)
    snapshot = json.dumps(device.users, sort_keys=True)

    report = probe.run_probe(client)
    assert "erro" not in report, report
    assert json.dumps(device.users, sort_keys=True) == snapshot, "sem foto o teste não mexe em nada"
    hits = report["ids"]["id_igual_ao_faceid_de_outro"]
    assert [(h["nome"], h["e_o_faceid_de"]) for h in hits] == [("Flavio", "Jose")], hits
    assert "cadastro" not in report

    photo = ic.prepare_face_photo(_photo(tmp / "equipe.jpg")).read_bytes()
    report = probe.run_probe(client, photo)
    assert "erro" not in report and "erro" not in report["cadastro"], report
    assert report["cadastro"]["faceid_igual_ao_id"] is True, report["cadastro"]
    assert report["afetados"] == [], report["afetados"]
    assert report["usuario_de_teste_apagado"] is True
    assert device._by_userid(probe.PROBE_REGISTRATION) is None


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_clean_face_id()
        test_new_member_skips_ids_that_are_faces(root)
        test_photo_update_on_colliding_id_recreates(root)
        test_probe_read_only_and_real_photo(root)
    print("faceid probe ok")


if __name__ == "__main__":
    main()
