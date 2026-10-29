from __future__ import annotations

import base64
import hashlib
import logging
import os
import tarfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from PIL import Image, ImageOps

from app.config import (
    DOOR_NUM,
    PORT,
    SERVER_LAN_IP,
    VALIDITY_DAYS,
    configured_device_urls,
    registration_pools,
    selected_pools,
)

# Agenda 1001 é a opção embutida "Sempre" do XPE. O número depois do hífen é o relé permitido.
ALWAYS_SCHEDULE_RELAY = f"1001-{DOOR_NUM}"
# Termo de validade na tela: 0 = Sempre, 1 = Nunca, 2 = Agenda.
FACE_VALIDITY_ALWAYS = "0"
FACE_SEND_ROUNDS = 3
FACE_CONFIRM_RETRIES = 8
FACE_CONFIRM_DELAY = 1.5
FACE_MIN_SIDE = 250
FACE_MAX_SIDE = 800
FACE_MIN_BYTES = 1000
FACE_MAX_BYTES = 2 * 1024 * 1024
DELETE_CONFIRM_RETRIES = 4
DELETE_CONFIRM_DELAY = 1.0
USER_PAGE_LIMIT = 50

# Modelo do rosto no export: () = sem modelo, None = export indisponível.
FaceSignature = Optional[tuple[str, ...]]

FACE_NOT_ACCEPTED_MSG = (
    "A foto não foi aceita pelo equipamento Intelbras. "
    "Tire outra foto com o rosto de frente, bem iluminado, "
    "sem boné ou óculos escuro, e tente cadastrar de novo."
)

logger = logging.getLogger(__name__)


class DeviceError(Exception):
    """Erro de comunicação ou resposta do equipamento facial."""


class FaceIDConflict(DeviceError):
    """O rosto do sócio ficou no mesmo FaceID de outro usuário do facial.

    O facial reconhece pelo FaceID: com dois usuários nele, o rosto de um
    libera a entrada no nome do outro.
    """


@dataclass
class ExportUser:
    user_id: str
    name: str
    face_id: str
    has_model: bool
    photo: Optional[bytes]


@dataclass
class CreatedUser:
    device_user_id: int
    registration: str
    face_ok: bool = True


class IntelbrasClient:
    """Cliente HTTP para Intelbras XPE / Akuvox (API /api/* com Basic Auth)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        login: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        # Sem valor, lê o env.txt agora: a aba Configurações muda login e senha sem reiniciar.
        from app.config import reload_env

        reload_env()
        if base_url is None:
            base_url = os.getenv("DEVICE_URL", "")
        self.base_url = base_url.rstrip("/")
        self.login_name = login if login is not None else os.getenv("DEVICE_LOGIN", "admin")
        self.password = password if password is not None else os.getenv("DEVICE_PASSWORD", "")
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
        """Lista todos os usuários. Se o facial paginar a resposta, busca as outras páginas."""
        try:
            with self._client() as client:
                response = client.get(self._url("/api/user/get"))
                response.raise_for_status()
                data = self._check(response.json(), "user/get")
                payload = data.get("data") or {}
                items = list(payload.get("item") or [])
                total = _int_or_none(payload.get("num") or payload.get("total"))
                page = 1
                while total is not None and len(items) < total and page < USER_PAGE_LIMIT:
                    page += 1
                    response = client.get(self._url("/api/user/get"), params={"page": page})
                    response.raise_for_status()
                    more = (self._check(response.json(), "user/get").get("data") or {}).get("item")
                    known = {str(item.get("ID")) for item in items}
                    fresh = [item for item in (more or []) if str(item.get("ID")) not in known]
                    if not fresh:
                        break
                    items.extend(fresh)
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceError(f"Não foi possível listar os usuários do facial: {exc}") from exc
        return items

    def find_user_by_registration(self, registration: str) -> Optional[dict[str, Any]]:
        for user in self.list_users():
            if str(user.get("UserID")) == str(registration):
                return user
        return None

    def _export_archive(self) -> bytes:
        with self._client() as client:
            response = client.get(self._url("/api/user/export"))
            response.raise_for_status()
            return response.content

    def export_users(self, *, with_photos: bool = False) -> Optional[list[ExportUser]]:
        """Usuários do export com FaceID, se têm modelo do rosto e a foto guardada.

        O export traz todos os usuários de uma vez, sem paginação, então é a
        fonte confiável para saber quais FaceIDs já estão em uso. A foto do
        FaceID fica em faceIDs/RegFaceMeun/<FaceID>.jpg. None = export indisponível.
        """
        try:
            raw = self._export_archive()
            with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
                xml_member = archive.extractfile("faceIDs/userdata.xml")
                if xml_member is None:
                    return None
                root = ET.fromstring(xml_member.read())
                model_ids: set[str] = set()
                photos: dict[str, bytes] = {}
                for item in archive.getmembers():
                    if not (item.isfile() and item.size > 0):
                        continue
                    parts = item.name.split("/")
                    if len(parts) >= 3 and parts[0] == "faceIDs" and "-" in parts[1]:
                        model_ids.add(parts[1].split("-", 1)[0])
                    elif with_photos and item.name.startswith("faceIDs/RegFaceMeun/"):
                        fileobj = archive.extractfile(item)
                        if fileobj is not None:
                            photos[Path(item.name).stem] = fileobj.read()
        except (httpx.HTTPError, tarfile.TarError, ET.ParseError, OSError, EOFError) as exc:
            logger.warning("Não foi possível ler o export do facial %s: %s", self.base_url, exc)
            return None
        users: list[ExportUser] = []
        for key in root.findall("Key"):
            face_id = _clean_face_id(key.get("FaceID"))
            users.append(
                ExportUser(
                    user_id=str(key.get("UserID") or ""),
                    name=str(key.get("Name") or ""),
                    face_id=face_id,
                    has_model=bool(face_id) and face_id in model_ids,
                    photo=photos.get(face_id) if face_id else None,
                )
            )
        return users

    def face_shared_with(self, registration: str) -> list[str]:
        """Nomes dos outros usuários deste facial que estão no mesmo FaceID da matrícula."""
        users = self.export_users()
        if users is None:
            users = [
                ExportUser(
                    user_id=str(user.get("UserID") or ""),
                    name=str(user.get("Name") or ""),
                    face_id=_clean_face_id(user.get("FaceID")),
                    has_model=False,
                    photo=None,
                )
                for user in self.list_users()
            ]
        face_id = next((u.face_id for u in users if u.user_id == str(registration)), "")
        if not face_id:
            return []
        return [
            u.name or u.user_id
            for u in users
            if u.face_id == face_id and u.user_id != str(registration)
        ]

    def _face_signature(self, registration: str) -> FaceSignature:
        """Modelo do rosto que o facial gerou para a matrícula.

        O facial só reconhece quem tem a pasta faceIDs/<FaceID>-<nome>/ no
        export. Ter FaceID ou a foto em RegFaceMeun não basta: a foto fica lá
        sem modelo quando o facial não achou o rosto, e o leitor responde
        "usuário desconhecido".
        """
        try:
            raw = self._export_archive()
            with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
                xml_member = archive.extractfile("faceIDs/userdata.xml")
                if xml_member is None:
                    return None
                root = ET.fromstring(xml_member.read())
                face_id = next(
                    (
                        str(key.get("FaceID") or "")
                        for key in root.findall("Key")
                        if str(key.get("UserID")) == str(registration)
                    ),
                    "",
                )
                if face_id in ("", "0"):
                    return ()
                # O mtime no export é a hora do export e as partes têm tamanho fixo:
                # só o conteúdo diz se o modelo mudou.
                prefix = f"faceIDs/{face_id}-"
                digest = hashlib.sha256()
                found = False
                for item in sorted(archive.getmembers(), key=lambda entry: entry.name):
                    if not (item.isfile() and item.size > 0 and item.name.startswith(prefix)):
                        continue
                    fileobj = archive.extractfile(item)
                    if fileobj is None:
                        continue
                    digest.update(fileobj.read())
                    found = True
        except (httpx.HTTPError, tarfile.TarError, ET.ParseError, OSError, EOFError) as exc:
            logger.warning("Não foi possível ler o export do facial %s: %s", self.base_url, exc)
            return None
        return (face_id, digest.hexdigest()) if found else ()

    def _face_confirmed(
        self,
        registration: str,
        *,
        previous: FaceSignature = (),
        retries: int = FACE_CONFIRM_RETRIES,
    ) -> bool:
        """Confirma que o facial gerou um modelo novo do rosto para a matrícula."""
        for attempt in range(retries):
            user = self.find_user_by_registration(registration)
            if user and _face_assigned(user):
                signature = self._face_signature(registration)
                if signature is None:
                    # Sem export não dá para ver o modelo: só vale o FaceStatus do facial.
                    if user.get("FaceStatus") in (1, "1", True):
                        return True
                elif signature and signature != previous:
                    return True
            if attempt + 1 < retries:
                time.sleep(FACE_CONFIRM_DELAY)
        return False

    def _face_attempts(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
        face_public_url: Optional[str],
    ) -> list[tuple[str, Callable[[], None]]]:
        attempts: list[tuple[str, Callable[[], None]]] = []
        if face_public_url and face_public_url.startswith("http://"):
            attempts.append(
                (
                    "url",
                    lambda: self.update_user(
                        device_user_id=device_user_id,
                        registration=registration,
                        name=name,
                        face_public_url=face_public_url,
                    ),
                )
            )
        # O import do UserData.tgz grava a foto sem gerar o modelo do rosto, então não entra aqui.
        attempts.extend(
            [
                (
                    "register_img",
                    lambda: self._send_face_via_register_img(
                        registration=registration,
                        name=name,
                        jpeg_path=jpeg_path,
                        device_user_id=device_user_id,
                    ),
                ),
                (
                    "base64",
                    lambda: self.update_user(
                        device_user_id=device_user_id,
                        registration=registration,
                        name=name,
                        image_path=jpeg_path,
                    ),
                ),
                (
                    "base64_data_uri",
                    lambda: self._send_face_via_data_uri(
                        device_user_id=device_user_id,
                        registration=registration,
                        name=name,
                        jpeg_path=jpeg_path,
                    ),
                ),
                (
                    "multipart",
                    lambda: self._send_face_via_multipart(
                        device_user_id=device_user_id,
                        registration=registration,
                        name=name,
                        jpeg_path=jpeg_path,
                    ),
                ),
            ]
        )
        return attempts

    def create_user_with_face(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        face_public_url: Optional[str] = None,
        image_ready: bool = False,
    ) -> CreatedUser:
        reg = (registration or f"AP{int(time.time()) % 100000000:08d}")[:12]
        jpeg_path = image_path if image_ready else _ensure_jpeg_file(image_path)

        # Cria usuário sem FaceUrl (evita rejeição no add)
        payload = {
            "target": "user",
            "action": "add",
            "data": {
                "item": [
                    {
                        "Name": name.strip()[:60],
                        "UserID": reg,
                        "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
                        "WebRelay": "0",
                        "LiftFloorNum": "9999",
                        "Validity": FACE_VALIDITY_ALWAYS,
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
        try:
            face_ok = self._send_face(
                registration=reg,
                name=name,
                jpeg_path=jpeg_path,
                device_user_id=device_id,
                face_public_url=face_public_url,
            )
        except FaceIDConflict:
            try:
                self.delete_user(registration=reg)
            except DeviceError as exc:
                logger.warning("Rollback do usuário %s falhou: %s", reg, exc)
            raise

        try:
            self._grant_always_access(
                device_user_id=device_id,
                registration=reg,
                name=name,
                current=user,
            )
        except DeviceError as exc:
            logger.warning("Relé permitido / validade sempre não confirmados: %s", exc)

        if not face_ok:
            logger.error(
                "Face não confirmada para %s — removendo cadastro incompleto do XPE",
                reg,
            )
            try:
                self.delete_user(registration=reg)
            except DeviceError as exc:
                logger.warning("Rollback do usuário %s falhou: %s", reg, exc)
            raise DeviceError(FACE_NOT_ACCEPTED_MSG)

        return CreatedUser(
            device_user_id=device_id,
            registration=reg,
            face_ok=True,
        )

    def _send_face(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
        face_public_url: Optional[str] = None,
    ) -> bool:
        """Tenta vários métodos, em rodadas, até o XPE gerar o modelo do rosto."""
        previous = self._face_signature(registration) or ()
        attempts = self._face_attempts(
            registration=registration,
            name=name,
            jpeg_path=jpeg_path,
            device_user_id=device_user_id,
            face_public_url=face_public_url,
        )
        for round_num in range(FACE_SEND_ROUNDS):
            for label, action in attempts:
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 — tenta o próximo método
                    logger.warning(
                        "Face via %s (rodada %s) falhou (reg=%s): %s",
                        label,
                        round_num + 1,
                        registration,
                        exc,
                    )
                    continue
                if self._face_confirmed(registration, previous=previous):
                    others = self.face_shared_with(registration)
                    if others:
                        # Outro método não muda o FaceID do usuário: para aqui.
                        logger.error(
                            "Face de %s (reg=%s) ficou no FaceID de %s em %s",
                            name,
                            registration,
                            ", ".join(others),
                            self.base_url,
                        )
                        raise FaceIDConflict(
                            f"O facial {self.base_url} gravou o rosto de {name} no mesmo número "
                            f"de rosto (FaceID) de {', '.join(others)}. Nesse estado o rosto de um "
                            "libera a entrada no nome do outro. Use o botão Foto em cada um para "
                            "refazer, ou confira em ADM > Conferir fotos no Intelbras."
                        )
                    logger.info(
                        "Face confirmada via %s na rodada %s (reg=%s)",
                        label,
                        round_num + 1,
                        registration,
                    )
                    return True
                logger.warning(
                    "%s aceito na rodada %s, mas o facial não gerou o modelo do rosto (reg=%s)",
                    label,
                    round_num + 1,
                    registration,
                )
            if round_num + 1 < FACE_SEND_ROUNDS:
                time.sleep(1.5)
        return False

    def _send_face_via_data_uri(
        self,
        *,
        device_user_id: int,
        registration: str,
        name: str,
        jpeg_path: Path,
    ) -> None:
        encoded = _prepare_jpeg_base64(jpeg_path)
        self.update_user(
            device_user_id=device_user_id,
            registration=registration,
            name=name,
            face_public_url=f"data:image/jpeg;base64,{encoded}",
        )

    def _next_face_id(self) -> int:
        """FaceID que ninguém usa neste facial.

        Só a lista da API pode vir cortada pela paginação, e um FaceID de outro
        sócio fazia o rosto novo liberar no nome dele. Sem o export, que traz
        todos, este método de envio não é usado.
        """
        exported = self.export_users()
        if exported is None:
            raise DeviceError("Sem o export do facial não dá para escolher um FaceID livre.")
        used = {u.face_id for u in exported if u.face_id}
        used.update(_clean_face_id(user.get("FaceID")) for user in self.list_users())
        max_face = 0
        for face_id in used:
            try:
                max_face = max(max_face, int(face_id))
            except (TypeError, ValueError):
                pass
        return max_face + 1

    def _put_register_img(self, face_id: int, jpeg_bytes: bytes) -> str:
        path = f"/Image/RegisterImg/{face_id}.jpg"
        last_error: Optional[Exception] = None
        with self._client() as client:
            for verb in (client.put, client.post):
                try:
                    response = verb(
                        self._url(path),
                        content=jpeg_bytes,
                        headers={"Content-Type": "image/jpeg"},
                    )
                    if response.status_code in {200, 201, 204}:
                        return f"{self.base_url}{path}"
                    last_error = DeviceError(
                        f"HTTP {response.status_code}: {response.text[:120]}"
                    )
                except httpx.HTTPError as exc:
                    last_error = exc
        raise DeviceError(f"Não foi possível gravar {path}: {last_error}")

    def _send_face_via_register_img(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
    ) -> None:
        face_id = self._next_face_id()
        img_url = self._put_register_img(face_id, Path(jpeg_path).read_bytes())
        self.update_user(
            device_user_id=device_user_id,
            registration=registration,
            name=name,
            face_public_url=img_url,
        )

    def _send_face_via_multipart(
        self,
        *,
        device_user_id: int,
        registration: str,
        name: str,
        jpeg_path: Path,
    ) -> None:
        jpeg_bytes = Path(jpeg_path).read_bytes()
        form_base = {
            "target": "user",
            "action": "set",
            "ID": str(device_user_id),
            "UserID": registration,
            "Name": name.strip()[:60],
        }
        last_error: Optional[Exception] = None
        with self._client() as client:
            for field in ("Face", "face", "File", "file", "image", "Image"):
                try:
                    response = client.post(
                        self._url("/api/user/set"),
                        data=form_base,
                        files={field: ("face.jpg", jpeg_bytes, "image/jpeg")},
                    )
                    response.raise_for_status()
                    self._check(response.json(), f"user/set multipart ({field})")
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
        raise DeviceError(f"Upload multipart da face rejeitado: {last_error}")

    def _grant_always_access(
        self,
        *,
        device_user_id: int,
        registration: str,
        name: Optional[str] = None,
        current: Optional[dict[str, Any]] = None,
    ) -> None:
        """Seleciona relé permitido e termo de validade Sempre."""
        known = current if current is not None else (self.find_user_by_registration(registration) or {})
        item: dict[str, Any] = {
            "ID": str(known.get("ID") or device_user_id),
            "UserID": registration,
            "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
            "WebRelay": "0",
            "LiftFloorNum": "9999",
            "Validity": FACE_VALIDITY_ALWAYS,
        }
        resolved_name = (name or str(known.get("Name") or "")).strip()
        if resolved_name:
            item["Name"] = resolved_name[:60]

        payload = {"target": "user", "action": "set", "data": {"item": [item]}}
        try:
            with self._client() as client:
                response = client.post(self._url("/api/user/set"), json=payload)
                response.raise_for_status()
                self._check(response.json(), "user/set acesso")
        except httpx.HTTPError as exc:
            raise DeviceError(f"Falha ao liberar relé e validade sempre: {exc}") from exc

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
            "ScheduleRelay": ALWAYS_SCHEDULE_RELAY,
            "WebRelay": "0",
            "LiftFloorNum": "9999",
            "Validity": FACE_VALIDITY_ALWAYS,
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

    def _find_user(
        self,
        *,
        registration: Optional[str],
        device_user_id: Optional[int],
    ) -> Optional[dict[str, Any]]:
        for user in self.list_users():
            if registration:
                if str(user.get("UserID")) == str(registration):
                    return user
            elif str(user.get("ID")) == str(device_user_id):
                return user
        return None

    def _post_delete(self, item: dict[str, Any]) -> None:
        payload = {"target": "user", "action": "del", "data": {"item": [item]}}
        try:
            with self._client() as client:
                response = client.post(self._url("/api/user/del"), json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceError(f"Falha ao remover usuário: {exc}") from exc
        if isinstance(data, dict) and str(data.get("message", "")).lower() in {
            "user not exist",
            "not exist",
        }:
            return
        self._check(data, "user/del")

    def delete_user(
        self,
        *,
        registration: Optional[str] = None,
        device_user_id: Optional[int] = None,
    ) -> None:
        """Apaga o usuário e confere na lista do facial que ele sumiu.

        O facial pode responder OK sem apagar, então só retorna quando a
        matrícula não aparece mais na lista. O ID interno muda de um facial
        para o outro: com matrícula, ele é sempre lido deste aparelho.
        """
        if not registration and device_user_id is None:
            raise DeviceError("Informe registration ou device_user_id para excluir.")
        user = self._find_user(registration=registration, device_user_id=device_user_id)
        if user is None:
            if registration:
                # Garante a exclusão mesmo se a lista vier incompleta.
                try:
                    self._post_delete({"UserID": registration})
                except DeviceError as exc:
                    logger.debug("Exclusão de %s fora da lista: %s", registration, exc)
            return

        internal_id = str(user.get("ID") or "")
        user_id = str(user.get("UserID") or registration or "")
        payloads = [
            item
            for item in (
                {"ID": internal_id} if internal_id else None,
                {"UserID": user_id} if user_id else None,
                {"ID": internal_id, "UserID": user_id} if internal_id and user_id else None,
            )
            if item
        ]
        last_error: Optional[Exception] = None
        for item in payloads:
            try:
                self._post_delete(item)
            except DeviceError as exc:
                last_error = exc
                continue
            for attempt in range(DELETE_CONFIRM_RETRIES):
                if self._find_user(registration=registration, device_user_id=device_user_id) is None:
                    logger.info("Usuário %s apagado do facial %s", user_id or internal_id, self.base_url)
                    return
                if attempt + 1 < DELETE_CONFIRM_RETRIES:
                    time.sleep(DELETE_CONFIRM_DELAY)
        detail = f": {last_error}" if last_error else ""
        raise DeviceError(
            f"O facial {self.base_url} não apagou o usuário {user_id or internal_id}{detail}"
        )

    def block_user(self, device_user_id: int, registration: Optional[str] = None) -> None:
        if registration:
            self.delete_user(registration=registration)
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

    def _drop_shared_face(self, registration: str, name: str) -> None:
        """Apaga do facial o usuário que divide o FaceID com outro, para recriar do zero.

        Reenviar a foto no mesmo usuário mantém o FaceID repetido e troca o
        rosto do outro sócio junto. Recriado, o usuário recebe um FaceID novo.
        """
        logger.warning(
            "%s (reg=%s) divide o FaceID com outro usuário em %s: recriando",
            name,
            registration,
            self.base_url,
        )
        self.delete_user(registration=registration)
        return None

    def update_member_face(
        self,
        *,
        name: str,
        registration: str,
        image_path: Path,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        """Atualiza a foto facial de um membro já cadastrado (ou recria se sumiu)."""
        jpeg_path = _ensure_jpeg_file(image_path)
        existing = self.find_user_by_registration(registration)
        if existing and self.face_shared_with(registration):
            existing = self._drop_shared_face(registration, name)
        if existing:
            device_id = int(existing["ID"])
            face_ok = self._send_face(
                registration=registration,
                name=name,
                jpeg_path=jpeg_path,
                device_user_id=device_id,
                face_public_url=face_public_url,
            )
            try:
                self._grant_always_access(
                    device_user_id=device_id,
                    registration=registration,
                    name=name,
                    current=existing,
                )
            except DeviceError as exc:
                logger.warning("Relé permitido / validade sempre não confirmados: %s", exc)
            if not face_ok:
                raise DeviceError(FACE_NOT_ACCEPTED_MSG)
            return CreatedUser(
                device_user_id=device_id,
                registration=registration,
                face_ok=True,
            )
        return self.create_user_with_face(
            name,
            jpeg_path,
            registration=registration,
            face_public_url=face_public_url,
            image_ready=True,
        )

    def register_member(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        validity_days: int = VALIDITY_DAYS,
        face_public_url: Optional[str] = None,
        image_ready: bool = False,
    ) -> CreatedUser:
        _ = validity_days
        return self.create_user_with_face(
            name,
            image_path,
            registration=registration,
            face_public_url=face_public_url,
            image_ready=image_ready,
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
        if existing and self.face_shared_with(registration):
            if not image_path or not Path(image_path).exists():
                raise DeviceError(
                    f"{name} está no mesmo número de rosto (FaceID) de outro usuário no facial "
                    f"{self.base_url}, e a foto salva não está neste PC. Envie uma foto nova pelo botão Foto."
                )
            existing = self._drop_shared_face(registration, name)
        if existing:
            device_id = int(existing["ID"])
            face_ok = True
            # Quem já tem o modelo do rosto no facial não precisa reenviar a foto.
            has_model = bool((image_path or face_public_url) and self._face_signature(registration))
            if image_path and not has_model:
                face_ok = self._send_face(
                    registration=registration,
                    name=name,
                    jpeg_path=_ensure_jpeg_file(image_path),
                    device_user_id=device_id,
                    face_public_url=face_public_url,
                )
            elif face_public_url and not has_model:
                self.update_user(
                    device_user_id=device_id,
                    registration=registration,
                    name=name,
                    face_public_url=face_public_url,
                )
                if not self._face_confirmed(registration):
                    raise DeviceError(FACE_NOT_ACCEPTED_MSG)
            try:
                self._grant_always_access(
                    device_user_id=device_id,
                    registration=registration,
                    name=name,
                    current=existing,
                )
            except DeviceError as exc:
                logger.warning("Relé permitido / validade sempre não confirmados: %s", exc)
            if image_path and not face_ok:
                raise DeviceError(FACE_NOT_ACCEPTED_MSG)
            return CreatedUser(
                device_user_id=device_id,
                registration=registration,
                face_ok=True,
            )

        if not image_path or not Path(image_path).exists():
            raise DeviceError(
                "A foto salva não foi encontrada para recriar o acesso facial. "
                "Envie uma foto nova pelo botão Foto."
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


def device_clients() -> list[IntelbrasClient]:
    urls = configured_device_urls()
    if not urls:
        return [IntelbrasClient()]
    return [IntelbrasClient(base_url=url) for url in urls]


def fleet_for_pool(pool: Optional[str]) -> DeviceFleet:
    """Uma piscina vai para o facial dela. As duas, ou cadastro antigo, vão para os dois."""
    chosen = selected_pools(pool)
    if not chosen or len(chosen) >= len(registration_pools()):
        return DeviceFleet()
    return DeviceFleet([IntelbrasClient(base_url=chosen[0]["url"])])


class DeviceFleet:
    """Envia o mesmo cadastro para todos os faciais configurados."""

    def __init__(self, clients: Optional[list[IntelbrasClient]] = None) -> None:
        self.clients = clients if clients is not None else device_clients()

    def _label(self, client: IntelbrasClient) -> str:
        return client.base_url or "equipamento"

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for client in self.clients:
            try:
                info = client.ping()
                model = ((info.get("data") or {}).get("Status") or {}).get("Model")
                rows.append(
                    {
                        "url": client.base_url,
                        "ok": True,
                        "model": model or "conectado",
                    }
                )
            except DeviceError as exc:
                rows.append(
                    {
                        "url": client.base_url,
                        "ok": False,
                        "model": None,
                        "error": str(exc),
                    }
                )
        return rows

    def register_member(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        validity_days: int = VALIDITY_DAYS,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        if not self.clients:
            raise DeviceError("Nenhum facial configurado (DEVICE_URL).")

        shared_registration = (registration or f"AP{int(time.time()) % 100000000:08d}")[:12]
        jpeg_path = _ensure_jpeg_file(image_path)
        targets = ", ".join(self._label(client) for client in self.clients)
        print(f"Cadastrando em: {targets}", flush=True)

        def _send(client: IntelbrasClient) -> CreatedUser:
            print(f"Enviando cadastro para {self._label(client)} ...", flush=True)
            try:
                result = client.register_member(
                    name,
                    jpeg_path,
                    registration=shared_registration,
                    validity_days=validity_days,
                    face_public_url=face_public_url,
                    image_ready=True,
                )
            except DeviceError as exc:
                print(f"Falhou em {self._label(client)}: {exc}", flush=True)
                raise DeviceError(f"{self._label(client)}: {exc}") from exc
            print(f"Cadastro aceito em {self._label(client)}", flush=True)
            return result

        results: dict[int, CreatedUser] = {}
        errors: list[str] = []
        try:
            with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as pool:
                futures = {
                    pool.submit(_send, client): index
                    for index, client in enumerate(self.clients)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 — um aparelho não pode abortar o outro
                        errors.append(str(exc))
            if errors:
                raise DeviceError("; ".join(errors))
        except DeviceError:
            for client in self.clients:
                try:
                    client.delete_user(registration=shared_registration)
                except DeviceError as exc:
                    logger.warning(
                        "Rollback do cadastro falhou em %s: %s",
                        client.base_url,
                        exc,
                    )
            raise

        created = results.get(0)
        if created is None:
            raise DeviceError("Nenhum facial recebeu o cadastro.")
        if any(not item.face_ok for item in results.values()):
            created.face_ok = False
        return created

    def renew_member(
        self,
        *,
        name: str,
        registration: str,
        image_path: Optional[Path],
        previous_device_user_id: Optional[int] = None,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        return self._map_user(
            "renovar",
            lambda client: client.renew_member(
                name=name,
                registration=registration,
                image_path=image_path,
                previous_device_user_id=previous_device_user_id,
                face_public_url=face_public_url,
            ),
        )

    def update_member_face(
        self,
        *,
        name: str,
        registration: str,
        image_path: Path,
        face_public_url: Optional[str] = None,
    ) -> CreatedUser:
        return self._map_user(
            "atualizar a foto",
            lambda client: client.update_member_face(
                name=name,
                registration=registration,
                image_path=image_path,
                face_public_url=face_public_url,
            ),
        )

    def block_user(self, device_user_id: int, registration: Optional[str] = None) -> None:
        errors: list[str] = []
        for client in self.clients:
            try:
                client.block_user(
                    device_user_id=device_user_id,
                    registration=registration,
                )
            except DeviceError as exc:
                errors.append(f"{self._label(client)}: {exc}")
        if errors:
            raise DeviceError("Falha ao bloquear em " + "; ".join(errors))

    def delete_user(
        self,
        *,
        registration: Optional[str] = None,
        device_user_id: Optional[int] = None,
    ) -> None:
        errors: list[str] = []
        for client in self.clients:
            try:
                client.delete_user(
                    registration=registration,
                    device_user_id=device_user_id,
                )
            except DeviceError as exc:
                errors.append(f"{self._label(client)}: {exc}")
        if errors:
            raise DeviceError("Falha ao excluir em " + "; ".join(errors))

    def list_door_logs(self, *, max_items: int = 200) -> list[tuple[str, dict[str, Any]]]:
        """Logs de cada facial. O prefixo evita colisão de ID entre aparelhos."""
        merged: list[tuple[str, dict[str, Any]]] = []
        for index, client in enumerate(self.clients):
            try:
                items = client.list_door_logs(max_items=max_items)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sync de logs falhou em %s: %s", client.base_url, exc)
                continue
            prefix = "" if index == 0 else f"{self._label(client)}:"
            for item in items:
                merged.append((prefix, item))
        return merged

    def _map_user(self, action: str, fn) -> CreatedUser:
        created: Optional[CreatedUser] = None
        errors: list[str] = []
        for client in self.clients:
            try:
                result = fn(client)
            except DeviceError as exc:
                errors.append(f"{self._label(client)}: {exc}")
                continue
            if created is None:
                created = result
            elif not result.face_ok:
                created.face_ok = False
        if errors:
            raise DeviceError(f"Falha ao {action} em " + "; ".join(errors))
        if created is None:
            raise DeviceError(f"Nenhum facial respondeu ao pedido de {action}.")
        return created


def public_media_url(filename: str) -> str:
    from app.config import reload_env

    reload_env()
    ip = os.getenv("SERVER_LAN_IP", SERVER_LAN_IP).strip()
    if not ip:
        return ""
    return f"http://{ip}:{PORT}/media/{filename}"


_photo_lock = threading.Lock()


def _int_or_none(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _clean_face_id(value: object) -> str:
    """FaceID como texto; vazio quando o usuário não tem rosto ("0")."""
    face_id = str(value or "").strip()
    return "" if face_id == "0" else face_id


def _face_assigned(user: dict[str, Any]) -> bool:
    if user.get("FaceStatus") in (1, "1", True):
        return True
    return user.get("FaceID") not in (None, "", "0", 0)


def _jpeg_ready(path: Path) -> bool:
    """JPEG inteiro, RGB, sem rotação pendente e no tamanho que o facial aceita."""
    try:
        size = path.stat().st_size
        if size < FACE_MIN_BYTES or size > FACE_MAX_BYTES:
            return False
        with Image.open(path) as img:
            if img.format != "JPEG" or img.mode != "RGB":
                return False
            if img.getexif().get(0x0112, 1) != 1:
                return False
            w, h = img.size
            if min(w, h) < FACE_MIN_SIDE or max(w, h) > FACE_MAX_SIDE:
                return False
            img.load()
    except Exception:  # noqa: BLE001 — arquivo que não abre inteiro precisa ser refeito
        return False
    return True


def _ensure_jpeg_file(image_path: Path) -> Path:
    """Devolve um JPEG pronto para o facial, sem regravar o que já está pronto.

    Um facial baixa a foto pela FaceUrl enquanto o outro recebe a mesma foto.
    Regravar o arquivo no meio do download entregava um JPEG cortado, então a
    foto pronta não é tocada e a nova vai para um temporário trocado de uma vez.
    """
    path = Path(image_path)
    if not path.exists():
        raise DeviceError(f"Arquivo de foto não encontrado: {path}")

    dest = path if path.suffix.lower() in {".jpg", ".jpeg"} else path.with_suffix(".jpg")
    with _photo_lock:
        if _jpeg_ready(dest) and (dest == path or dest.stat().st_mtime >= path.stat().st_mtime):
            return dest
        tmp = dest.with_name(f".{dest.stem}_{uuid.uuid4().hex[:8]}.tmp")
        try:
            with Image.open(path) as source:
                img = ImageOps.exif_transpose(source)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                w, h = img.size
                if min(w, h) < FACE_MIN_SIDE:
                    scale = FACE_MIN_SIDE / float(min(w, h))
                    img = img.resize(
                        (max(FACE_MIN_SIDE, int(w * scale)), max(FACE_MIN_SIDE, int(h * scale))),
                        Image.Resampling.LANCZOS,
                    )
                w, h = img.size
                if max(w, h) > FACE_MAX_SIDE:
                    ratio = FACE_MAX_SIDE / float(max(w, h))
                    img = img.resize(
                        (max(1, int(w * ratio)), max(1, int(h * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                img.save(tmp, format="JPEG", quality=92)
            if not _jpeg_ready(tmp):
                raise DeviceError("A foto ficou inválida depois de convertida. Tire outra foto.")
            os.replace(tmp, dest)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"Não foi possível processar a imagem: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
    return dest


def face_photo(image_path: Path) -> Path:
    """JPEG que vai para o facial a partir da foto salva (foto antiga em PNG vira .jpg ao lado)."""
    return _ensure_jpeg_file(Path(image_path))


def prepare_face_photo(image_path: Path) -> Path:
    """Converte a foto recebida no JPEG que vai para o facial e apaga o original."""
    path = Path(image_path)
    jpeg = _ensure_jpeg_file(path)
    if jpeg != path:
        path.unlink(missing_ok=True)
    return jpeg


def _prepare_jpeg_base64(image_path: Path) -> str:
    path = _ensure_jpeg_file(image_path)
    data = path.read_bytes()
    if len(data) < FACE_MIN_BYTES:
        raise DeviceError("Arquivo de foto inválido ou muito pequeno.")
    return base64.b64encode(data).decode("ascii")
