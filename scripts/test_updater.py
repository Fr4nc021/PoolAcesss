"""Testa a atualização sem rede: versão, assinatura, extração, troca e rotas."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import updater_main
from app.update_core import (
    APP_NAME,
    UpdateError,
    check_release,
    check_staged_files,
    extract_verified,
    is_newer,
    parse_manifest,
    parse_version,
    sha256_bytes,
    sha256_file,
    sign_manifest,
    verify_signature,
)


def _raises(fn, *args, contains: str = "", **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except UpdateError as exc:
        assert contains.lower() in str(exc).lower(), str(exc)
        return
    raise AssertionError(f"{fn.__name__} deveria recusar {args}")


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, base64.b64encode(public).decode("ascii")


def _manifest_bytes(files: dict[str, bytes], zip_path: Path, version: str = "1.2.0") -> bytes:
    data = {
        "app": APP_NAME,
        "version": version,
        "zip_name": zip_path.name,
        "zip_sha256": sha256_file(zip_path),
        "zip_size": zip_path.stat().st_size,
        "files": {name: sha256_bytes(content) for name, content in files.items()},
    }
    return json.dumps(data).encode("utf-8")


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


GOOD_FILES = {
    "AcessoPiscinas.exe": b"exe novo" * 100,
    "_internal/base_library.zip": b"lib" * 50,
    "AcessoPiscinas-Atualizador.exe": b"atualizador",
}


# --- versão -----------------------------------------------------------------


def test_version_comparison() -> None:
    assert parse_version("1.2.3") == (1, 2, 3)
    assert is_newer("1.2.0", "1.1.9")
    assert is_newer("1.10.0", "1.9.0")
    assert is_newer("2.0.0", "1.99.99")
    assert not is_newer("1.2.0", "1.2.0")
    assert not is_newer("1.1.0", "1.2.0")
    for bad in ("1.2", "1.2.3.4", "v1.2.3", "1.02.3", "1.2.3-beta", "", "a.b.c", " 1.2.3x"):
        _raises(parse_version, bad, contains="inválida")


def test_release_check_needs_matching_tag_and_newer_version() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _zip(Path(tmp) / "AcessoPiscinas-1.2.0.zip", GOOD_FILES)
        manifest = parse_manifest(_manifest_bytes(GOOD_FILES, zip_path))
    check_release(manifest, tag="v1.2.0", current_version="1.1.0")
    _raises(check_release, manifest, tag="v1.3.0", current_version="1.1.0", contains="não bate")
    _raises(check_release, manifest, tag="v1.2.0", current_version="1.2.0", contains="não é mais nova")
    _raises(check_release, manifest, tag="v1.2.0", current_version="1.5.0", contains="não é mais nova")


# --- assinatura -------------------------------------------------------------


def test_signature_verification() -> None:
    key, public = _keypair()
    _, other_public = _keypair()
    manifest = b'{"app": "AcessoPiscinas", "version": "1.2.0"}'
    signature = sign_manifest(manifest, key)

    verify_signature(manifest, signature, public)
    _raises(verify_signature, manifest + b" ", signature, public, contains="não confere")
    _raises(verify_signature, manifest.replace(b"1.2.0", b"9.9.9"), signature, public, contains="não confere")
    _raises(verify_signature, manifest, signature, other_public, contains="não confere")
    _raises(verify_signature, manifest, b"nao-e-base64!!", public, contains="formato")
    _raises(verify_signature, manifest, b"A" * 2000, public, contains="grande")
    _raises(verify_signature, manifest, signature, "", contains="não configurada")
    _raises(verify_signature, manifest, signature, "abc", contains="inválida")


def test_manifest_rejects_protected_and_bad_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _zip(Path(tmp) / "AcessoPiscinas-1.2.0.zip", GOOD_FILES)
        for name in ("data/acessopiscinas.db", ".env", "env.txt", "_update/x", "../fora.txt", "/abs.txt", "C:/win.txt", "sub/../../x"):
            files = dict(GOOD_FILES)
            files[name] = b"x"
            _raises(parse_manifest, _manifest_bytes(files, zip_path))
        data = json.loads(_manifest_bytes(GOOD_FILES, zip_path))
        data["app"] = "Outro"
        _raises(parse_manifest, json.dumps(data).encode(), contains="não é do")
        data = json.loads(_manifest_bytes(GOOD_FILES, zip_path))
        data["zip_name"] = "../x.zip"
        _raises(parse_manifest, json.dumps(data).encode(), contains="nome do pacote")


# --- extração ---------------------------------------------------------------


def test_safe_extraction_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        zip_path = _zip(root / "AcessoPiscinas-1.2.0.zip", GOOD_FILES)
        manifest = parse_manifest(_manifest_bytes(GOOD_FILES, zip_path))
        dest = root / "_update" / "staging" / "1.2.0"
        extract_verified(zip_path, dest, manifest)
        for name, content in GOOD_FILES.items():
            assert (dest / name).read_bytes() == content
        check_staged_files(dest, manifest)
        (dest / "extra.txt").write_text("x")
        _raises(check_staged_files, dest, manifest, contains="mudou")


def _extract_should_fail(entries: dict[str, bytes], manifest_files: dict[str, bytes], contains: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        zip_path = _zip(root / "AcessoPiscinas-1.2.0.zip", entries)
        manifest = parse_manifest(_manifest_bytes(manifest_files, zip_path))
        dest = root / "staging"
        _raises(extract_verified, zip_path, dest, manifest, contains=contains)
        assert not dest.exists(), "extração recusada não pode deixar pasta pela metade"
        assert not (root / "fora.txt").exists()


def test_extraction_rejects_zip_slip_and_protected() -> None:
    for evil in ("../fora.txt", "..\\fora.txt", "/fora.txt", "C:/fora.txt", "a/../../fora.txt"):
        entries = dict(GOOD_FILES)
        entries[evil] = b"ataque"
        _extract_should_fail(entries, GOOD_FILES, "recusado")
    for protected in ("data/acessopiscinas.db", ".env", "env.txt", "_update/runner/x.exe", "DATA/fotos/a.jpg", "sub/.env"):
        entries = dict(GOOD_FILES)
        entries[protected] = b"troca"
        _extract_should_fail(entries, GOOD_FILES, "protegido")


def test_extraction_rejects_tampered_or_unlisted_files() -> None:
    entries = dict(GOOD_FILES)
    entries["AcessoPiscinas.exe"] = b"exe trocado"
    _extract_should_fail(entries, GOOD_FILES, "hash não confere")

    entries = dict(GOOD_FILES)
    entries["virus.dll"] = b"x"
    _extract_should_fail(entries, GOOD_FILES, "fora do manifest")

    entries = dict(GOOD_FILES)
    del entries["_internal/base_library.zip"]
    _extract_should_fail(entries, GOOD_FILES, "faltam")


def test_extraction_rejects_symlink_and_size_limit() -> None:
    import app.update_core as core

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        zip_path = root / "AcessoPiscinas-1.2.0.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for name, content in GOOD_FILES.items():
                archive.writestr(name, content)
            link = zipfile.ZipInfo("link")
            link.external_attr = (0o120777 << 16)
            archive.writestr(link, "C:/Windows")
        files = dict(GOOD_FILES)
        files["link"] = b"C:/Windows"
        manifest = parse_manifest(_manifest_bytes(files, zip_path))
        _raises(extract_verified, zip_path, root / "staging", manifest, contains="link")

    old = core.MAX_EXTRACTED_BYTES
    core.MAX_EXTRACTED_BYTES = 100
    try:
        _extract_should_fail(GOOD_FILES, GOOD_FILES, "grande demais")
    finally:
        core.MAX_EXTRACTED_BYTES = old


# --- atualizador: troca e rollback ------------------------------------------


def _install_layout(root: Path) -> tuple[Path, Path]:
    install = root / "AcessoPiscinas"
    (install / "_internal").mkdir(parents=True)
    (install / "AcessoPiscinas.exe").write_text("velho")
    (install / "_internal" / "lib.dll").write_text("lib velha")
    (install / "data" / "photos").mkdir(parents=True)
    (install / "data" / "acessopiscinas.db").write_text("banco original")
    (install / "data" / "photos" / "a.jpg").write_text("foto")
    (install / ".env").write_text("DEVICE_PASSWORD=segredo")
    (install / "env.txt").write_text("SERVER_LAN_IP=1.2.3.4")
    staging = install / "_update" / "staging" / "1.2.0"
    (staging / "_internal").mkdir(parents=True)
    (staging / "AcessoPiscinas.exe").write_text("novo")
    (staging / "_internal" / "lib.dll").write_text("lib nova")
    (staging / "LEIA-ME.txt").write_text("leia")
    return install, staging


def _plan(install: Path, staging: Path) -> updater_main.Plan:
    update = install / "_update"
    return updater_main.Plan(
        {
            "install_dir": str(install),
            "staging_dir": str(staging),
            "backup_root": str(update / "backup"),
            "result_file": str(update / "ultimo-resultado.json"),
            "log_file": str(update / "atualizador.log"),
            "app_exe": "AcessoPiscinas.exe",
            "pid": 999999,
            "port": 5999,
            "from_version": "1.0.0",
            "to_version": "1.2.0",
        }
    )


def _untouched(install: Path) -> None:
    assert (install / "data" / "photos" / "a.jpg").read_text() == "foto"
    assert (install / ".env").read_text() == "DEVICE_PASSWORD=segredo"
    assert (install / "env.txt").read_text() == "SERVER_LAN_IP=1.2.3.4"


def test_updater_swaps_and_keeps_protected_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install, staging = _install_layout(Path(tmp))
        plan = _plan(install, staging)
        started: list[Path] = []
        updater_main.wait_for_exit = lambda pid, timeout: True
        updater_main.start_app = lambda d, exe: started.append(d) or object()
        updater_main.time.sleep = lambda s: None
        assert updater_main.run(plan, health=lambda *a: True)
        assert (install / "AcessoPiscinas.exe").read_text() == "novo"
        assert (install / "_internal" / "lib.dll").read_text() == "lib nova"
        assert (install / "data" / "acessopiscinas.db").read_text() == "banco original"
        _untouched(install)
        assert not staging.exists()
        result = json.loads((install / "_update" / "ultimo-resultado.json").read_text(encoding="utf-8"))
        assert result["ok"] is True
        backups = list((install / "_update" / "backup").iterdir())
        assert len(backups) == 1
        assert (backups[0] / "app" / "AcessoPiscinas.exe").read_text() == "velho"
        assert (backups[0] / "data" / "acessopiscinas.db").read_text() == "banco original"


def test_updater_rolls_back_when_health_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install, staging = _install_layout(Path(tmp))
        plan = _plan(install, staging)
        started: list[Path] = []
        updater_main.wait_for_exit = lambda pid, timeout: True
        updater_main.start_app = lambda d, exe: started.append(d) or object()
        updater_main._stop = lambda process: None
        updater_main.time.sleep = lambda s: None

        def broken_health(*_args) -> bool:
            # A versão nova mexeu no banco antes de cair.
            (install / "data" / "acessopiscinas.db").write_text("banco migrado")
            (install / "data" / "acessopiscinas.db-wal").write_text("wal novo")
            return False

        assert not updater_main.run(plan, health=broken_health)
        assert (install / "AcessoPiscinas.exe").read_text() == "velho"
        assert (install / "_internal" / "lib.dll").read_text() == "lib velha"
        assert not (install / "LEIA-ME.txt").exists()
        assert (install / "data" / "acessopiscinas.db").read_text() == "banco original"
        assert not (install / "data" / "acessopiscinas.db-wal").exists()
        _untouched(install)
        assert len(started) == 2, "abre a nova e depois a anterior"
        result = json.loads((install / "_update" / "ultimo-resultado.json").read_text(encoding="utf-8"))
        assert result["ok"] is False and "restaurada" in result["message"]


def test_updater_does_nothing_if_app_stays_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install, staging = _install_layout(Path(tmp))
        plan = _plan(install, staging)
        updater_main.wait_for_exit = lambda pid, timeout: False
        assert not updater_main.run(plan, health=lambda *a: True)
        assert (install / "AcessoPiscinas.exe").read_text() == "velho"


def test_plan_rejects_paths_outside_update_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install, staging = _install_layout(Path(tmp))
        data = {
            "install_dir": str(install),
            "staging_dir": str(Path(tmp) / "outro"),
            "backup_root": str(install / "_update" / "backup"),
            "result_file": str(install / "_update" / "r.json"),
            "log_file": str(install / "_update" / "l.log"),
            "pid": 1,
            "port": 5000,
        }
        try:
            updater_main.Plan(data)
        except ValueError:
            pass
        else:
            raise AssertionError("plano com pasta fora de _update deveria ser recusado")
        data["staging_dir"] = str(staging)
        data["app_exe"] = "..\\cmd.exe"
        try:
            updater_main.Plan(data)
        except ValueError:
            return
        raise AssertionError("nome de programa com caminho deveria ser recusado")


# --- rotas ------------------------------------------------------------------


def test_install_route_is_local_only_and_blocked_during_registration() -> None:
    import app.config as config
    import app.db as db
    from app import updater
    from app.main import create_app

    config.reload_env = lambda: None  # noqa: E731
    # O banco fica aberto pelas threads de fundo até o processo sair.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        config.DATA_DIR = root
        config.PHOTOS_DIR = root / "photos"
        config.PHOTOS_DIR.mkdir()
        config.DB_PATH = root / "t.db"
        db.DB_PATH = config.DB_PATH
        os.environ["SYNC_PEER_URL"] = ""
        os.environ["SUPABASE_URL"] = ""

        flask_app = create_app()
        client = flask_app.test_client()
        port = config.PORT
        local = {"REMOTE_ADDR": "127.0.0.1"}
        headers = {"X-Acesso-Local": "1"}

        saude = client.get("/saude?rapido=1")
        assert saude.get_json()["version"]

        response = client.post(
            "/atualizacao/instalar",
            environ_base={"REMOTE_ADDR": "192.168.0.50"},
            headers={**headers, "Host": f"127.0.0.1:{port}"},
        )
        assert response.status_code == 403, "outro PC da rede não instala"

        response = client.post(
            "/atualizacao/instalar",
            environ_base=local,
            headers={"Host": f"127.0.0.1:{port}"},
        )
        assert response.status_code == 403, "sem o header local é recusado"

        response = client.post(
            "/atualizacao/instalar",
            environ_base=local,
            headers={**headers, "Host": f"127.0.0.1:{port}", "Origin": "http://site-mal.com"},
        )
        assert response.status_code == 403, "Origin de outro site é recusada"

        response = client.post(
            "/atualizacao/instalar",
            environ_base=local,
            headers={**headers, "Host": f"evil.com:{port}"},
        )
        assert response.status_code == 403, "Host estranho (DNS rebinding) é recusado"

        response = client.post(
            "/atualizacao/instalar",
            environ_base=local,
            headers={**headers, "Host": f"127.0.0.1:{port}"},
        )
        assert response.status_code == 409, "sem atualização pronta, nada a instalar"

        updater._staged = root
        updater._staged_manifest = object()
        updater._set("ready", "pronta", "9.9.9")
        try:
            with updater.busy_operation():
                response = client.post(
                    "/atualizacao/instalar",
                    environ_base=local,
                    headers={**headers, "Host": f"127.0.0.1:{port}"},
                )
            assert response.status_code == 409
            assert "cadastro em andamento" in response.get_json()["error"]

            with updater._lock:
                updater._installing = True
            response = client.post(
                "/cadastrar",
                environ_base=local,
                data={"name": "Ana"},
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 503, "cadastro durante a instalação é recusado"
        finally:
            with updater._lock:
                updater._installing = False
            updater._staged = None
            updater._staged_manifest = None
            updater._set("idle")
            from app.expiry import stop_expiry_worker
            from app.sync import stop_sync_worker

            stop_sync_worker()
            stop_expiry_worker()


def main() -> None:
    test_version_comparison()
    test_release_check_needs_matching_tag_and_newer_version()
    test_signature_verification()
    test_manifest_rejects_protected_and_bad_paths()
    test_safe_extraction_happy_path()
    test_extraction_rejects_zip_slip_and_protected()
    test_extraction_rejects_tampered_or_unlisted_files()
    test_extraction_rejects_symlink_and_size_limit()
    test_updater_swaps_and_keeps_protected_files()
    test_updater_rolls_back_when_health_fails()
    test_updater_does_nothing_if_app_stays_open()
    test_plan_rejects_paths_outside_update_dir()
    test_install_route_is_local_only_and_blocked_during_registration()
    print("updater ok")


if __name__ == "__main__":
    main()
