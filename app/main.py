from __future__ import annotations

import logging
import threading
import time
import webbrowser

from flask import Flask

from app import db
from app.config import (
    HOST,
    PORT,
    RESOURCE_DIR,
    SECRET_KEY,
    SERVER_LAN_IP,
    ensure_directories,
)
from app.expiry import start_expiry_worker
from app.routes import bp


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
    def _ensure_expiry_worker():
        start_expiry_worker()

    return app


def _open_browser() -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


def main() -> None:
    app = create_app()
    start_expiry_worker()
    print("=" * 50)
    print(" Acesso Piscinas Clube Aliança")
    print(f" Neste PC:   http://127.0.0.1:{PORT}")
    print(f" Na rede:    http://{SERVER_LAN_IP}:{PORT}")
    print(" Feche esta janela para encerrar.")
    print("=" * 50)
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
