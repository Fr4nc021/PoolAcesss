"""Mostra como cada facial liga o ID interno de cada usuário ao FaceID do rosto.

O facial grava o rosto de um usuário novo no número do ID interno dele e
escolhe esse ID sozinho. Nos sócios antigos os dois números não batem, então
um usuário novo podia ganhar um ID igual ao FaceID de outro sócio e trocar o
rosto desse sócio (Paula no FaceID do Flavio, o teste anterior no Winston).

1. Só leitura: o mapa ID -> FaceID de cada usuário (matrícula mascarada) e,
   pelo nome, quem tem o ID igual ao FaceID de outro.
2. Só com uma foto real enviada pela tela: cadastra um usuário de teste pelo
   caminho novo (ID livre, com reserva quando preciso), mostra o ID e o
   FaceID que ele ganhou e apaga. Antes e depois compara o modelo e a foto de
   cada FaceID; quem mudou aparece pelo nome para refazer.
"""

from __future__ import annotations

import base64
import hashlib
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx

from app.intelbras_client import (
    DeviceError,
    IntelbrasClient,
    _clean_face_id,
    _prepare_jpeg_base64,
    is_reserve,
)

PROBE_REGISTRATION = "APFIDTESTE"
PROBE_NAME = "TESTE FACEID (apagar)"


def _mask(registration: str) -> str:
    return f"…{registration[-4:]}" if registration else "?"


def _snapshot(client: IntelbrasClient) -> dict[str, Any]:
    """Usuários do export e o hash do modelo e da foto de cada FaceID."""
    raw = client._export_archive()
    models: dict[str, Any] = {}
    photos: dict[str, str] = {}
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
        xml_member = archive.extractfile("faceIDs/userdata.xml")
        if xml_member is None:
            raise DeviceError("O export do facial veio sem userdata.xml.")
        root = ET.fromstring(xml_member.read())
        for item in sorted(archive.getmembers(), key=lambda entry: entry.name):
            if not (item.isfile() and item.size > 0):
                continue
            parts = item.name.split("/")
            fileobj = archive.extractfile(item)
            if fileobj is None:
                continue
            if len(parts) >= 3 and parts[0] == "faceIDs" and parts[1] != "RegFaceMeun" and "-" in parts[1]:
                face_id = parts[1].split("-", 1)[0]
                models.setdefault(face_id, hashlib.sha256()).update(fileobj.read())
            elif item.name.startswith("faceIDs/RegFaceMeun/"):
                photos[item.name.rsplit("/", 1)[-1].split(".", 1)[0]] = hashlib.sha256(fileobj.read()).hexdigest()
    users = [
        {
            "registration": str(key.get("UserID") or ""),
            "name": str(key.get("Name") or ""),
            "face_id": _clean_face_id(key.get("FaceID")),
            "device_id": str(key.get("ID") or ""),
        }
        for key in root.findall("Key")
    ]
    return {
        "users": users,
        "models": {face_id: digest.hexdigest() for face_id, digest in models.items()},
        "photos": photos,
    }


def _summary(snap: dict[str, Any]) -> dict[str, Any]:
    users = [u for u in snap["users"] if u["registration"] != PROBE_REGISTRATION]
    with_face = [u for u in users if u["face_id"]]
    owners: dict[str, list[str]] = {}
    for user in with_face:
        owners.setdefault(user["face_id"], []).append(_mask(user["registration"]))
    return {
        "usuarios": len(users),
        "reservas": sum(1 for u in users if is_reserve(u["registration"])),
        "com_faceid": len(with_face),
        "repetidos": {face_id: regs for face_id, regs in owners.items() if len(regs) > 1},
        "modelos_sem_usuario": sorted(set(snap["models"]) - {u["face_id"] for u in with_face}),
    }


def _id_map(client: IntelbrasClient, snap: dict[str, Any]) -> dict[str, Any]:
    """ID interno x FaceID de cada usuário e quem tem o ID igual ao FaceID de outro."""
    api = client.list_users()
    by_registration = {str(u.get("UserID") or ""): u for u in api}
    face_owner = {u["face_id"]: u for u in snap["users"] if u["face_id"]}
    rows: list[dict[str, Any]] = []
    collisions: list[dict[str, str]] = []
    for user in snap["users"]:
        registration = user["registration"]
        listed = by_registration.get(registration) or {}
        device_id = str(listed.get("ID") or user["device_id"] or "")
        rows.append(
            {
                "matricula": _mask(registration),
                "id": device_id or None,
                "faceid": user["face_id"] or None,
                "faceid_na_lista": _clean_face_id(listed.get("FaceID")) or None,
                "reserva": is_reserve(registration),
            }
        )
        owner = face_owner.get(device_id)
        if owner and owner["registration"] != registration and not is_reserve(registration):
            collisions.append(
                {
                    "nome": user["name"],
                    "matricula": registration,
                    "id": device_id,
                    "e_o_faceid_de": owner["name"],
                    "matricula_dele": owner["registration"],
                }
            )
    return {
        "usuarios_na_lista": len(api),
        "usuarios_no_export": len(snap["users"]),
        "id_igual_ao_faceid_de_outro": collisions,
        "mapa": rows,
    }


def _affected(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """Sócios cujo modelo ou foto mudou durante o teste: precisam ser refeitos."""
    rows: list[dict[str, str]] = []
    for user in before["users"]:
        face_id = user["face_id"]
        if not face_id or user["registration"] == PROBE_REGISTRATION:
            continue
        changed = []
        if before["models"].get(face_id) != after["models"].get(face_id):
            changed.append("modelo do rosto")
        if before["photos"].get(face_id) != after["photos"].get(face_id):
            changed.append("foto")
        if changed:
            rows.append({"nome": user["name"], "matricula": user["registration"], "faceid": face_id, "mudou": ", ".join(changed)})
    return rows


def _cleanup(client: IntelbrasClient) -> Any:
    try:
        client.delete_user(registration=PROBE_REGISTRATION)
        return True
    except DeviceError as exc:
        return f"não: {exc}"


def _create_test(client: IntelbrasClient, photo: bytes, before: dict[str, Any]) -> dict[str, Any]:
    """Cadastra o usuário de teste pelo caminho dos sócios e mostra os números que ele ganhou."""
    result: dict[str, Any] = {"teste": "cadastro pelo caminho novo (ID livre)"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "teste.jpg"
        path.write_bytes(photo)
        try:
            created = client.create_user_with_face(PROBE_NAME, path, registration=PROBE_REGISTRATION)
            result["id_dado"] = str(created.device_user_id)
        except DeviceError as exc:
            result["erro"] = str(exc)
    after = _snapshot(client)
    mine = next((u for u in after["users"] if u["registration"] == PROBE_REGISTRATION), None)
    result["faceid_dado"] = (mine or {}).get("face_id") or None
    result["gerou_modelo"] = bool(mine and mine["face_id"] in after["models"])
    if mine and result.get("id_dado"):
        result["faceid_igual_ao_id"] = mine["face_id"] == result["id_dado"]
    result["reservas_criadas"] = _summary(after)["reservas"] - _summary(before)["reservas"]
    return result


def run_probe(client: IntelbrasClient, photo: Optional[bytes] = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "facial": client.base_url,
        "foto_de_teste": "enviada pela tela" if photo else "nenhuma: só leitura",
    }
    before: Optional[dict[str, Any]] = None
    try:
        client.ping()
        _cleanup(client)
        before = _snapshot(client)
        report["antes"] = _summary(before)
        report["ids"] = _id_map(client, before)
        if photo:
            report["cadastro"] = _create_test(client, photo, before)
    except (DeviceError, httpx.HTTPError, tarfile.TarError, ET.ParseError, OSError) as exc:
        report["erro"] = str(exc)
    finally:
        if photo:
            report["usuario_de_teste_apagado"] = _cleanup(client)
            if before is not None:
                try:
                    after = _snapshot(client)
                    report["afetados"] = _affected(before, after)
                    report["depois"] = _summary(after)
                except (DeviceError, httpx.HTTPError, tarfile.TarError, ET.ParseError, OSError) as exc:
                    report["afetados"] = f"não deu para conferir: {exc}"
    return report


def prepared_photo(raw: bytes) -> bytes:
    """Foto enviada pela tela no JPEG que o facial aceita."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "teste.jpg"
        path.write_bytes(raw)
        return base64.b64decode(_prepare_jpeg_base64(path))
