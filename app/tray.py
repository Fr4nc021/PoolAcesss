from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading

from PIL import Image

from app.shortcut import _icon_path

logger = logging.getLogger(__name__)

_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_TOPMOST = 0x00040000
_IDYES = 6

_close_lock = threading.Lock()
_closing = False
_prompt_timer: threading.Timer | None = None
_tray_icon = None


def hide_console() -> None:
    """Esconde a janela preta para não ser fechada por engano."""
    if os.name != "nt":
        return
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)


def silence_missing_stdio() -> None:
    """pythonw não tem console; print precisa de um destino."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def start_tray() -> None:
    """Ícone ao lado do relógio. Clicar pede confirmação antes de sair."""
    try:
        import pystray
    except ImportError:
        logger.warning("pystray não instalado; o programa segue sem ícone de bandeja")
        return

    icon_file = _icon_path()
    if not icon_file.is_file():
        logger.warning("Ícone da bandeja não encontrado: %s", icon_file)
        return

    image = Image.open(icon_file).convert("RGBA")
    global _tray_icon
    _tray_icon = pystray.Icon(
        "acessopiscinas",
        image,
        "Acesso Piscinas — clique para fechar",
        menu=pystray.Menu(
            pystray.MenuItem("Fechar programa", _confirm_close, default=True),
        ),
    )
    threading.Thread(target=_tray_icon.run, name="tray", daemon=True).start()


def schedule_close_prompt() -> None:
    """A aba sumiu. Se nenhuma página reabrir logo, pergunta se fecha o programa."""
    global _prompt_timer
    with _close_lock:
        if _closing:
            return
        if _prompt_timer is not None:
            _prompt_timer.cancel()
        _prompt_timer = threading.Timer(1.8, _ask_close_from_browser)
        _prompt_timer.daemon = True
        _prompt_timer.start()


def cancel_close_prompt() -> None:
    global _prompt_timer
    with _close_lock:
        if _prompt_timer is not None:
            _prompt_timer.cancel()
            _prompt_timer = None


def _confirm_close(icon, _item) -> None:
    if not _ask_yes_no():
        return
    _shutdown(icon)


def _ask_close_from_browser() -> None:
    global _prompt_timer
    with _close_lock:
        _prompt_timer = None
        if _closing:
            return
    if not _ask_yes_no():
        from app.config import PORT

        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    _shutdown(_tray_icon)


def _ask_yes_no() -> bool:
    answer = ctypes.windll.user32.MessageBoxW(
        None,
        "Fechar programa?",
        "Acesso Piscinas",
        _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST,
    )
    return answer == _IDYES


def _shutdown(icon) -> None:
    global _closing
    with _close_lock:
        if _closing:
            return
        _closing = True
    if icon is not None:
        try:
            icon.title = "Sincronizando antes de fechar..."
        except Exception:
            logger.exception("Não foi possível atualizar o ícone da bandeja")
    try:
        from app.sync import run_sync_cycle

        run_sync_cycle()
    except Exception:
        logger.exception("Falha ao sincronizar antes de fechar")
    os._exit(0)
