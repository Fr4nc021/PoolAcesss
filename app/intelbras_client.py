from __future__ import annotations

import base64
import logging
import tarfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image

from app.config import (
    DEVICE_LOGIN,
    DEVICE_PASSWORD,
    DEVICE_URL,
    PORT,
    SERVER_LAN_IP,
    VALIDITY_DAYS,
)

logger = logging.getLogger(__name__)


class DeviceError(Exception):
    """Erro de comunicação ou resposta do equipamento facial."""


@dataclass
class CreatedUser:
    device_user_id: int
    registration: str
    face_ok: bool = True


class IntelbrasClient:
    """Cliente HTTP para Intelbras XPE / Akuvox (API /api/* com Basic Auth)."""

    def __init__(
        self,
        base_url: str = DEVICE_URL,
        login: str = DEVICE_LOGIN,
        password: str = DEVICE_PASSWORD,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login_name = login
        self.password = password
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            auth=(self.login_name, self.password),
            follow_redirects=True,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _check(self, data: dict[str, Any], action: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise DeviceError(f"Resposta inválida em {action}: {data!r}")
        retcode = data.get("retcode")
        if retcode not in (0, "0", 1, "1"):
            message = data.get("message") or data.get("action") or "erro"
            raise DeviceError(f"Equipamento rejeitou {action}: {message}")
        return data

    def ping(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(self._url("/api/system/info"))
                response.raise_for_status()
                return self._check(response.json(), "system/info")
        except httpx.HTTPError as exc:
            raise DeviceError(f"Não foi possível contatar o equipamento: {exc}") from exc

    def list_users(self) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.get(self._url("/api/user/get"))
            response.raise_for_status()
            data = self._check(response.json(), "user/get")
        items = ((data.get("data") or {}).get("item")) or []
        return list(items)

    def find_user_by_registration(self, registration: str) -> Optional[dict[str, Any]]:
        for user in self.list_users():
            if str(user.get("UserID")) == str(registration):
                return user
        return None

    def _face_looks_ok(self, registration: str) -> bool:
        user = self.find_user_by_registration(registration)
        if not user:
            return False
        face_id = str(user.get("FaceID") or "")
        status = user.get("FaceStatus")
        return status in (1, "1") or (
            "RegisterImg" in face_id and not face_id.endswith("/-1.jpg")
        )

    def create_user_with_face(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        reg = (registration or f"AP{int(time.time()) % 100000000:08d}")[:12]
        jpeg_path = _ensure_jpeg_file(image_path)

        # 1) Cria usuário sem FaceUrl (evita rejeição no add)
        payload = {
            "target": "user",
            "action": "add",
            "data": {
                "item": [
                    {
                        "Name": name.strip()[:60],
                        "UserID": reg,
                        "WebRelay": "0",
                        "LiftFloorNum": "9999",
                    }
                ]
            },
        }
        try:
            with self._client() as client:
                response = client.post(self._url("/api/user/add"), json=payload)
                response.raise_for_status()
                result = self._check(response.json(), "user/add")
        except httpx.HTTPError as exc:
            raise DeviceError(f"Falha ao cadastrar usuário no XPE: {exc}") from exc

        user = self.find_user_by_registration(reg)
        if not user:
            matches = [
                u
                for u in self.list_users()
                if str(u.get("Name", "")).strip() == name.strip()
            ]
            user = matches[-1] if matches else None
            if user and user.get("UserID"):
                reg = str(user["UserID"])
        if not user:
            raise DeviceError(
                f"Usuário criado ({reg}), mas não apareceu na lista do equipamento. "
                f"Resposta: {result}"
            )

        device_id = int(user["ID"])
        face_ok = False

        # 2) Tenta face: base64 e depois URL pública do nosso app
        for label, use_url in (("base64", False), ("url", True)):
            if use_url and not face_public_url:
                continue
            try:
                self.update_user(
                    device_user_id=device_id,
                    registration=reg,
                    name=name,
                    image_path=None if use_url else jpeg_path,
                    face_public_url=face_public_url if use_url else None,
                )
                time.sleep(1.2)
                if self._face_looks_ok(reg):
                    face_ok = True
                    logger.info("Face aceita via %s (reg=%s)", label, reg)
                    break
            except DeviceError as exc:
                logger.warning("Tentativa de face (%s) falhou: %s", label, exc)

        # 3) SEMPRE sincroniza via import TGZ — é o que faz o usuário aparecer
        #    corretamente em Moradores/Usuário (Validity=0, Frequency=0, FaceID).
        try:
            synced = self._attach_face_via_import(
                registration=reg,
                name=name.strip()[:60],
                jpeg_path=jpeg_path,
                device_user_id=device_id,
            )
            if synced:
                face_ok = True
                logger.info("Usuário sincronizado via import tgz (reg=%s)", reg)
            else:
                logger.warning("Import tgz concluiu, mas FaceStatus ainda não confirmou (reg=%s)", reg)
        except Exception as exc:
            logger.warning("Import de face/sincronização falhou: %s", exc)

        if not face_ok:
            logger.warning(
                "Usuário %s criado no XPE sem confirmação de FaceStatus. "
                "Verifique em Moradores > Usuário (busque a matrícula %s).",
                reg,
                reg,
            )

        refreshed = self.find_user_by_registration(reg) or user
        return CreatedUser(
            device_user_id=int(refreshed["ID"]),
            registration=reg,
            face_ok=face_ok,
        )

    def _attach_face_via_import(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
    ) -> bool:
        jpeg_bytes = Path(jpeg_path).read_bytes()
        with self._client() as client:
            exported = client.get(self._url("/api/user/export"))
            exported.raise_for_status()
            raw = exported.content

        with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as src:
            xml_member = src.extractfile("faceIDs/userdata.xml")
            if xml_member is None:
                raise DeviceError("Export do XPE sem userdata.xml")
            xml_bytes = xml_member.read()

        root = ET.fromstring(xml_bytes)
        keys = list(root.findall("Key"))
        max_face = 0
        target = None
        for key in keys:
            try:
                max_face = max(max_face, int(key.get("FaceID") or 0))
            except ValueError:
                pass
            if str(key.get("UserID")) == str(registration):
                target = key

        new_face = max_face + 1
        if target is None:
            ET.SubElement(
                root,
                "Key",
                {
                    "ID": str(device_user_id),
                    "FaceID": str(new_face),
                    "UserID": registration,
                    "Name": name,
                    "Card": "",
                    "Pin": "",
                    "Relay": "1",
                    "WebRelay": "0",
                    "Floor": "9999",
                    "Validity": "0",
                    "Frequency": "0",
                    "Mon": "0",
                    "Tue": "0",
                    "Wed": "0",
                    "Thur": "0",
                    "Fri": "0",
                    "Sat": "0",
                    "Sun": "0",
                    "TimeStart": "",
                    "TimeEnd": "",
                },
            )
        else:
            target.set("FaceID", str(new_face))
            target.set("Name", name)
            target.set("Validity", "0")
            target.set("Frequency", "0")
            target.set("Relay", target.get("Relay") or "1")
            target.set("WebRelay", "0")
            target.set("Floor", target.get("Floor") or "9999")

        new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        out = BytesIO()
        with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as src, tarfile.open(
            fileobj=out, mode="w:gz"
        ) as dst:
            for member in src.getmembers():
                if member.name == "faceIDs/userdata.xml":
                    info = tarfile.TarInfo(name=member.name)
                    info.size = len(new_xml)
                    info.mtime = int(time.time())
                    dst.addfile(info, BytesIO(new_xml))
                else:
                    fileobj = src.extractfile(member)
                    dst.addfile(member, fileobj)
            face_name = f"faceIDs/RegFaceMeun/{new_face}.jpg"
            info = tarfile.TarInfo(name=face_name)
            info.size = len(jpeg_bytes)
            info.mtime = int(time.time())
            dst.addfile(info, BytesIO(jpeg_bytes))

        package = out.getvalue()
        with self._client() as client:
            response = client.post(
                self._url("/api/user/import"),
                files={"file": ("UserData.tgz", package, "application/gzip")},
                data={"target": "user", "action": "import"},
            )
            response.raise_for_status()
            self._check(response.json(), "user/import")

        time.sleep(2.0)
        return self._face_looks_ok(registration)

    def update_user(
        self,
        *,
        device_user_id: int,
        registration: str,
        name: Optional[str] = None,
        image_path: Optional[Path] = None,
        face_public_url: Optional[str] = None,
    ) -> None:
        item: dict[str, Any] = {
            "ID": str(device_user_id),
            "UserID": registration,
            "WebRelay": "0",
            "LiftFloorNum": "9999",
        }
        if name:
            item["Name"] = name.strip()[:60]
        if face_public_url:
            item["FaceUrl"] = face_public_url
        elif image_path:
            item["FaceUrl"] = _prepare_jpeg_base64(_ensure_jpeg_file(image_path))

        payload = {"target": "user", "action": "set", "data": {"item": [item]}}
        try:
            with self._client() as client:
                response = client.post(self._url("/api/user/set"), json=payload)
                response.raise_for_status()
                self._check(response.json(), "user/set")
        except httpx.HTTPError as exc:
            raise DeviceError(f"Falha ao atualizar usuário: {exc}") from exc

    def delete_user(
        self,
        *,
        registration: Optional[str] = None,
        device_user_id: Optional[int] = None,
    ) -> None:
        item: dict[str, Any] = {}
        if registration:
            item["UserID"] = registration
        elif device_user_id is not None:
            item["ID"] = str(device_user_id)
        else:
            raise DeviceError("Informe registration ou device_user_id para excluir.")

        payload = {"target": "user", "action": "del", "data": {"item": [item]}}
        try:
            with self._client() as client:
                response = client.post(self._url("/api/user/del"), json=payload)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and str(data.get("message", "")).lower() in {
                    "user not exist",
                    "not exist",
                }:
                    return
                self._check(data, "user/del")
        except httpx.HTTPError as exc:
            raise DeviceError(f"Falha ao remover usuário: {exc}") from exc

    def block_user(self, device_user_id: int, registration: Optional[str] = None) -> None:
        if registration:
            self.delete_user(registration=registration)
            return
        user = None
        for item in self.list_users():
            if int(item.get("ID", -1)) == int(device_user_id):
                user = item
                break
        if user and user.get("UserID"):
            self.delete_user(registration=str(user["UserID"]))
        else:
            self.delete_user(device_user_id=device_user_id)

    def list_door_logs(self, *, max_items: int = 200) -> list[dict[str, Any]]:
        """Busca logs de acesso do XPE (melhor esforço entre endpoints comuns)."""
        candidates = [
            ("/api/doorlog/get", {"Action": "list"}),
            ("/api/doorlog/get", {}),
            ("/api/log/get", {"target": "doorlog"}),
        ]
        last_error: Optional[Exception] = None
        # Timeout curto: ADM não deve travar se o XPE estiver offline
        with httpx.Client(
            timeout=8.0,
            auth=(self.login_name, self.password),
            follow_redirects=True,
        ) as client:
            for path, params in candidates:
                try:
                    response = client.get(self._url(path), params=params)
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        continue
                    payload = data.get("data") or data
                    items = payload.get("item") if isinstance(payload, dict) else None
                    if items is None and isinstance(payload, list):
                        items = payload
                    if not isinstance(items, list):
                        continue
                    return list(items)[:max_items]
                except Exception as exc:  # noqa: BLE001 — tenta próximo endpoint
                    last_error = exc
                    logger.debug("doorlog via %s falhou: %s", path, exc)
        if last_error:
            logger.warning("Não foi possível ler logs do XPE: %s", last_error)
        return []

    def update_member_face(
        self,
        *,
        name: str,
        registration: str,
        image_path: Path,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        """Atualiza a foto facial de um membro já cadastrado (ou recria se sumiu)."""
        self.ping()
        jpeg_path = _ensure_jpeg_file(image_path)
        existing = self.find_user_by_registration(registration)
        if existing:
            device_id = int(existing["ID"])
            try:
                self.update_user(
                    device_user_id=device_id,
                    registration=registration,
                    name=name,
                    image_path=jpeg_path,
                    face_public_url=face_public_url,
                )
            except DeviceError as exc:
                logger.warning("update_user na troca de foto falhou: %s", exc)
            try:
                self._attach_face_via_import(
                    registration=registration,
                    name=name.strip()[:60],
                    jpeg_path=jpeg_path,
                    device_user_id=device_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import na troca de foto falhou: %s", exc)
            return CreatedUser(
                device_user_id=device_id,
                registration=registration,
                face_ok=self._face_looks_ok(registration),
            )
        return self.create_user_with_face(
            name,
            jpeg_path,
            registration=registration,
            face_public_url=face_public_url,
        )

    def register_member(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        validity_days: int = VALIDITY_DAYS,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        _ = validity_days
        self.ping()
        return self.create_user_with_face(
            name,
            image_path,
            registration=registration,
            face_public_url=face_public_url,
        )

    def renew_member(
        self,
        *,
        name: str,
        registration: str,
        image_path: Optional[Path],
        previous_device_user_id: Optional[int] = None,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        self.ping()
        existing = self.find_user_by_registration(registration)
        if existing:
            if image_path or face_public_url:
                self.update_user(
                    device_user_id=int(existing["ID"]),
                    registration=registration,
                    name=name,
                    image_path=image_path,
                    face_public_url=face_public_url,
                )
            return CreatedUser(
                device_user_id=int(existing["ID"]),
                registration=registration,
                face_ok=True,
            )

        if not image_path or not Path(image_path).exists():
            raise DeviceError(
                "Exame renovado, mas a foto salva não foi encontrada para recriar o acesso facial."
            )
        if previous_device_user_id is not None:
            try:
                self.delete_user(registration=registration)
            except DeviceError:
                pass
        return self.create_user_with_face(
            name,
            Path(image_path),
            registration=registration,
            face_public_url=face_public_url,
        )


def public_media_url(filename: str) -> str:
    return f"http://{SERVER_LAN_IP}:{PORT}/media/{filename}"


def _ensure_jpeg_file(image_path: Path) -> Path:
    path = Path(image_path)
    if not path.exists():
        raise DeviceError(f"Arquivo de foto não encontrado: {path}")

    dest = path if path.suffix.lower() in {".jpg", ".jpeg"} else path.with_suffix(".jpg")
    try:
        with Image.open(path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            if min(w, h) < 250:
                scale = 250 / float(min(w, h))
                img = img.resize(
                    (max(250, int(w * scale)), max(250, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            max_side = 800
            w, h = img.size
            if max(w, h) > max_side:
                ratio = max_side / float(max(w, h))
                img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
            img.save(dest, format="JPEG", quality=92)
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(f"Não foi possível processar a imagem: {exc}") from exc

    if dest.stat().st_size > 2 * 1024 * 1024:
        raise DeviceError("Foto maior que 2MB após compressão. Use outra imagem.")
    return dest


def _prepare_jpeg_base64(image_path: Path) -> str:
    path = _ensure_jpeg_file(image_path)
    data = path.read_bytes()
    if len(data) < 1000:
        raise DeviceError("Arquivo de foto inválido ou muito pequeno.")
    return base64.b64encode(data).decode("ascii")
