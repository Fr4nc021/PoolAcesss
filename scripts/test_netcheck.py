"""Testa o aviso de SERVER_LAN_IP que não é deste PC, sem rede e sem facial."""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config
import app.netcheck as netcheck

netcheck.reload_env = lambda: None  # noqa: E731
config.reload_env = lambda: None  # noqa: E731
netcheck.on_chromeos = lambda: False  # noqa: E731
os.environ["DEVICE_URL"] = "http://192.168.15.86"
os.environ["DEVICE_URL_2"] = "http://192.168.15.44"

THIS_PC = frozenset({"192.168.15.8", "192.168.56.1"})


def test_matching_ip_has_no_warning() -> None:
    os.environ["SERVER_LAN_IP"] = "192.168.15.8"
    assert netcheck.lan_ip_problem(THIS_PC) == ""


def test_ip_of_other_pc_is_reported_with_the_right_suggestion() -> None:
    os.environ["SERVER_LAN_IP"] = "192.168.15.4"
    message = netcheck.lan_ip_problem(THIS_PC)
    assert "192.168.15.4" in message and "não é deste PC" in message
    # Sugere o IP da mesma rede dos faciais, não o da placa virtual.
    assert "Troque para 192.168.15.8 em ADM > Configurações" in message


def test_empty_and_invalid_values() -> None:
    os.environ["SERVER_LAN_IP"] = ""
    assert "vazio" in netcheck.lan_ip_problem(THIS_PC)
    os.environ["SERVER_LAN_IP"] = "pc-do-medico"
    assert "não é um endereço IP" in netcheck.lan_ip_problem(THIS_PC)


def test_unknown_local_ips_do_not_block() -> None:
    os.environ["SERVER_LAN_IP"] = "192.168.15.4"
    assert netcheck.lan_ip_problem(frozenset()) == ""


def test_chromeos_is_not_compared() -> None:
    os.environ["SERVER_LAN_IP"] = "192.168.15.4"
    netcheck.on_chromeos = lambda: True  # noqa: E731
    try:
        assert netcheck.lan_ip_problem(THIS_PC) == ""
    finally:
        netcheck.on_chromeos = lambda: False  # noqa: E731


def test_registration_is_refused_before_saving_the_photo() -> None:
    import app.db as db
    import app.routes as routes
    from app.main import create_app

    os.environ["SERVER_LAN_IP"] = "192.168.15.4"
    original = routes.lan_ip_problem
    routes.lan_ip_problem = lambda: netcheck.lan_ip_problem(THIS_PC)  # noqa: E731
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        config.DATA_DIR = root
        config.PHOTOS_DIR = root / "photos"
        config.PHOTOS_DIR.mkdir()
        routes.PHOTOS_DIR = config.PHOTOS_DIR
        config.DB_PATH = root / "t.db"
        db.DB_PATH = config.DB_PATH
        os.environ["SYNC_PEER_URL"] = ""
        os.environ["SUPABASE_URL"] = ""
        try:
            client = create_app().test_client()
            response = client.post(
                "/cadastrar",
                data={
                    "name": "Teste",
                    "pool": "normal",
                    "photo": (io.BytesIO(b"\xff\xd8\xff" + b"0" * 2000), "rosto.jpg"),
                },
                headers={"Accept": "application/json"},
                content_type="multipart/form-data",
            )
            assert response.status_code == 500
            assert "não é deste PC" in response.get_json()["error"]
            assert not any(config.PHOTOS_DIR.iterdir()), "a foto não pode ficar salva"

            page = client.get("/membros").get_data(as_text=True)
            assert 'id="lan-ip-warning"' in page
        finally:
            routes.lan_ip_problem = original
            from app.expiry import stop_expiry_worker
            from app.sync import stop_sync_worker

            stop_sync_worker()
            stop_expiry_worker()


def main() -> None:
    test_matching_ip_has_no_warning()
    test_ip_of_other_pc_is_reported_with_the_right_suggestion()
    test_empty_and_invalid_values()
    test_unknown_local_ips_do_not_block()
    test_chromeos_is_not_compared()
    test_registration_is_refused_before_saving_the_photo()
    print("netcheck ok")


if __name__ == "__main__":
    main()
