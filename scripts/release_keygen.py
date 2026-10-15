"""Gera o par de chaves Ed25519 que assina as releases. Rode uma vez só.

    python scripts/release_keygen.py C:\\chaves\\acessopiscinas-release.pem

A chave privada é gravada com senha, fora da pasta do projeto. O valor
impresso no final vai para UPDATE_PUBLIC_KEY em app/update_key.py.
"""

from __future__ import annotations

import base64
import getpass
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    target = Path(sys.argv[1]).expanduser().resolve()
    if target == ROOT or ROOT in target.parents:
        print("ERRO: grave a chave privada fora da pasta do projeto.", file=sys.stderr)
        sys.exit(1)
    if target.exists():
        print(f"ERRO: {target} já existe. Não sobrescrevo chave.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Senha para proteger a chave privada: ")
    if len(password) < 12:
        print("ERRO: use uma senha com pelo menos 12 caracteres.", file=sys.stderr)
        sys.exit(1)
    if getpass.getpass("Repita a senha: ") != password:
        print("ERRO: as senhas não conferem.", file=sys.stderr)
        sys.exit(1)

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pem)

    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_text = base64.b64encode(public_raw).decode("ascii")
    print(f"Chave privada gravada em {target}")
    print("Guarde uma cópia em lugar seguro. Sem ela, nenhuma versão nova é aceita.")
    print()
    print("Cole em app/update_key.py:")
    print(f'UPDATE_PUBLIC_KEY = "{public_text}"')


if __name__ == "__main__":
    main()
