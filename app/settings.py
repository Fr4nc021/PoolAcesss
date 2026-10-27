"""Configurações editadas pela aba Configurações da ADM.

Grava no mesmo env.txt ou .env que o programa lê, mantendo comentários e
linhas que a tela não mostra. Quase tudo vale na hora porque quem usa
estes valores relê o arquivo (reload_env) antes de falar com o facial,
com o outro PC ou com a nuvem.
"""

from __future__ import annotations

import ipaddress
import os
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.config import PORT, RUNTIME_DIR, _device_base_url, _env_file, on_chromeos, reload_env

_lock = threading.Lock()

# Chave -> (rótulo, secreto). Secreto não volta para a tela; em branco mantém o atual.
FIELDS: dict[str, tuple[str, bool]] = {
    "DEVICE_URL": ("Facial da piscina normal", False),
    "DEVICE_URL_2": ("Facial da piscina térmica", False),
    "DEVICE_LOGIN": ("Usuário dos faciais", False),
    "DEVICE_PASSWORD": ("Senha dos faciais", True),
    "SERVER_LAN_IP": ("IP deste PC", False),
    "SYNC_PEER_URL": ("Endereço do outro PC", False),
    "SYNC_TOKEN": ("Senha de sincronização", True),
    "SYNC_INTERVAL_SECONDS": ("Intervalo da troca", False),
    "SUPABASE_URL": ("Endereço da nuvem", False),
    "SUPABASE_KEY": ("Chave da nuvem", True),
}


class SettingsError(ValueError):
    """Valor inválido. A mensagem vai para a tela."""


def env_path() -> Path:
    """Arquivo que o programa já lê. Sem nenhum, cria o do sistema."""
    existing = _env_file()
    if existing is not None:
        return existing
    return RUNTIME_DIR / ("env.txt" if on_chromeos() else ".env")


def current() -> dict[str, object]:
    """Valores para a tela. Os secretos vêm só como definido ou não."""
    reload_env()
    values: dict[str, object] = {}
    for key, (_label, secret) in FIELDS.items():
        value = os.getenv(key, "").strip()
        values[key] = bool(value) if secret else value
    if not values["DEVICE_LOGIN"]:
        values["DEVICE_LOGIN"] = "admin"
    if not values["SYNC_INTERVAL_SECONDS"]:
        values["SYNC_INTERVAL_SECONDS"] = "20"
    values["env_file"] = str(env_path())
    values["port"] = PORT
    return values


def _last_octet(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.rsplit(".", 1)[-1]


def _http_url(value: str, label: str, *, default_port: Optional[int] = None) -> str:
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsError(f"{label}: use um endereço como http://192.168.0.10.")
    url = _device_base_url(text)
    if default_port and parsed.port is None and parsed.scheme == "http":
        url = f"{url}:{default_port}"
    return url


def validate(form: dict[str, str]) -> dict[str, str]:
    """Confere o formulário e devolve só o que vai para o arquivo."""
    clean: dict[str, str] = {}
    get = lambda key: str(form.get(key) or "").strip()  # noqa: E731

    normal = _http_url(get("DEVICE_URL"), "Facial da piscina normal")
    if not normal:
        raise SettingsError("Informe o endereço do facial da piscina normal.")
    if _last_octet(normal) != "86":
        raise SettingsError("O facial da piscina normal precisa terminar em .86 (ex.: http://192.168.0.86).")
    clean["DEVICE_URL"] = normal
    termica = _http_url(get("DEVICE_URL_2"), "Facial da piscina térmica")
    if termica and _last_octet(termica) != "44":
        raise SettingsError("O facial da piscina térmica precisa terminar em .44 (ex.: http://192.168.0.44).")
    clean["DEVICE_URL_2"] = termica

    login = get("DEVICE_LOGIN")
    if not login:
        raise SettingsError("Informe o usuário dos faciais.")
    clean["DEVICE_LOGIN"] = login

    ip = get("SERVER_LAN_IP")
    try:
        if ipaddress.ip_address(ip).version != 4:
            raise ValueError
    except ValueError:
        raise SettingsError("IP deste PC: use o Endereço IPv4, como 192.168.0.20.") from None
    clean["SERVER_LAN_IP"] = ip

    peer = _http_url(get("SYNC_PEER_URL"), "Endereço do outro PC", default_port=PORT)
    if peer and (urlparse(peer).hostname or "") in {ip, "127.0.0.1", "localhost"}:
        raise SettingsError("Endereço do outro PC: use o IP do outro computador, não o deste.")
    clean["SYNC_PEER_URL"] = peer

    interval = get("SYNC_INTERVAL_SECONDS") or "20"
    if not interval.isdigit() or not 5 <= int(interval) <= 3600:
        raise SettingsError("Intervalo da troca: use de 5 a 3600 segundos.")
    clean["SYNC_INTERVAL_SECONDS"] = interval

    cloud = get("SUPABASE_URL")
    if cloud:
        parsed = urlparse(cloud)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SettingsError("Endereço da nuvem: use o https://... do projeto Supabase.")
        cloud = cloud.rstrip("/")
    clean["SUPABASE_URL"] = cloud

    for key in ("DEVICE_PASSWORD", "SYNC_TOKEN", "SUPABASE_KEY"):
        value = get(key)
        if value:
            clean[key] = value
    token = clean.get("SYNC_TOKEN")
    if token is not None and len(token) < 12:
        raise SettingsError("Senha de sincronização: use pelo menos 12 caracteres (ou clique em Gerar).")
    for key, value in clean.items():
        if "\n" in value or "\r" in value:
            raise SettingsError(f"{FIELDS[key][0]}: valor com quebra de linha.")
    return clean


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return text.splitlines()


def write_env(values: dict[str, str], path: Optional[Path] = None) -> Path:
    """Troca as linhas KEY=... e acrescenta as que faltam. O resto do arquivo fica igual."""
    target = path or env_path()
    with _lock:
        lines = _read_lines(target)
        pending = dict(values)
        for index, line in enumerate(lines):
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line.lstrip("﻿"))
            if match and match.group(1) in pending:
                key = match.group(1)
                lines[index] = f"{key}={pending.pop(key)}"
        if pending:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("# Gravado pela aba Configurações")
            lines.extend(f"{key}={value}" for key, value in pending.items())
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    for key, value in values.items():
        os.environ[key] = value
    reload_env()
    return target


def save(form: dict[str, str]) -> list[str]:
    """Valida, grava e devolve os rótulos do que mudou."""
    clean = validate(form)
    reload_env()
    changed = [key for key, value in clean.items() if os.getenv(key, "").strip() != value]
    if changed:
        write_env({key: clean[key] for key in changed})
    return [FIELDS[key][0] for key in changed]
