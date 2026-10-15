"""Descobre como o facial guarda uma data de validade, sem mexer em sócios.

Cria um usuário de teste sem foto, tenta gravar Validity=2 (Agenda) com
TimeStart/TimeEnd em formatos diferentes e apaga o usuário. A lista do
aparelho não mostra TimeStart/TimeEnd, então o teste lê o arquivo de
exportação (userdata.xml), onde esses campos aparecem. Também mostra as
datas de quem foi configurado com Agenda pela tela do Intelbras, sem nome
e com a matrícula mascarada. O resultado vai para a tela da ADM.
"""

from __future__ import annotations

import tarfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import httpx

from app.intelbras_client import ALWAYS_SCHEDULE_RELAY, DeviceError, IntelbrasClient

PROBE_REGISTRATION = "APVALTESTE"
PROBE_NAME = "TESTE VALIDADE (apagar)"
_FIELDS = ("Validity", "TimeStart", "TimeEnd", "Frequency", "Mon", "Sun", "ScheduleRelay")
_EXPORT_FIELDS = ("Validity", "TimeStart", "TimeEnd", "Frequency", "Mon", "Tue", "Sun", "Relay")


def _formats(start: datetime, end: datetime) -> list[tuple[str, str, str]]:
    return [
        ("AAAA-MM-DD HH:MM:SS", start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        ("AAAA-MM-DDTHH:MM:SS", start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")),
        ("AAAA/MM/DD HH:MM:SS", start.strftime("%Y/%m/%d %H:%M:%S"), end.strftime("%Y/%m/%d %H:%M:%S")),
        ("AAAA-MM-DD", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
        ("AAAAMMDDHHMMSS", start.strftime("%Y%m%d%H%M%S"), end.strftime("%Y%m%d%H%M%S")),
        ("segundos unix", str(int(start.timestamp())), str(int(end.timestamp()))),
    ]


def _post(client: IntelbrasClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    with client._client() as http:
        response = http.post(client._url(path), json=payload)
        response.raise_for_status()
        return response.json()


def _stored(client: IntelbrasClient) -> dict[str, Any]:
    user = client.find_user_by_registration(PROBE_REGISTRATION) or {}
    return {key: user.get(key) for key in _FIELDS if key in user}


def _export_keys(client: IntelbrasClient) -> list[dict[str, str]]:
    with client._client() as http:
        response = http.get(client._url("/api/user/export"))
        response.raise_for_status()
        raw = response.content
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
        member = archive.extractfile("faceIDs/userdata.xml")
        if member is None:
            return []
        root = ET.fromstring(member.read())
    return [dict(key.attrib) for key in root.findall("Key")]


def _exported_probe(client: IntelbrasClient) -> dict[str, Any]:
    try:
        for key in _export_keys(client):
            if key.get("UserID") == PROBE_REGISTRATION:
                return {field: key.get(field) for field in _EXPORT_FIELDS if field in key}
        return {"aviso": "usuário de teste não apareceu no export"}
    except (httpx.HTTPError, tarfile.TarError, ET.ParseError, KeyError) as exc:
        return {"erro": f"export: {exc}"}


def _configured_on_screen(client: IntelbrasClient) -> list[dict[str, Any]]:
    """Quem tem Agenda configurada pela tela: só campos de data, sem nome."""
    rows: list[dict[str, Any]] = []
    try:
        keys = _export_keys(client)
    except (httpx.HTTPError, tarfile.TarError, ET.ParseError, KeyError) as exc:
        return [{"erro": f"export: {exc}"}]
    for key in keys:
        if key.get("UserID") == PROBE_REGISTRATION or key.get("Validity") in (None, "", "0"):
            continue
        user_id = str(key.get("UserID") or "")
        row = {"matricula_final": user_id[-4:]}
        row.update({field: key.get(field) for field in _EXPORT_FIELDS if field in key})
        rows.append(row)
    return rows[:20]


def run_probe(client: IntelbrasClient) -> dict[str, Any]:
    report: dict[str, Any] = {"facial": client.base_url, "tentativas": []}
    report["configurados_na_tela"] = _configured_on_screen(client)
    start = datetime.now().replace(microsecond=0)
    end = (start + timedelta(days=30)).replace(hour=23, minute=59, second=59)
    try:
        client.ping()
        client.delete_user(registration=PROBE_REGISTRATION)
        created = _post(
            client,
            "/api/user/add",
            {
                "target": "user",
                "action": "add",
                "data": {
                    "item": [
                        {
                            "Name": PROBE_NAME,
                            "UserID": PROBE_REGISTRATION,
                            "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
                            "WebRelay": "0",
                            "LiftFloorNum": "9999",
                            "Validity": "0",
                        }
                    ]
                },
            },
        )
        report["criado"] = created
        user = client.find_user_by_registration(PROBE_REGISTRATION)
        if not user:
            report["erro"] = "O usuário de teste não apareceu na lista do facial."
            return report
        report["campos_do_usuario"] = sorted(user.keys())
        for label, time_start, time_end in _formats(start, end):
            item = {
                "ID": str(user["ID"]),
                "UserID": PROBE_REGISTRATION,
                "Name": PROBE_NAME,
                "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
                "WebRelay": "0",
                "LiftFloorNum": "9999",
                "Validity": "2",
                "TimeStart": time_start,
                "TimeEnd": time_end,
            }
            try:
                answer = _post(
                    client,
                    "/api/user/set",
                    {"target": "user", "action": "set", "data": {"item": [item]}},
                )
            except (httpx.HTTPError, ValueError) as exc:
                answer = {"erro": str(exc)}
            report["tentativas"].append(
                {
                    "formato": label,
                    "enviado": {"TimeStart": time_start, "TimeEnd": time_end},
                    "resposta": answer,
                    "guardado": _stored(client),
                    "guardado_no_export": _exported_probe(client),
                }
            )
    except (DeviceError, httpx.HTTPError, ValueError) as exc:
        report["erro"] = str(exc)
    finally:
        try:
            client.delete_user(registration=PROBE_REGISTRATION)
            report["usuario_de_teste_apagado"] = True
        except (DeviceError, httpx.HTTPError) as exc:
            report["usuario_de_teste_apagado"] = f"não: {exc}"
    return report
