"""Descobre como o facial escolhe o FaceID de um rosto novo.

Na térmica, o rosto de um sócio recriado do zero caiu no FaceID de outro.
Quem escolhe o número é o facial, então este teste mede a regra dele:

1. lê o export e mostra os FaceIDs em uso (sem nomes, matrícula mascarada);
2. cria um usuário de teste, envia uma foto e vê que FaceID o facial deu,
   comparando com regras prováveis (maior + 1, quantidade + 1, menor livre);
3. vê se o usuário apagado deixa o modelo ou a foto para trás;
4. tenta escolher o FaceID: no cadastro, no user/set e pelo RegisterImg.

Usa só o usuário de teste e apaga no final. Antes e depois compara o modelo
e a foto de cada FaceID: se algum sócio foi afetado, o relatório lista o
nome para refazer com "Enviar foto do sistema".
"""

from __future__ import annotations

import base64
import hashlib
import tarfile
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw

from app.intelbras_client import (
    ALWAYS_SCHEDULE_RELAY,
    DeviceError,
    IntelbrasClient,
    _clean_face_id,
    _prepare_jpeg_base64,
)

PROBE_REGISTRATION = "APFIDTESTE"
PROBE_NAME = "TESTE FACEID (apagar)"
WAIT_ROUNDS = 8
WAIT_DELAY = 1.5


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
    numbers = sorted({int(u["face_id"]) for u in with_face if u["face_id"].isdigit()})
    owners: dict[str, list[str]] = {}
    for user in with_face:
        owners.setdefault(user["face_id"], []).append(_mask(user["registration"]))
    lowest_free = next((n for n in range(1, (numbers[-1] if numbers else 0) + 2) if n not in numbers), 1)
    return {
        "usuarios": len(users),
        "com_faceid": len(with_face),
        "faceids_diferentes": len(numbers),
        "menor_faceid": numbers[0] if numbers else None,
        "maior_faceid": numbers[-1] if numbers else None,
        "buracos": (numbers[-1] - len(numbers)) if numbers else 0,
        "repetidos": {face_id: regs for face_id, regs in owners.items() if len(regs) > 1},
        "modelos_sem_usuario": sorted(set(snap["models"]) - {u["face_id"] for u in with_face}),
        "fotos_sem_usuario": sorted(set(snap["photos"]) - {u["face_id"] for u in with_face}),
        "previsoes": {
            "maior_mais_1": (numbers[-1] + 1) if numbers else 1,
            "quantidade_mais_1": len(with_face) + 1,
            "usuarios_mais_1": len(users) + 1,
            "menor_livre": lowest_free,
        },
    }


def _test_photo(photo: Optional[bytes]) -> str:
    """Foto em base64: a enviada pela tela ou um desenho sem rosto de ninguém."""
    if photo:
        return base64.b64encode(photo).decode("ascii")
    img = Image.new("RGB", (480, 640), (196, 170, 150))
    draw = ImageDraw.Draw(img)
    draw.ellipse((140, 160, 340, 420), fill=(220, 190, 165))
    draw.ellipse((185, 250, 215, 275), fill=(40, 30, 30))
    draw.ellipse((265, 250, 295, 275), fill=(40, 30, 30))
    draw.arc((200, 320, 280, 370), 20, 160, fill=(120, 50, 50), width=6)
    out = BytesIO()
    img.save(out, format="JPEG", quality=92)
    return base64.b64encode(out.getvalue()).decode("ascii")


def _post(client: IntelbrasClient, path: str, item: dict[str, Any], action: str) -> Any:
    try:
        with client._client() as http:
            response = http.post(
                client._url(path),
                json={"target": "user", "action": action, "data": {"item": [item]}},
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"erro": str(exc)}


def _base_item(**extra: Any) -> dict[str, Any]:
    return {
        "Name": PROBE_NAME,
        "UserID": PROBE_REGISTRATION,
        "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
        "WebRelay": "0",
        "LiftFloorNum": "9999",
        "Validity": "0",
        **extra,
    }


def _probe_face_id(client: IntelbrasClient) -> tuple[str, bool]:
    """Espera o facial dar FaceID ao usuário de teste. Devolve o FaceID e se gerou modelo."""
    face_id = ""
    for attempt in range(WAIT_ROUNDS):
        snap = _snapshot(client)
        face_id = next((u["face_id"] for u in snap["users"] if u["registration"] == PROBE_REGISTRATION), "")
        if face_id and face_id in snap["models"]:
            return face_id, True
        if attempt + 1 < WAIT_ROUNDS:
            time.sleep(WAIT_DELAY)
    return face_id, False


def _owners(snap: dict[str, Any], face_id: str) -> list[str]:
    return [
        _mask(u["registration"])
        for u in snap["users"]
        if face_id and u["face_id"] == face_id and u["registration"] != PROBE_REGISTRATION
    ]


def _cleanup(client: IntelbrasClient) -> Any:
    try:
        client.delete_user(registration=PROBE_REGISTRATION)
        return True
    except DeviceError as exc:
        return f"não: {exc}"


def _attempt(
    client: IntelbrasClient,
    label: str,
    before: dict[str, Any],
    photo_b64: str,
    *,
    add_extra: Optional[dict[str, Any]] = None,
    set_extra: Optional[dict[str, Any]] = None,
    face_url: Optional[str] = None,
    wanted: Optional[str] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"teste": label}
    _cleanup(client)
    result["resposta_add"] = _post(client, "/api/user/add", _base_item(**(add_extra or {})), "add")
    user = client.find_user_by_registration(PROBE_REGISTRATION)
    if not user:
        result["erro"] = "usuário de teste não apareceu na lista"
        return result
    result["faceid_depois_do_add"] = _clean_face_id(user.get("FaceID")) or None
    item = _base_item(ID=str(user["ID"]), FaceUrl=face_url or photo_b64, **(set_extra or {}))
    result["resposta_set"] = _post(client, "/api/user/set", item, "set")
    face_id, has_model = _probe_face_id(client)
    result["faceid_dado"] = face_id or None
    result["gerou_modelo"] = has_model
    if wanted is not None:
        result["faceid_pedido"] = wanted
        result["respeitou_o_pedido"] = face_id == wanted
    result["ja_era_de"] = _owners(before, face_id)
    result["regras_que_batem"] = [
        rule for rule, value in _summary(before)["previsoes"].items() if face_id and str(value) == face_id
    ]
    return result


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


def run_probe(client: IntelbrasClient, photo: Optional[bytes] = None) -> dict[str, Any]:
    report: dict[str, Any] = {"facial": client.base_url, "foto_de_teste": "enviada pela tela" if photo else "desenho sem rosto real"}
    before: Optional[dict[str, Any]] = None
    try:
        client.ping()
        _cleanup(client)
        before = _snapshot(client)
        report["antes"] = _summary(before)
        photo_b64 = _test_photo(photo)
        tests = report["testes"] = []

        tests.append(_attempt(client, "facial escolhe (como hoje)", before, photo_b64))
        removed_id = tests[-1].get("faceid_dado")
        _cleanup(client)
        if removed_id:
            leftover = _snapshot(client)
            report["depois_de_apagar"] = {
                "faceid": removed_id,
                "modelo_ficou": removed_id in leftover["models"] and not _owners(leftover, removed_id),
                "foto_ficou": removed_id in leftover["photos"] and not _owners(leftover, removed_id),
            }

        used = {int(u["face_id"]) for u in before["users"] if u["face_id"].isdigit()}
        if removed_id and str(removed_id).isdigit():
            used.add(int(removed_id))
        free = str((max(used) if used else 0) + 1)

        tests.append(_attempt(client, "FaceID no cadastro (user/add)", before, photo_b64, add_extra={"FaceID": free}, wanted=free))
        tests.append(_attempt(client, "FaceID junto da foto (user/set)", before, photo_b64, set_extra={"FaceID": free}, wanted=free))

        register = {"teste": "foto em RegisterImg/<FaceID>.jpg"}
        try:
            data = base64.b64decode(photo_b64)
            url = client._put_register_img(int(free), data)
            register.update(_attempt(client, register["teste"], before, photo_b64, face_url=url, wanted=free))
        except DeviceError as exc:
            register["erro"] = str(exc)
        tests.append(register)
    except (DeviceError, httpx.HTTPError, tarfile.TarError, ET.ParseError, OSError) as exc:
        report["erro"] = str(exc)
    finally:
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
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "teste.jpg"
        path.write_bytes(raw)
        return base64.b64decode(_prepare_jpeg_base64(path))
