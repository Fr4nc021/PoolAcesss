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
from typing import Any, Optional

import httpx
from PIL import Image, ImageOps

from app import intelbras_fcgi

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
FACE_CONFIRM_RETRIES = 8
FACE_CONFIRM_DELAY = 1.5
# Pelo painel o facial leva uns 20 s para gerar o modelo do rosto.
PANEL_CONFIRM_RETRIES = 20
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
    libera a entrada no nome do outro. others são as matrículas dos outros.
    """

    def __init__(self, message: str, others: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.others = others or []


@dataclass
class ExportUser:
    user_id: str
    name: str
    face_id: str
    has_model: bool
    photo: Optional[bytes]
    device_id: str = ""


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
        FaceID fica em faceIDs/RegFaceMeun/<FaceID - 1>.jpg (o arquivo conta a
        partir de 0, o FaceID a partir de 1). None = export indisponível.
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
                            photos[_photo_face_id(Path(item.name).stem)] = fileobj.read()
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
                    device_id=str(key.get("ID") or ""),
                )
            )
        return users

    def face_shared_with(self, registration: str) -> list[str]:
        """Nomes dos outros usuários deste facial que estão no mesmo FaceID da matrícula."""
        signature, shared = self._face_state(registration)
        if signature is not None:
            return shared
        # Sem export, a lista da API é o que há.
        users = self.list_users()
        face_id = next(
            (_clean_face_id(u.get("FaceID")) for u in users if str(u.get("UserID")) == str(registration)),
            "",
        )
        if not face_id:
            return []
        return [
            str(u.get("Name") or u.get("UserID") or "")
            for u in users
            if _clean_face_id(u.get("FaceID")) == face_id and str(u.get("UserID")) != str(registration)
        ]

    def _face_signature(self, registration: str) -> FaceSignature:
        return self._face_state(registration)[0]

    def _face_state(self, registration: str) -> tuple[FaceSignature, list[str]]:
        """Modelo do rosto da matrícula e os nomes de quem mais está no FaceID dela.

        O facial só reconhece quem tem a pasta faceIDs/<FaceID>-<nome>/ no
        export. Ter FaceID ou a foto em RegFaceMeun não basta: a foto fica lá
        sem modelo quando o facial não achou o rosto, e o leitor responde
        "usuário desconhecido". Cada leitura baixa o export inteiro, com as
        fotos, então o modelo e o FaceID repetido saem da mesma leitura.
        """
        try:
            raw = self._export_archive()
            with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
                xml_member = archive.extractfile("faceIDs/userdata.xml")
                if xml_member is None:
                    return None, []
                root = ET.fromstring(xml_member.read())
                keys = root.findall("Key")
                face_id = next(
                    (
                        _clean_face_id(key.get("FaceID"))
                        for key in keys
                        if str(key.get("UserID")) == str(registration)
                    ),
                    "",
                )
                if not face_id:
                    return (), []
                shared = [
                    str(key.get("Name") or key.get("UserID") or "")
                    for key in keys
                    if _clean_face_id(key.get("FaceID")) == face_id
                    and str(key.get("UserID")) != str(registration)
                ]
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
            return None, []
        return ((face_id, digest.hexdigest()) if found else ()), shared

    def _face_confirmed(
        self,
        registration: str,
        *,
        previous: FaceSignature = (),
        retries: int = FACE_CONFIRM_RETRIES,
        name: str = "",
    ) -> bool:
        """Confirma que o facial gerou um modelo novo do rosto para a matrícula.

        Um modelo novo num FaceID de outro usuário não vale: levanta FaceIDConflict.
        """
        for attempt in range(retries):
            user = self.find_user_by_registration(registration)
            if user and _face_assigned(user):
                signature, shared = self._face_state(registration)
                if signature is None:
                    # Sem export não dá para ver o modelo: só vale o FaceStatus do facial.
                    if user.get("FaceStatus") in (1, "1", True):
                        return True
                elif signature and signature != previous:
                    if shared:
                        self._raise_conflict(registration, name or str(user.get("Name") or ""), shared)
                    return True
            if attempt + 1 < retries:
                time.sleep(FACE_CONFIRM_DELAY)
        return False

    def _panel_http(self) -> httpx.Client:
        """Sessão do painel web: login por cookie, sem Basic Auth."""
        return httpx.Client(timeout=self.timeout, follow_redirects=True)

    def _add_via_panel(self, name: str, registration: str, jpeg_path: Path) -> None:
        """Cria usuário e rosto pelo /fcgi, como o botão Salvar do painel do Intelbras.

        Os faciais do clube não têm API oficial. Pelos endereços /api/user/*
        (user/add e depois user/set com FaceUrl), todo rosto novo caía no FaceID
        do último cadastrado; pelo caminho do painel, cada um recebe o seu.
        Por isso rosto só entra por aqui.
        """
        try:
            with self._panel_http() as http:
                intelbras_fcgi.login(http, self.base_url, self.login_name, self.password)
                intelbras_fcgi.add_user_with_face(
                    http, self.base_url, registration, name, Path(jpeg_path).read_bytes()
                )
        except intelbras_fcgi.PanelUnavailable as exc:
            raise DeviceError(
                f"O facial {self.base_url} não respondeu como o painel do Intelbras (/fcgi): {exc}"
            ) from exc
        except intelbras_fcgi.PanelError as exc:
            raise DeviceError(f"{self.base_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DeviceError(f"Falha ao cadastrar pelo painel do facial {self.base_url}: {exc}") from exc

    def _wait_for_user(self, registration: str) -> dict[str, Any]:
        for attempt in range(FACE_CONFIRM_RETRIES):
            user = self.find_user_by_registration(registration)
            if user:
                return user
            if attempt + 1 < FACE_CONFIRM_RETRIES:
                time.sleep(FACE_CONFIRM_DELAY)
        raise DeviceError(
            f"O facial {self.base_url} recebeu o cadastro de {registration}, mas ele não apareceu na lista."
        )

    def create_user_with_face(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        image_ready: bool = False,
    ) -> CreatedUser:
        """Cadastra usuário e rosto pelo painel e confere que o facial gerou um rosto só dele."""
        reg = registration or f"AP{int(time.time()) % 100000000:08d}"
        if not registration_fits_panel(reg):
            raise DeviceError(
                f"A matrícula {reg} não cabe no facial: só letras e números, até 11 caracteres."
            )
        jpeg_path = image_path if image_ready else _ensure_jpeg_file(image_path)
        self._add_via_panel(name, reg, jpeg_path)
        try:
            user = self._wait_for_user(reg)
            face_ok = self._face_confirmed(reg, retries=PANEL_CONFIRM_RETRIES, name=name)
        except FaceIDConflict:
            self._rollback(reg)
            raise
        if not face_ok:
            logger.error("Face não confirmada para %s — removendo cadastro incompleto do facial", reg)
            self._rollback(reg)
            raise DeviceError(FACE_NOT_ACCEPTED_MSG)
        return CreatedUser(device_user_id=int(user["ID"]), registration=reg, face_ok=True)

    def _rollback(self, registration: str) -> None:
        try:
            self.delete_user(registration=registration)
        except DeviceError as exc:
            logger.warning("Rollback do usuário %s falhou: %s", registration, exc)

    def _raise_conflict(self, registration: str, name: str, others: list[str]) -> None:
        logger.error(
            "Face de %s (reg=%s) ficou no FaceID de %s em %s",
            name,
            registration,
            ", ".join(others),
            self.base_url,
        )
        exported = self.export_users() or []
        face_id = next((u.face_id for u in exported if u.user_id == str(registration)), "")
        registrations = [
            u.user_id for u in exported if face_id and u.face_id == face_id and u.user_id != str(registration)
        ]
        # Na térmica e na normal, todo rosto novo caiu no FaceID do último
        # cadastrado, com pessoas diferentes: é a contagem do aparelho, não a foto.
        raise FaceIDConflict(
            f"O facial {self.base_url} repetiu o número de rosto (FaceID) de "
            f"{', '.join(others)} para {name}. Isso é defeito do aparelho, não da foto: "
            "reinicie o facial e tente de novo; se continuar, use ADM > Configurações > "
            "Refazer facial do zero ou fale com a Intelbras.",
            registrations,
        )

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

    def rename_user(self, registration: str, name: str) -> None:
        """Acerta o nome no facial sem mexer no rosto."""
        known = self.find_user_by_registration(registration)
        if not known:
            raise DeviceError(f"{name} não está no facial {self.base_url}.")
        self._grant_always_access(
            device_user_id=int(known["ID"]),
            registration=registration,
            name=name,
            current=known,
        )

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

    def update_member_face(
        self,
        *,
        name: str,
        registration: str,
        image_path: Path,
    ) -> CreatedUser:
        """Troca a foto de um sócio: apaga do facial e cadastra de novo pelo painel.

        O painel edita com outro comando (cUserEdit), ainda não usado aqui;
        recriar dá o mesmo resultado e o rosto novo ganha um FaceID só dele.
        """
        jpeg_path = _ensure_jpeg_file(image_path)
        if self.find_user_by_registration(registration):
            self.delete_user(registration=registration)
        return self.create_user_with_face(name, jpeg_path, registration=registration, image_ready=True)

    def register_member(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        validity_days: int = VALIDITY_DAYS,
        image_ready: bool = False,
    ) -> CreatedUser:
        _ = validity_days
        return self.create_user_with_face(
            name,
            image_path,
            registration=registration,
            image_ready=image_ready,
        )

    def renew_member(
        self,
        *,
        name: str,
        registration: str,
        image_path: Optional[Path],
        previous_device_user_id: Optional[int] = None,
    ) -> CreatedUser:
        """Renova: quem está no facial com o rosto dele fica como está; os outros são recriados.

        Recria (apaga e cadastra pelo painel) quem divide o FaceID com outro,
        quem está no facial sem rosto e quem não está no facial.
        """
        _ = previous_device_user_id
        self.ping()
        existing = self.find_user_by_registration(registration)
        if existing and not self.face_shared_with(registration) and self._face_signature(registration):
            return CreatedUser(device_user_id=int(existing["ID"]), registration=registration, face_ok=True)
        if not image_path or not Path(image_path).exists():
            raise DeviceError(
                f"A foto salva de {name} não está neste PC, e o facial {self.base_url} precisa dela "
                "para cadastrar o rosto. Envie uma foto nova pelo botão Foto."
            )
        if existing:
            self.delete_user(registration=registration)
        return self.create_user_with_face(name, Path(image_path), registration=registration)



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
    ) -> CreatedUser:
        return self._map_user(
            "renovar",
            lambda client: client.renew_member(
                name=name,
                registration=registration,
                image_path=image_path,
                previous_device_user_id=previous_device_user_id,
            ),
        )

    def update_member_face(
        self,
        *,
        name: str,
        registration: str,
        image_path: Path,
    ) -> CreatedUser:
        return self._map_user(
            "atualizar a foto",
            lambda client: client.update_member_face(
                name=name,
                registration=registration,
                image_path=image_path,
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


def registration_fits_panel(registration: object) -> bool:
    return intelbras_fcgi.registration_fits(str(registration or ""))


def _clean_face_id(value: object) -> str:
    """FaceID como texto; vazio quando o usuário não tem rosto.

    O export traz o número ("12", "0" = sem rosto). A lista da API traz um
    endereço no lugar, com o arquivo contado a partir de 0: FaceID 12 vem
    como ".../Image/RegisterImg/11.jpg" e "-1.jpg" é sem rosto.
    """
    face_id = str(value or "").strip()
    if "/" in face_id:
        index = face_id.rsplit("/", 1)[-1].split(".", 1)[0]
        return str(int(index) + 1) if index.isdigit() else ""
    return "" if face_id in ("0", "-1") else face_id


def _photo_face_id(stem: str) -> str:
    """FaceID da foto faceIDs/RegFaceMeun/<arquivo>.jpg: o arquivo conta a partir de 0."""
    return str(int(stem) + 1) if stem.isdigit() else stem


def _face_assigned(user: dict[str, Any]) -> bool:
    if user.get("FaceStatus") in (1, "1", True):
        return True
    return bool(_clean_face_id(user.get("FaceID")))


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

    Os dois faciais recebem a mesma foto ao mesmo tempo. Regravar o arquivo
    no meio de um envio entregava um JPEG cortado, então a foto pronta não é
    tocada e a nova vai para um temporário trocado de uma vez.
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
