from __future__ import annotations

import ctypes
import logging
import os
import shutil
import signal
import subprocess
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


def open_local_browser(port: int) -> None:
    """Abre a tela no Chrome. No Chromebook o Chrome fica fora do Linux."""
    url = f"http://127.0.0.1:{port}/"
    from app.config import on_chromeos

    if on_chromeos():
        for cmd in (["garcon-url-handler", url], ["xdg-open", url]):
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except OSError:
                continue
    import webbrowser

    webbrowser.open(url)


def install_shutdown_signals() -> None:
    """No ChromeOS, fechar o terminal sincroniza antes de sair."""
    if os.name == "nt":
        return

    def _handle(_signum, _frame) -> None:
        _shutdown(None)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handle)
        except (OSError, ValueError):
            continue


def start_tray() -> None:
    """Ícone ao lado do relógio, só no Windows."""
    if os.name != "nt":
        return
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
            pystray.MenuItem(
                _update_label,
                _confirm_update,
                visible=_update_ready,
            ),
        ),
    )
    threading.Thread(target=_tray_icon.run, name="tray", daemon=True).start()


def _update_ready(_item=None) -> bool:
    from app import updater

    return updater.status().get("state") == "ready"


def _update_label(_item=None) -> str:
    from app import updater

    return f"Instalar atualização {updater.status().get('available') or ''}".strip()


def notify_update_ready(version: str) -> None:
    """Mostra o item Instalar no menu e um aviso ao lado do relógio."""
    icon = _tray_icon
    if icon is None:
        return
    try:
        icon.update_menu()
        icon.notify(
            f"Versão {version} pronta. Clique no ícone e escolha Instalar atualização.",
            "Acesso Piscinas",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Não foi possível avisar a atualização na bandeja")


def _confirm_update(icon, _item) -> None:
    from app import updater
    from app.update_core import UpdateError

    version = updater.status().get("available") or ""
    if not _message_yes_no(
        f"Instalar a versão {version} agora?\n\n"
        "O programa fecha, troca os arquivos e abre de novo sozinho. "
        "Cadastros e fotos não são apagados."
    ):
        return
    try:
        updater.install()
    except UpdateError as exc:
        _message_info(str(exc))


def shutdown_for_update() -> None:
    """Fecha para o atualizador trocar os arquivos. Sincroniza antes, como sempre."""
    icon = _tray_icon
    if icon is not None:
        try:
            icon.title = "Atualizando..."
        except Exception:  # noqa: BLE001
            pass
    _shutdown(icon)


def _message_yes_no(text: str) -> bool:
    if os.name != "nt":
        return False
    answer = ctypes.windll.user32.MessageBoxW(
        None, text, "Acesso Piscinas", _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST
    )
    return answer == _IDYES


def _message_info(text: str) -> None:
    if os.name != "nt":
        logger.info(text)
        return
    ctypes.windll.user32.MessageBoxW(None, text, "Acesso Piscinas", _MB_TOPMOST)


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

        open_local_browser(PORT)
        return
    _shutdown(_tray_icon)


def _ask_yes_no() -> bool:
    if os.name != "nt":
        zenity = shutil.which("zenity")
        if not zenity:
            logger.info("Feche o Terminal para encerrar o Acesso Piscinas.")
            return False
        result = subprocess.run(
            [
                zenity,
                "--question",
                "--no-wrap",
                "--title=Acesso Piscinas",
                "--text=Fechar programa?",
            ],
            check=False,
        )
        return result.returncode == 0
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
