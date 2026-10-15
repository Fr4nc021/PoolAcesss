"""Testa o teste de validade contra um facial de mentira: cria, tenta e apaga."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.intelbras_client import IntelbrasClient
from app.validity_probe import PROBE_REGISTRATION, run_probe


class FakeDevice:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {
            "S1": {"ID": 1, "UserID": "S1", "Name": "Sócio", "Validity": "0"},
            "01301689041": {
                "ID": 2,
                "UserID": "01301689041",
                "Name": "Configurado na tela",
                "Validity": "2",
                "TimeStart": "2026-10-01 00:00",
                "TimeEnd": "2026-10-31 23:59",
            },
        }
        self.calls: list[str] = []

    def export(self) -> bytes:
        import io
        import tarfile
        from xml.sax.saxutils import quoteattr

        keys = "".join(
            "<Key " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in user.items()) + " />"
            for user in self.users.values()
        )
        xml = f"<KeyData>{keys}</KeyData>".encode("utf-8")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("faceIDs/userdata.xml")
            info.size = len(xml)
            archive.addfile(info, io.BytesIO(xml))
        return buffer.getvalue()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        body = json.loads(request.content or b"{}")
        if path == "/api/system/info":
            return httpx.Response(200, json={"retcode": 0})
        if path == "/api/user/export":
            return httpx.Response(200, content=self.export())
        if path == "/api/user/get":
            return httpx.Response(200, json={"retcode": 0, "data": {"item": list(self.users.values())}})
        item = (body.get("data") or {}).get("item", [{}])[0]
        if path == "/api/user/add":
            self.users[item["UserID"]] = {**item, "ID": 99, "TimeStart": "", "TimeEnd": ""}
            return httpx.Response(200, json={"retcode": 0})
        if path == "/api/user/set":
            user = self.users[item["UserID"]]
            # Simula um aparelho que só entende AAAA-MM-DD HH:MM:SS.
            if "-" in item["TimeEnd"] and " " in item["TimeEnd"]:
                user.update(item)
                return httpx.Response(200, json={"retcode": 0})
            return httpx.Response(200, json={"retcode": -1, "message": "invalid time"})
        if path == "/api/user/del":
            self.users.pop(item.get("UserID"), None)
            return httpx.Response(200, json={"retcode": 0})
        return httpx.Response(404)


def test_probe_reports_formats_and_cleans_up() -> None:
    device = FakeDevice()
    client = IntelbrasClient(base_url="http://192.168.15.86", login="admin", password="x")
    client._client = lambda: httpx.Client(transport=httpx.MockTransport(device.handler))  # noqa: E731
    report = run_probe(client)
    assert "erro" not in report, report
    assert len(report["tentativas"]) == 6
    first = report["tentativas"][0]
    assert first["formato"] == "AAAA-MM-DD HH:MM:SS"
    assert first["guardado"]["Validity"] == "2"
    assert first["guardado"]["TimeEnd"] == first["enviado"]["TimeEnd"]
    assert report["tentativas"][4]["resposta"]["retcode"] == -1
    assert first["guardado_no_export"]["TimeEnd"] == first["enviado"]["TimeEnd"]
    screen = report["configurados_na_tela"]
    assert screen == [
        {
            "matricula_final": "9041",
            "Validity": "2",
            "TimeStart": "2026-10-01 00:00",
            "TimeEnd": "2026-10-31 23:59",
        }
    ], screen
    assert "Configurado na tela" not in json.dumps(report), "nome do sócio não pode aparecer"
    assert report["usuario_de_teste_apagado"] is True
    assert PROBE_REGISTRATION not in device.users
    assert device.users["S1"]["Validity"] == "0", "sócio não pode ser alterado"


def test_probe_cleans_up_even_when_device_fails() -> None:
    device = FakeDevice()

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/user/set":
            raise httpx.ConnectError("caiu")
        return device.handler(request)

    client = IntelbrasClient(base_url="http://192.168.15.44", login="admin", password="x")
    client._client = lambda: httpx.Client(transport=httpx.MockTransport(broken))  # noqa: E731
    report = run_probe(client)
    assert all("erro" in item["resposta"] for item in report["tentativas"])
    assert PROBE_REGISTRATION not in device.users


def main() -> None:
    test_probe_reports_formats_and_cleans_up()
    test_probe_cleans_up_even_when_device_fails()
    print("validity probe ok")


if __name__ == "__main__":
    main()
