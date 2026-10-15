from __future__ import annotations

import logging
import threading
import time

from flask import Flask, request

from app import db
from app.config import (
    HOST,
    PORT,
    RESOURCE_DIR,
    SECRET_KEY,
    SERVER_LAN_IP,
    configured_device_urls,
    ensure_directories,
    on_chromeos,
    sync_settings,
)
from app.expiry import start_expiry_worker
from app.routes import bp
from app.shortcut import ensure_desktop_shortcut
from app.sync import start_sync_worker, status_view
from app.tray import (
    cancel_close_prompt,
    hide_console,
    install_shutdown_signals,
    open_local_browser,
    silence_missing_stdio,
    start_tray,
)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "static"),
    )
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    ensure_directories()
    db.init_db()
    app.register_blueprint(bp)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    @app.before_request
    def _ensure_background_workers():
        start_expiry_worker()
        start_sync_worker()
        if request.endpoint != "main.fechar_aba":
            cancel_close_prompt()

    @app.context_processor
    def _inject_sync_status():
        return {"sync_status": status_view()}

    return app


def _open_browser() -> None:
    time.sleep(1.2)
    open_local_browser(PORT)


def main() -> None:
    silence_missing_stdio()
    hide_console()
    ensure_desktop_shortcut()
    install_shutdown_signals()
    app = create_app()
    start_tray()
    start_expiry_worker()
    start_sync_worker()
    devices = ", ".join(configured_device_urls()) or "(nenhum facial configurado)"
    sync = sync_settings()
    peer = sync.peer_url or "(não configurado)"
    cloud = "configurada" if sync.cloud_enabled else "(não configurada)"
    host = HOST
    if on_chromeos() and host in {"127.0.0.1", "localhost", ""}:
        host = "0.0.0.0"
    print("=" * 50)
    print(" Acesso Piscinas Clube Aliança")
    print(f" Neste PC:   http://127.0.0.1:{PORT}")
    print(f" Na rede:    http://{SERVER_LAN_IP}:{PORT}")
    print(f" Faciais:    {devices}")
    print(f" Outro PC:   {peer}")
    print(f" Nuvem:      {cloud}")
    if on_chromeos():
        print(" ChromeOS: o Chrome abre em http://127.0.0.1:%s" % PORT)
        print(" Para o facial alcançar as fotos, encaminhe a porta %s:" % PORT)
        print(" Configurações > Sobre o ChromeOS > Linux > Encaminhamento de porta.")
        print(" SERVER_LAN_IP no env.txt é o IP do Wi-Fi, em Configurações > Rede.")
        print(" Para fechar: feche a janela do Terminal.")
    else:
        print(" Para fechar: feche a aba do navegador e confirme.")
    print("=" * 50)
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=host, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
