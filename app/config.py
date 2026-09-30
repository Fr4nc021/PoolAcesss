from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Segundo facial, só se DEVICE_URL_2 estiver no .env.
_DEFAULT_DEVICE_URL_2 = ""


def _runtime_dir() -> Path:
    """Pasta gravável: ao lado do .exe (ou raiz do projeto em desenvolvimento)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_dir() -> Path:
    """Pasta de recursos empacotados (templates/static)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


RUNTIME_DIR = _runtime_dir()
RESOURCE_DIR = _resource_dir()
BASE_DIR = RUNTIME_DIR


def reload_env() -> None:
    """Relê o .env ao lado do programa (UTF-8 ou UTF-16 do Bloco de Notas)."""
    path = RUNTIME_DIR / ".env"
    if not path.is_file():
        return
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ[key] = value


reload_env()

DATA_DIR = RUNTIME_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "acessopiscinas.db"

DEVICE_URL = os.getenv("DEVICE_URL", "").rstrip("/")
DEVICE_LOGIN = os.getenv("DEVICE_LOGIN", "admin")
DEVICE_PASSWORD = os.getenv("DEVICE_PASSWORD", "")


def configured_device_urls() -> list[str]:
    """Faciais que recebem o mesmo cadastro. DEVICE_URL é o principal."""
    reload_env()
    chunks = [
        os.getenv("DEVICE_URL", ""),
        os.getenv("DEVICE_URL_2", "") or _DEFAULT_DEVICE_URL_2,
        os.getenv("DEVICE_URLS", ""),
    ]
    seen: list[str] = []
    for chunk in chunks:
        for part in chunk.split(","):
            url = _device_base_url(part)
            if url and url not in seen:
                seen.append(url)
    return seen


POOL_NORMAL = "normal"
POOL_TERMICA = "termica"


def registration_pools() -> list[dict[str, str]]:
    """Opções do cadastro. Cada piscina usa um facial."""
    reload_env()
    by_host: dict[str, str] = {}
    for url in configured_device_urls():
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        by_host[host] = url

    def url_for(octet: str, fallback_host: str) -> str:
        for host, url in by_host.items():
            if host == octet or host.endswith(f".{octet}"):
                return url
        return f"http://{fallback_host}"

    return [
        {
            "id": POOL_NORMAL,
            "label": "Piscina normal",
            "url": url_for("86", "192.168.x.86"),
        },
        {
            "id": POOL_TERMICA,
            "label": "Piscina térmica",
            "url": url_for("44", "192.168.x.44"),
        },
    ]


def pool_by_id(pool_id: Optional[str]) -> Optional[dict[str, str]]:
    if not pool_id:
        return None
    for pool in registration_pools():
        if pool["id"] == pool_id:
            return pool
    return None


def selected_pools(pool_value: Optional[str]) -> list[dict[str, str]]:
    """Piscinas gravadas no membro. Vazio significa as duas (cadastro antigo)."""
    if not pool_value:
        return []
    wanted = {part.strip() for part in pool_value.split(",") if part.strip()}
    return [pool for pool in registration_pools() if pool["id"] in wanted]


def normalize_pool_ids(raw_ids: list[str]) -> Optional[str]:
    """Aceita uma ou as duas piscinas e devolve 'normal', 'termica' ou 'normal,termica'."""
    wanted: set[str] = set()
    for item in raw_ids:
        for part in item.split(","):
            part = part.strip()
            if part:
                wanted.add(part)
    ordered = [pool["id"] for pool in registration_pools() if pool["id"] in wanted]
    if not ordered:
        return None
    return ",".join(ordered)


def pool_label(pool_id: Optional[str]) -> str:
    if not pool_id:
        return "As duas piscinas"
    chosen = selected_pools(pool_id)
    if not chosen or len(chosen) > 1:
        return "As duas piscinas"
    return chosen[0]["label"]


def _device_base_url(value: str) -> str:
    """Aceita http://ip ou a URL da tela (/fcgi/do?...) e devolve só a origem."""
    text = value.strip()
    if not text:
        return ""
    if "://" in text:
        from urllib.parse import urlparse

        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return text.rstrip("/")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


VALIDITY_DAYS = _int_env("VALIDITY_DAYS", 30)
EXPIRY_CHECK_MINUTES = _int_env("EXPIRY_CHECK_MINUTES", 15)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _int_env("PORT", 5000)
SECRET_KEY = os.getenv("SECRET_KEY", "acessopiscinas-dev")
SERVER_LAN_IP = os.getenv("SERVER_LAN_IP", "")
DOOR_NUM = _int_env("DOOR_NUM", 1)


class SyncSettings:
    def __init__(
        self,
        *,
        peer_url: str,
        token: str,
        interval: int,
        supabase_url: str,
        supabase_key: str,
    ) -> None:
        self.peer_url = peer_url
        self.token = token
        self.interval = interval
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key

    @property
    def peer_enabled(self) -> bool:
        return bool(self.peer_url and self.token)

    @property
    def cloud_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


def sync_settings() -> SyncSettings:
    """Relê o .env para o outro PC e a nuvem valerem sem reiniciar o processo."""
    reload_env()
    peer = os.getenv("SYNC_PEER_URL", "").strip().rstrip("/")
    return SyncSettings(
        peer_url=peer,
        token=os.getenv("SYNC_TOKEN", "").strip(),
        interval=max(5, _int_env("SYNC_INTERVAL_SECONDS", 20)),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_key=os.getenv("SUPABASE_KEY", "").strip(),
    )


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
