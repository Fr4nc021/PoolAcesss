"""Mostra como cada facial guarda o rosto de cada usuário (ID, FaceID, modelo e foto).

O teste só de leitura da térmica mostrou que o ID interno não tem relação
com o FaceID, e que a foto guardada e o número da lista contam a partir de
0 (FaceID 12 = RegFaceMeun/11.jpg = RegisterImg/11.jpg).

1. Só leitura: para cada usuário (matrícula mascarada), o ID, o FaceID, se
   tem modelo do rosto e se tem foto guardada, e se a lista bate com o export.
2. Só com uma foto real enviada pela tela: cadastra um usuário de teste
   como um sócio, mostra o FaceID que ele ganhou e de quem esse número já
   era, e apaga. Antes e depois compara o modelo e a foto de cada FaceID;
   quem mudou aparece pelo nome para refazer.
"""

from __future__ import annotations

import base64
import hashlib
import tarfile
import tempfile
import uuid
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import PHOTOS_DIR, ensure_directories
from app.intelbras_client import (
    DeviceError,
    IntelbrasClient,
    _clean_face_id,
    _photo_face_id,
    _prepare_jpeg_base64,
    public_media_url,
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
                stem = item.name.rsplit("/", 1)[-1].split(".", 1)[0]
                photos[_photo_face_id(stem)] = hashlib.sha256(fileobj.read()).hexdigest()
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
    in_use = {u["face_id"] for u in with_face}
    return {
        "usuarios": len(users),
        "com_faceid": len(with_face),
        "repetidos": {face_id: regs for face_id, regs in owners.items() if len(regs) > 1},
        "com_modelo_sem_foto": sorted(_mask(u["registration"]) for u in with_face if u["face_id"] not in snap["photos"]),
        "sem_modelo": sorted(_mask(u["registration"]) for u in with_face if u["face_id"] not in snap["models"]),
        "modelos_sem_usuario": sorted(set(snap["models"]) - in_use),
        "fotos_sem_usuario": sorted(set(snap["photos"]) - in_use),
    }


def _face_map(client: IntelbrasClient, snap: dict[str, Any]) -> dict[str, Any]:
    """ID, FaceID, modelo e foto de cada usuário, e se a lista da API bate com o export."""
    api = client.list_users()
    by_registration = {str(u.get("UserID") or ""): u for u in api}
    rows: list[dict[str, Any]] = []
    for user in snap["users"]:
        listed = by_registration.get(user["registration"]) or {}
        face_id = user["face_id"]
        rows.append(
            {
                "matricula": _mask(user["registration"]),
                "id": str(listed.get("ID") or user["device_id"] or "") or None,
                "faceid": face_id or None,
                "faceid_na_lista": _clean_face_id(listed.get("FaceID")) or None,
                "tem_modelo": bool(face_id) and face_id in snap["models"],
                "tem_foto": bool(face_id) and face_id in snap["photos"],
            }
        )
    return {
        "usuarios_na_lista": len(api),
        "usuarios_no_export": len(snap["users"]),
        "lista_bate_com_export": all(row["faceid"] == row["faceid_na_lista"] for row in rows),
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
    result: dict[str, Any] = {"teste": "cadastro como um sócio"}
    # Como no cadastro de verdade, o facial baixa a foto deste PC pela FaceUrl.
    ensure_directories()
    path = PHOTOS_DIR / f"teste_faceid_{uuid.uuid4().hex[:8]}.jpg"
    path.write_bytes(photo)
    face_url = public_media_url(path.name)
    result["foto_por_endereco"] = bool(face_url)
    try:
        created = client.create_user_with_face(
            PROBE_NAME, path, registration=PROBE_REGISTRATION, face_public_url=face_url, image_ready=True
        )
        result["id_dado"] = str(created.device_user_id)
    except DeviceError as exc:
        result["erro"] = str(exc)
    finally:
        path.unlink(missing_ok=True)
    after = _snapshot(client)
    mine = next((u for u in after["users"] if u["registration"] == PROBE_REGISTRATION), None)
    face_id = (mine or {}).get("face_id") or ""
    used = sorted(int(u["face_id"]) for u in before["users"] if u["face_id"].isdigit())
    result["faceid_dado"] = face_id or None
    result["gerou_modelo"] = bool(face_id) and face_id in after["models"]
    result["ja_era_de"] = [
        _mask(u["registration"])
        for u in before["users"]
        if face_id and u["face_id"] == face_id and u["registration"] != PROBE_REGISTRATION
    ]
    result["menor_livre_antes"] = next((n for n in range(1, len(used) + 2) if n not in used), 1)
    result["maior_antes"] = used[-1] if used else None
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
        report["rostos"] = _face_map(client, before)
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
