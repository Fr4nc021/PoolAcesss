"""Monta a pasta do pacote, o .zip da release e o manifest.json (sem assinatura).

Rode depois do PyInstaller:
    pyinstaller --noconfirm --clean AcessoPiscinas.spec
    pyinstaller --noconfirm --clean AcessoPiscinas-Atualizador.spec
    python scripts/build_release.py

Recusa o pacote se aparecer .env, env.txt, data/ ou banco .db.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.update_core import (  # noqa: E402
    APP_NAME,
    MANIFEST_NAME,
    UPDATER_EXE,
    is_protected,
    sha256_file,
)
from app.update_key import UPDATE_PUBLIC_KEY  # noqa: E402
from app.version import VERSION  # noqa: E402

LEIA_ME = """AcessoPiscinas - como usar neste PC
==================================

1. Na primeira instalacao, copie env.exemplo.txt para env.txt e preencha
   (IP do facial, senha, SERVER_LAN_IP deste PC). Atualizacoes nunca trocam
   o env.txt nem a pasta data.
2. De dois cliques em AcessoPiscinas.exe
   Na primeira abertura surge o atalho Acesso Piscinas na area de trabalho.
3. O Chrome abre em http://127.0.0.1:5000
4. Em outro PC da rede: http://IP-DESTE-PC:5000

Nao ha janela preta. Para fechar, clique no icone ao lado do relogio
e confirme "Fechar programa". O sistema sincroniza antes de sair.

Quando houver versao nova, aparece um aviso no topo da tela e no icone
ao lado do relogio. Clique em Instalar: o programa fecha, troca os
arquivos e abre de novo sozinho.
"""


def fail(message: str) -> None:
    print(f"ERRO: {message}", file=sys.stderr)
    sys.exit(1)


def forbidden_entries(package: Path) -> list[str]:
    bad: list[str] = []
    for path in package.rglob("*"):
        rel = path.relative_to(package).as_posix()
        if is_protected(rel):
            bad.append(rel)
    return bad


def build_package(dist: Path) -> Path:
    onedir = dist / APP_NAME
    updater = dist / UPDATER_EXE
    if not (onedir / f"{APP_NAME}.exe").is_file():
        fail(f"{onedir / (APP_NAME + '.exe')} não existe. Rode o PyInstaller antes.")
    if not updater.is_file():
        fail(f"{updater} não existe. Rode o PyInstaller do atualizador antes.")

    package = dist / f"{APP_NAME}_Pacote"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(onedir, package)
    shutil.copy2(updater, package / UPDATER_EXE)
    shutil.copy2(ROOT / "assets" / "aquaaccess.ico", package / "aquaaccess.ico")
    shutil.copy2(ROOT / ".env.example", package / ".env.example")
    shutil.copy2(ROOT / ".env.example", package / "env.exemplo.txt")
    (package / "LEIA-ME.txt").write_text(LEIA_ME, encoding="utf-8")

    bad = forbidden_entries(package)
    if bad:
        fail(f"O pacote contém arquivos que não podem ir para a release: {bad[:5]}")
    return package


def build_zip(package: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{APP_NAME}-{VERSION}.zip"
    zip_path.unlink(missing_ok=True)
    files = sorted(p for p in package.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(package).as_posix())
    return zip_path


def build_manifest(package: Path, zip_path: Path, out: Path) -> Path:
    files = {
        path.relative_to(package).as_posix(): sha256_file(path)
        for path in sorted(package.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "app": APP_NAME,
        "version": VERSION,
        "zip_name": zip_path.name,
        "zip_sha256": sha256_file(zip_path),
        "zip_size": zip_path.stat().st_size,
        "commit": os.environ.get("GITHUB_SHA", ""),
        "files": files,
    }
    manifest_path = out / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def check_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        bad = [name for name in archive.namelist() if is_protected(name.rstrip("/"))]
    if bad:
        fail(f"O .zip contém arquivos proibidos: {bad[:5]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    parser.add_argument("--out", default=str(ROOT / "dist" / "release"))
    parser.add_argument("--tag", help="Tag da release. Precisa ser v + app/version.py.")
    parser.add_argument(
        "--require-key",
        action="store_true",
        help="Falha se app/update_key.py estiver sem chave pública.",
    )
    args = parser.parse_args()

    if args.tag and args.tag != f"v{VERSION}":
        fail(f"A tag {args.tag} não bate com VERSION = {VERSION} em app/version.py.")
    if args.require_key and not UPDATE_PUBLIC_KEY.strip():
        fail("app/update_key.py está sem chave pública. Os PCs recusariam esta versão.")

    dist = Path(args.dist)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    package = build_package(dist)
    zip_path = build_zip(package, out)
    check_zip(zip_path)
    manifest_path = build_manifest(package, zip_path, out)
    print(f"Pasta do pacote: {package}")
    print(f"Release:         {zip_path}")
    print(f"Manifest:        {manifest_path} (falta assinar)")


if __name__ == "__main__":
    main()
