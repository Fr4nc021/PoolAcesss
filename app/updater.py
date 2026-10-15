"""Procura versão nova no GitHub Releases, baixa, confere e prepara a troca.

A troca dos arquivos fica com o AcessoPiscinas-Atualizador.exe, que roda
depois que este processo fecha. Nada é instalado sem alguém confirmar.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import httpx

from app.config import PORT, RUNTIME_DIR, on_chromeos
from app.update_core import (
    MANIFEST_NAME,
    Manifest,
    MAX_MANIFEST_BYTES,
    MAX_SIGNATURE_BYTES,
    SIGNATURE_NAME,
    UPDATER_EXE,
    UpdateError,
    check_release,
    check_staged_files,
    check_zip_file,
    extract_verified,
    parse_manifest,
    verify_signature,
)
from app.update_key import UPDATE_PUBLIC_KEY
from app.version import VERSION

logger = logging.getLogger(__name__)

GITHUB_REPO = "Fr4nc021/PoolAcesss"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
FIRST_CHECK_DELAY = 30
CHECK_INTERVAL = 6 * 60 * 60

UPDATE_DIR = RUNTIME_DIR / "_update"
RESULT_FILE = UPDATE_DIR / "ultimo-resultado.json"

_lock = threading.Lock()
_state: dict[str, object] = {
    "state": "idle",
    "message": "",
    "current": VERSION,
    "available": None,
}
_staged: Optional[Path] = None
_staged_manifest: Optional[Manifest] = None
_busy = 0
_installing = False
_thread: Optional[threading.Thread] = None
_wake = threading.Event()


class Busy(Exception):
    """Pedido recusado porque a atualização está sendo instalada."""


def enabled_reason() -> str:
    """Vazio quando este PC pode se atualizar sozinho."""
    if not getattr(sys, "frozen", False) and not os.environ.get("ACESSO_UPDATE_DEV"):
        return "Atualização automática só no programa instalado (.exe)."
    if os.name != "nt" or on_chromeos():
        return "Atualização automática só no Windows."
    if not UPDATE_PUBLIC_KEY.strip():
        return "Chave pública de atualização não configurada nesta versão."
    return ""


def status() -> dict[str, object]:
    with _lock:
        data = dict(_state)
        data["busy"] = _busy > 0
        data["installing"] = _installing
    return data


def _set(state: str, message: str = "", available: Optional[str] = None) -> None:
    with _lock:
        changed = _state["state"] != state
        _state["state"] = state
        _state["message"] = message
        if available is not None or state in {"idle", "current"}:
            _state["available"] = available
    if state == "ready" and changed and available:
        from app.tray import notify_update_ready

        notify_update_ready(available)


@contextmanager
def busy_operation() -> Iterator[None]:
    """Marca um cadastro, renovação ou troca de foto em andamento."""
    global _busy
    with _lock:
        if _installing:
            raise Busy("O programa está instalando uma atualização. Tente em instantes.")
        _busy += 1
    try:
        yield
    finally:
        with _lock:
            _busy -= 1


def _get_limited(client: httpx.Client, url: str, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise UpdateError("Endereço de download sem HTTPS.")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        if not str(response.url).startswith("https://"):
            raise UpdateError("Download redirecionado para fora do HTTPS.")
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise UpdateError("Arquivo da release maior que o esperado.")
    return bytes(data)


def _download_to(client: httpx.Client, url: str, dest: Path, limit: int) -> None:
    if not url.startswith("https://"):
        raise UpdateError("Endereço de download sem HTTPS.")
    written = 0
    with client.stream("GET", url) as response, open(dest, "wb") as out:
        response.raise_for_status()
        if not str(response.url).startswith("https://"):
            raise UpdateError("Download redirecionado para fora do HTTPS.")
        for chunk in response.iter_bytes():
            written += len(chunk)
            if written > limit:
                raise UpdateError("Pacote maior que o manifest informa.")
            out.write(chunk)


def check_now() -> dict[str, object]:
    """Consulta a última release. Se houver versão maior e válida, deixa pronta."""
    global _staged, _staged_manifest
    reason = enabled_reason()
    if reason:
        _set("disabled", reason)
        return status()
    with _lock:
        if _installing or _state["state"] in {"checking", "downloading"}:
            return dict(_state)
    _set("checking", "Procurando versão nova...")
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0),
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"AcessoPiscinas/{VERSION}",
            },
        ) as client:
            response = client.get(LATEST_URL)
            if response.status_code == 404:
                _set("current", "Nenhuma versão publicada ainda.")
                return status()
            response.raise_for_status()
            release = response.json()
            tag = str(release.get("tag_name", ""))
            assets = {
                str(item.get("name")): str(item.get("browser_download_url", ""))
                for item in release.get("assets", [])
            }
            if tag == f"v{VERSION}":
                _set("current", f"Versão {VERSION} é a mais recente.")
                return status()
            if MANIFEST_NAME not in assets or SIGNATURE_NAME not in assets:
                raise UpdateError(f"A release {tag} não tem manifest assinado.")

            manifest_bytes = _get_limited(client, assets[MANIFEST_NAME], MAX_MANIFEST_BYTES)
            signature = _get_limited(client, assets[SIGNATURE_NAME], MAX_SIGNATURE_BYTES)
            verify_signature(manifest_bytes, signature, UPDATE_PUBLIC_KEY)
            manifest = parse_manifest(manifest_bytes)
            check_release(manifest, tag=tag, current_version=VERSION)

            staging = UPDATE_DIR / "staging" / manifest.version
            ready_flag = staging.parent / f"{manifest.version}.ok"
            if ready_flag.is_file() and staging.is_dir():
                _staged, _staged_manifest = staging, manifest
                _set("ready", f"Versão {manifest.version} pronta para instalar.", manifest.version)
                return status()

            if manifest.zip_name not in assets:
                raise UpdateError(f"A release {tag} não tem o pacote {manifest.zip_name}.")
            _set("downloading", f"Baixando a versão {manifest.version}...", manifest.version)
            UPDATE_DIR.mkdir(parents=True, exist_ok=True)
            zip_path = UPDATE_DIR / "download.zip"
            zip_path.unlink(missing_ok=True)
            try:
                _download_to(client, assets[manifest.zip_name], zip_path, manifest.zip_size)
                check_zip_file(zip_path, manifest)
                for old in (UPDATE_DIR / "staging").glob("*"):
                    if old.is_dir() and old != staging:
                        shutil.rmtree(old, ignore_errors=True)
                    elif old.suffix == ".ok":
                        old.unlink(missing_ok=True)
                extract_verified(zip_path, staging, manifest)
            finally:
                zip_path.unlink(missing_ok=True)
            ready_flag.write_text(manifest.zip_sha256, encoding="utf-8")
            _staged, _staged_manifest = staging, manifest
            _set("ready", f"Versão {manifest.version} pronta para instalar.", manifest.version)
            logger.info("Atualização %s verificada e pronta", manifest.version)
    except UpdateError as exc:
        logger.warning("Atualização recusada: %s", exc)
        _set("error", str(exc))
    except httpx.HTTPError as exc:
        logger.info("Sem acesso ao GitHub para atualizar: %s", exc)
        _set("error", "Sem acesso à internet para procurar atualização.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha inesperada ao procurar atualização")
        _set("error", f"Falha ao procurar atualização: {exc}")
    return status()


def install() -> None:
    """Chama o atualizador e fecha o programa. Recusa durante um cadastro."""
    global _installing
    with _lock:
        if _installing:
            raise UpdateError("A instalação já começou.")
        if _busy > 0:
            raise UpdateError(
                "Há um cadastro em andamento. Termine e clique em Instalar de novo."
            )
        staging, manifest = _staged, _staged_manifest
        if (
            _state.get("state") != "ready"
            or staging is None
            or manifest is None
            or not staging.is_dir()
        ):
            raise UpdateError("Nenhuma atualização pronta para instalar.")
        _installing = True
    try:
        check_staged_files(staging, manifest)
        runner_dir = UPDATE_DIR / "runner"
        shutil.rmtree(runner_dir, ignore_errors=True)
        runner_dir.mkdir(parents=True)
        source = staging / UPDATER_EXE
        if not source.is_file():
            source = RUNTIME_DIR / UPDATER_EXE
        if not source.is_file():
            raise UpdateError("Atualizador não encontrado no pacote.")
        runner = runner_dir / UPDATER_EXE
        shutil.copy2(source, runner)

        plan = {
            "install_dir": str(RUNTIME_DIR),
            "staging_dir": str(staging),
            "backup_root": str(UPDATE_DIR / "backup"),
            "result_file": str(RESULT_FILE),
            "log_file": str(UPDATE_DIR / "atualizador.log"),
            "app_exe": "AcessoPiscinas.exe",
            "pid": os.getpid(),
            "port": PORT,
            "from_version": VERSION,
            "to_version": _state.get("available"),
        }
        plan_path = UPDATE_DIR / "plano.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [str(runner), "--plan", str(plan_path)],
            cwd=str(runner_dir),
            creationflags=flags,
            close_fds=True,
        )
    except Exception:
        with _lock:
            _installing = False
        raise
    _set("installing", "Instalando. O programa volta sozinho em instantes.")
    logger.info("Atualizador iniciado; fechando o programa")
    threading.Thread(target=_close_for_update, daemon=True).start()


def _close_for_update() -> None:
    time.sleep(1.0)
    from app.tray import shutdown_for_update

    shutdown_for_update()


def last_result() -> Optional[dict[str, object]]:
    """Resultado da última troca, escrito pelo atualizador."""
    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        finished = datetime.fromisoformat(str(data.get("finished_at")))
        if (datetime.now() - finished).days >= 2:
            return None
    except ValueError:
        return None
    return data


def start_update_worker() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    reason = enabled_reason()
    if reason:
        _set("disabled", reason)
        return
    _thread = threading.Thread(target=_worker, name="updater", daemon=True)
    _thread.start()


def request_check() -> None:
    _wake.set()


def _worker() -> None:
    _wake.wait(FIRST_CHECK_DELAY)
    while True:
        _wake.clear()
        if status().get("state") not in {"ready", "installing"}:
            check_now()
        _wake.wait(CHECK_INTERVAL)
