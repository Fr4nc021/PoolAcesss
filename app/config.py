from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


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

load_dotenv(RUNTIME_DIR / ".env")

DATA_DIR = RUNTIME_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "acessopiscinas.db"

DEVICE_URL = os.getenv("DEVICE_URL", "").rstrip("/")
DEVICE_LOGIN = os.getenv("DEVICE_LOGIN", "admin")
DEVICE_PASSWORD = os.getenv("DEVICE_PASSWORD", "")
VALIDITY_DAYS = int(os.getenv("VALIDITY_DAYS", "30"))
EXPIRY_CHECK_MINUTES = int(os.getenv("EXPIRY_CHECK_MINUTES", "15"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
SECRET_KEY = os.getenv("SECRET_KEY", "acessopiscinas-dev")
SERVER_LAN_IP = os.getenv("SERVER_LAN_IP", "")
DOOR_NUM = int(os.getenv("DOOR_NUM", "1"))


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
