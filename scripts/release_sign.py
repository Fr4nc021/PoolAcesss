"""Confere o pacote gerado pelo GitHub Actions e assina o manifest.json.

    python scripts/release_sign.py --manifest manifest.json \\
        --zip AcessoPiscinas-1.2.0.zip --key C:\\chaves\\acessopiscinas-release.pem

Antes de assinar, confere hash e tamanho do .zip, extrai numa pasta
temporária e compara cada arquivo com o manifest. Grava manifest.sig
ao lado do manifest.json.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.update_core import (  # noqa: E402
    SIGNATURE_NAME,
    UpdateError,
    check_zip_file,
    extract_verified,
    parse_manifest,
    sign_manifest,
    verify_signature,
)
from app.update_key import UPDATE_PUBLIC_KEY  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--key", required=True, help="Chave privada .pem gerada pelo release_keygen.py")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    zip_path = Path(args.zip)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = parse_manifest(manifest_bytes)
        if zip_path.name != manifest.zip_name:
            raise UpdateError(f"O manifest fala de {manifest.zip_name}, não de {zip_path.name}.")
        check_zip_file(zip_path, manifest)
        with tempfile.TemporaryDirectory() as tmp:
            extract_verified(zip_path, Path(tmp) / "pacote", manifest)
    except UpdateError as exc:
        print(f"ERRO: pacote recusado: {exc}", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Senha da chave privada: ")
    private_key = serialization.load_pem_private_key(
        Path(args.key).read_bytes(),
        password=password.encode("utf-8"),
    )
    public_text = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    if public_text != UPDATE_PUBLIC_KEY.strip():
        print(
            "ERRO: esta chave não é a que está em app/update_key.py. "
            "Os PCs recusariam a assinatura.",
            file=sys.stderr,
        )
        sys.exit(1)

    signature = sign_manifest(manifest_bytes, private_key)
    verify_signature(manifest_bytes, signature, public_text)
    signature_path = manifest_path.with_name(SIGNATURE_NAME)
    signature_path.write_bytes(signature)
    print(f"Versão {manifest.version} conferida ({len(manifest.files)} arquivos).")
    print(f"Assinatura gravada em {signature_path}")


if __name__ == "__main__":
    main()
