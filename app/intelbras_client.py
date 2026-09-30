from __future__ import annotations

import base64
import logging
import tarfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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

logger = logging.getLogger(__name__)


def _mark_face_always(key: ET.Element) -> None:
    """Relé local permitido e termo de validade Sempre (não Agenda)."""
    key.set("Relay", "1")
    key.set("WebRelay", "0")
    key.set("Validity", FACE_VALIDITY_ALWAYS)
    key.set("Frequency", "0")
    for child in list(key.findall("Schedule")):
        key.remove(child)


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

    def create_user_with_face(
        self,
        name: str,
        image_path: Path,
        *,
        registration: Optional[str] = None,
        face_public_url: Optional[str] = None,
        image_ready: bool = False,
    ) -> CreatedUser:
        _ = face_public_url
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
        face_ok = self._send_face(
            registration=reg,
            name=name,
            jpeg_path=jpeg_path,
            device_user_id=device_id,
        )

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
            logger.warning(
                "Usuário %s criado no XPE sem confirmação de FaceStatus. "
                "Verifique em Moradores > Usuário (busque a matrícula %s).",
                reg,
                reg,
            )

        return CreatedUser(
            device_user_id=device_id,
            registration=reg,
            face_ok=face_ok,
        )

    def _send_face(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
    ) -> bool:
        """Grava a foto pelo import. Base64 só entra se o import falhar."""
        try:
            self._attach_face_via_import(
                registration=registration,
                name=name.strip()[:60],
                jpeg_path=jpeg_path,
                device_user_id=device_user_id,
            )
            logger.info("Usuário sincronizado via import tgz (reg=%s)", registration)
            return True
        except Exception as exc:  # noqa: BLE001 — tenta o envio direto em seguida
            logger.warning("Import de face falhou, tentando base64: %s", exc)
        try:
            self.update_user(
                device_user_id=device_user_id,
                registration=registration,
                name=name,
                image_path=jpeg_path,
            )
            logger.info("Face enviada via base64 (reg=%s)", registration)
            return True
        except DeviceError as exc:
            logger.warning("Tentativa de face (base64) falhou: %s", exc)
            return False

    def _attach_face_via_import(
        self,
        *,
        registration: str,
        name: str,
        jpeg_path: Path,
        device_user_id: int,
    ) -> None:
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
            target = ET.SubElement(
                root,
                "Key",
                {
                    "ID": str(device_user_id),
                    "FaceID": str(new_face),
                    "UserID": registration,
                    "Name": name,
                    "Card": "",
                    "Pin": "",
                    "Floor": "9999",
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
            target.set("Floor", target.get("Floor") or "9999")
        _mark_face_always(target)

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
        _ = face_public_url
        jpeg_path = _ensure_jpeg_file(image_path)
        existing = self.find_user_by_registration(registration)
        if existing:
            device_id = int(existing["ID"])
            face_ok = self._send_face(
                registration=registration,
                name=name,
                jpeg_path=jpeg_path,
                device_user_id=device_id,
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
            return CreatedUser(
                device_user_id=device_id,
                registration=registration,
                face_ok=face_ok,
            )
        return self.create_user_with_face(
            name,
            jpeg_path,
            registration=registration,
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
        if existing:
            if image_path or face_public_url:
                self.update_user(
                    device_user_id=int(existing["ID"]),
                    registration=registration,
                    name=name,
                    image_path=image_path,
                    face_public_url=face_public_url,
                )
            try:
                self._grant_always_access(
                    device_user_id=int(existing["ID"]),
                    registration=registration,
                    name=name,
                    current=existing,
                )
            except DeviceError as exc:
                logger.warning("Relé permitido / validade sempre não confirmados: %s", exc)
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
