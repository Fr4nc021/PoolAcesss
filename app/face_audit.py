"""Confere sócio a sócio se o cadastro e a foto no Intelbras batem com o banco.

Lê o export de cada facial uma vez (usuários, FaceID, modelo do rosto e a
foto que o facial guardou) e compara com o banco deste PC. O facial
reconhece pelo FaceID: dois usuários no mesmo FaceID fazem o rosto de um
liberar a entrada no nome do outro, que é o problema que esta tela acha.
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

from app import db
from app.config import registration_pools, selected_pools
from app.intelbras_client import ExportUser, IntelbrasClient, is_reserve

logger = logging.getLogger(__name__)

THUMB_SIDE = 96
# Diferença do dHash (0 a 64) a partir da qual a foto do facial parece outra.
# Mesma foto recomprimida ou recortada fica bem abaixo; é só um aviso para olhar.
PHOTO_DIFF_WARN = 20


def _thumb(data: Optional[bytes]) -> Optional[str]:
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((THUMB_SIDE, THUMB_SIDE * 4 // 3))
            out = BytesIO()
            img.save(out, format="JPEG", quality=70)
    except Exception:  # noqa: BLE001 — foto que não abre não tem miniatura
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _dhash(data: Optional[bytes]) -> Optional[int]:
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as img:
            gray = ImageOps.exif_transpose(img).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
    except Exception:  # noqa: BLE001
        return None
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return value


def photo_difference(ours: Optional[bytes], device: Optional[bytes]) -> Optional[int]:
    """Bits diferentes entre os dHash das duas fotos; None se faltar alguma."""
    a, b = _dhash(ours), _dhash(device)
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold()[:60] == " ".join(b.split()).casefold()[:60]


def _read_photo(member: db.Member) -> Optional[bytes]:
    if not member.photo_path:
        return None
    path = Path(member.photo_path)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _member_check(
    member: db.Member,
    user: Optional[ExportUser],
    users: list[ExportUser],
    our_photo: Optional[bytes],
    thumbnails: bool = True,
) -> dict[str, Any]:
    should_be_there = not member.access_blocked and not member.is_expired
    problems: list[str] = []
    warnings: list[str] = []
    # Códigos para a correção automática; o texto é só para a tela.
    codes: list[str] = []
    shared: list[str] = []
    difference = None
    if user is None:
        if should_be_there:
            codes.append("missing")
            problems.append("Não está no facial: não entra.")
    else:
        if not should_be_there:
            codes.append("expired_on_device")
            problems.append("Exame vencido, mas continua no facial.")
        elif not user.face_id or not user.has_model:
            codes.append("no_model")
            problems.append("Sem modelo do rosto no facial: aparece como desconhecido.")
        if user.face_id:
            shared = [
                other.name or other.user_id
                for other in users
                if other.face_id == user.face_id and other.user_id != user.user_id
            ]
            if shared and should_be_there:
                codes.append("shared")
                problems.append(
                    f"Mesmo FaceID {user.face_id} de {', '.join(shared)}: o rosto de um libera no nome do outro. "
                    "A foto do Intelbras é uma só para os dois: veja de quem ela é."
                )
        if should_be_there and user.name and not _same_name(user.name, member.name):
            codes.append("name")
            problems.append(f"Nome no facial é \"{user.name}\".")
        if should_be_there:
            difference = photo_difference(our_photo, user.photo)
            if difference is not None and difference >= PHOTO_DIFF_WARN:
                codes.append("photo_diff")
                warnings.append("A foto do facial parece diferente da salva no sistema. Compare as duas.")
    if our_photo is None and should_be_there:
        codes.append("no_local_photo")
        warnings.append("A foto do sistema não está neste PC.")
    status = "erro" if problems else ("aviso" if warnings else "ok")
    return {
        "member_id": member.id,
        "name": member.name,
        "registration": member.registration,
        "blocked": bool(member.access_blocked or member.is_expired),
        "status": status,
        "problems": problems,
        "warnings": warnings,
        "codes": codes,
        "shared_with": shared,
        "face_id": user.face_id if user else None,
        "device_name": user.name if user else None,
        "difference": difference,
        "has_device_photo": bool(user and user.photo),
        "device_photo": _thumb(user.photo) if user and thumbnails else None,
    }


def _audit_device(
    pool: dict[str, str],
    members: list[db.Member],
    photos: dict[int, Optional[bytes]],
    thumbs: dict[int, Optional[str]],
    thumbnails: bool,
) -> dict[str, Any]:
    url = pool["url"]
    section: dict[str, Any] = {"url": url, "label": pool["label"], "ok": True, "rows": [], "orphans": []}
    users = IntelbrasClient(base_url=url).export_users(with_photos=True)
    if users is None:
        section["ok"] = False
        section["error"] = "Não foi possível ler o export deste facial. Confira se ele está ligado."
        return section
    by_registration = {u.user_id: u for u in users if u.user_id}
    known: set[str] = set()
    for member in members:
        chosen = selected_pools(member.pool) or registration_pools()
        if pool["id"] not in {item["id"] for item in chosen}:
            continue
        known.add(member.registration)
        row = _member_check(
            member, by_registration.get(member.registration), users, photos[member.id], thumbnails
        )
        row["our_photo"] = thumbs.get(member.id)
        section["rows"].append(row)
    # Sócio de outra piscina que ficou neste facial também é um usuário sobrando aqui.
    for user in users:
        # Reserva de ID não tem rosto nem sócio: é do sistema, não sobra.
        if user.user_id in known or is_reserve(user.user_id):
            continue
        others = [
            other.name or other.user_id
            for other in users
            if user.face_id and other.face_id == user.face_id and other.user_id != user.user_id
        ]
        section["orphans"].append(
            {
                "registration": user.user_id,
                "name": user.name,
                "face_id": user.face_id or None,
                "shared_with": others,
                "device_photo": _thumb(user.photo) if thumbnails else None,
            }
        )
    order = {"erro": 0, "aviso": 1, "ok": 2}
    section["rows"].sort(key=lambda row: (order[row["status"]], row["name"].casefold()))
    section["counts"] = {key: sum(1 for row in section["rows"] if row["status"] == key) for key in order}
    return section


def run_audit(*, thumbnails: bool = True) -> dict[str, Any]:
    """Uma seção por facial: cada sócio que deveria estar nele e os usuários sem cadastro.

    Os faciais são lidos ao mesmo tempo: o export de cada um demora alguns segundos.
    """
    members = db.list_members()
    photos = {member.id: _read_photo(member) for member in members}
    thumbs = {member_id: _thumb(data) for member_id, data in photos.items()} if thumbnails else {}
    pools = registration_pools()
    with ThreadPoolExecutor(max_workers=max(1, len(pools))) as executor:
        devices = list(executor.map(lambda pool: _audit_device(pool, members, photos, thumbs, thumbnails), pools))
    return {"ok": True, "devices": devices}
