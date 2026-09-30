from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

from app.config import RESOURCE_DIR, RUNTIME_DIR

logger = logging.getLogger(__name__)

_SHORTCUT_NAME = "Acesso Piscinas.lnk"
_CSIDL_DESKTOPDIRECTORY = 0x0010


def ensure_desktop_shortcut() -> None:
    """Cria ou atualiza o atalho na área de trabalho deste usuário."""
    if os.name != "nt":
        return
    try:
        icon = _icon_path()
        target, workdir, arguments = _launch_target()
        desktop = _desktop_dir()
        if desktop is None or not icon.is_file() or not target.is_file():
            logger.warning(
                "Atalho não criado (ícone=%s, alvo=%s)",
                icon,
                target,
            )
            return
        link = desktop / _SHORTCUT_NAME
        _write_shortcut(link, target, workdir, icon, arguments)
        logger.info("Atalho da área de trabalho: %s", link)
    except Exception:
        logger.exception("Falha ao criar o atalho da área de trabalho")


def _launch_target() -> tuple[Path, Path, str]:
    if getattr(sys, "frozen", False):
        target = Path(sys.executable).resolve()
        return target, target.parent, ""
    pythonw = RUNTIME_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if pythonw.is_file():
        return pythonw.resolve(), RUNTIME_DIR, "-m app.main"
    python = RUNTIME_DIR / ".venv" / "Scripts" / "python.exe"
    return python.resolve(), RUNTIME_DIR, "-m app.main"


def _icon_path() -> Path:
    for path in (
        RUNTIME_DIR / "aquaaccess.ico",
        RUNTIME_DIR / "assets" / "aquaaccess.ico",
        RESOURCE_DIR / "aquaaccess.ico",
    ):
        if path.is_file():
            return path
    return RUNTIME_DIR / "assets" / "aquaaccess.ico"


def _desktop_dir() -> Path | None:
    buffer = ctypes.create_unicode_buffer(32767)
    result = ctypes.windll.shell32.SHGetFolderPathW(
        None,
        _CSIDL_DESKTOPDIRECTORY,
        None,
        0,
        buffer,
    )
    if result != 0 or not buffer.value:
        return None
    return Path(buffer.value)


def _write_shortcut(
    link: Path,
    target: Path,
    workdir: Path,
    icon: Path,
    arguments: str,
) -> None:
    script = """
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:AP_LINK)
$shortcut.TargetPath = $env:AP_TARGET
$shortcut.Arguments = $env:AP_ARGS
$shortcut.WorkingDirectory = $env:AP_WORKDIR
$shortcut.IconLocation = $env:AP_ICON + ',0'
$shortcut.Description = 'Acesso Piscinas'
$shortcut.Save()
"""
    env = os.environ.copy()
    env["AP_LINK"] = str(link)
    env["AP_TARGET"] = str(target)
    env["AP_ARGS"] = arguments
    env["AP_WORKDIR"] = str(workdir)
    env["AP_ICON"] = str(icon.resolve())
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=env,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
