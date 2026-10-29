"""Testa o teste de FaceID contra um facial que dá "quantidade de rostos + 1".

Com um buraco nos FaceIDs, essa regra cai no FaceID de um sócio, que é o
que a térmica faz. O teste tem que achar a regra, listar o sócio afetado e
apagar o usuário de teste.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_face_id_conflict import PhotoDevice  # noqa: E402 — monta o ambiente de teste
from test_face_and_block import FakeClient  # noqa: E402

import app.faceid_probe as probe

probe.time.sleep = lambda _seconds: None  # noqa: E731


class CountPlusOneDevice(PhotoDevice):
    def _enroll(self, user: dict, jpeg: bytes) -> None:
        with_face = [u for u in self.users.values() if u["FaceID"] not in ("", "0") and u is not user]
        self.next_face = len(with_face) + 1
        super()._enroll(user, jpeg)


def main() -> None:
    device = CountPlusOneDevice("http://termica")
    device.next_id = 50
    for uid, reg, name, face in (("1", "A1", "Ana", "1"), ("2", "B2", "Beto", "2"), ("3", "C3", "Flavio", "4")):
        device.users[uid] = {"ID": uid, "UserID": reg, "Name": name, "FaceID": face}
        device.templates[face] = f"modelo:{name}".encode()
    client = FakeClient(device)

    report = probe.run_probe(client)
    assert "erro" not in report, report
    assert report["antes"]["previsoes"]["quantidade_mais_1"] == 4
    first = report["testes"][0]
    assert first["faceid_dado"] == "4", first
    assert first["ja_era_de"] == ["…C3"], "mostra que o FaceID dado já era de um sócio"
    assert "quantidade_mais_1" in first["regras_que_batem"], first
    assert report["testes"][1]["respeitou_o_pedido"] is False, "este facial ignora FaceID no cadastro"
    assert "erro" in report["testes"][3], "RegisterImg indisponível vira erro no relatório, não quebra o teste"
    assert report["usuario_de_teste_apagado"] is True
    assert device._by_userid(probe.PROBE_REGISTRATION) is None
    names = [row["nome"] for row in report["afetados"]]
    assert names == ["Flavio"], f"o sócio do FaceID repetido aparece como afetado: {report['afetados']}"
    assert {u["UserID"] for u in device.users.values()} == {"A1", "B2", "C3"}, "nenhum sócio apagado"
    print("faceid probe ok")


if __name__ == "__main__":
    main()
