"""Regras da atualização: versão, assinatura do manifest e extração do .zip.

Nada aqui acessa a rede. O download fica em app/updater.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

APP_NAME = "AcessoPiscinas"
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"
UPDATER_EXE = "AcessoPiscinas-Atualizador.exe"

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_ZIP_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_BYTES = 600 * 1024 * 1024
MAX_FILES = 5000

# Nomes da pasta do programa que a atualização nunca cria, troca ou apaga.
PROTECTED_NAMES = frozenset({"data", ".env", "env.txt", "_update"})

_VERSION_RE = re.compile(r"^(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """Atualização recusada. A mensagem vai para a tela."""


def parse_version(text: str) -> tuple[int, int, int]:
    """Aceita só X.Y.Z com números. Qualquer outra forma é recusada."""
    match = _VERSION_RE.match(str(text).strip())
    if not match:
        raise UpdateError(f"Versão inválida: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_protected(rel_path: str) -> bool:
    """data/, .env, env.txt e _update/ ficam fora de qualquer atualização."""
    first = PurePosixPath(rel_path).parts[0].lower() if rel_path else ""
    name = PurePosixPath(rel_path).name.lower()
    return first in PROTECTED_NAMES or name in {".env", "env.txt"} or name.endswith(".db")


def safe_relative_path(name: str) -> str:
    """Normaliza um caminho do .zip. Recusa absoluto, unidade, .. e protegidos."""
    raw = name.replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
        raise UpdateError(f"Caminho recusado no pacote: {name!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise UpdateError(f"Caminho recusado no pacote: {name!r}")
    if any(":" in part for part in parts):
        raise UpdateError(f"Caminho recusado no pacote: {name!r}")
    rel = "/".join(parts)
    if is_protected(rel):
        raise UpdateError(f"O pacote tenta trocar um arquivo protegido: {rel}")
    return rel


def load_public_key(text: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    value = (text or "").strip()
    if not value:
        raise UpdateError("Chave pública de atualização não configurada.")
    try:
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("Chave pública de atualização inválida.") from exc


def verify_signature(manifest_bytes: bytes, signature_text: bytes, public_key_text: str) -> None:
    from cryptography.exceptions import InvalidSignature

    key = load_public_key(public_key_text)
    if len(signature_text) > MAX_SIGNATURE_BYTES:
        raise UpdateError("Assinatura grande demais.")
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("Assinatura em formato inválido.") from exc
    try:
        key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise UpdateError("Assinatura da atualização não confere. Nada foi instalado.") from exc


def sign_manifest(manifest_bytes: bytes, private_key) -> bytes:
    """Usado pelo script de publicação e pelos testes."""
    return base64.b64encode(private_key.sign(manifest_bytes)) + b"\n"


@dataclass
class Manifest:
    version: str
    zip_name: str
    zip_sha256: str
    zip_size: int
    files: dict[str, str]


def parse_manifest(manifest_bytes: bytes) -> Manifest:
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise UpdateError("Manifest grande demais.")
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("Manifest ilegível.") from exc
    if not isinstance(data, dict) or data.get("app") != APP_NAME:
        raise UpdateError("Manifest não é do Acesso Piscinas.")
    version = str(data.get("version", ""))
    parse_version(version)
    zip_name = str(data.get("zip_name", ""))
    if not re.match(r"^[A-Za-z0-9._-]+\.zip$", zip_name):
        raise UpdateError("Nome do pacote inválido no manifest.")
    zip_sha256 = str(data.get("zip_sha256", "")).lower()
    if not _SHA256_RE.match(zip_sha256):
        raise UpdateError("Hash do pacote inválido no manifest.")
    zip_size = data.get("zip_size")
    if not isinstance(zip_size, int) or zip_size <= 0 or zip_size > MAX_ZIP_BYTES:
        raise UpdateError("Tamanho do pacote inválido no manifest.")
    raw_files = data.get("files")
    if not isinstance(raw_files, dict) or not raw_files or len(raw_files) > MAX_FILES:
        raise UpdateError("Lista de arquivos inválida no manifest.")
    files: dict[str, str] = {}
    for name, digest in raw_files.items():
        rel = safe_relative_path(str(name))
        if rel != name:
            raise UpdateError(f"Caminho fora do padrão no manifest: {name!r}")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise UpdateError(f"Hash inválido para {name}.")
        files[rel] = digest
    return Manifest(version, zip_name, zip_sha256, zip_size, files)


def check_release(manifest: Manifest, *, tag: str, current_version: str) -> None:
    """A tag precisa bater com a versão assinada, e só versão maior passa."""
    if tag != f"v{manifest.version}":
        raise UpdateError(
            f"A tag {tag} não bate com a versão assinada {manifest.version}."
        )
    if not is_newer(manifest.version, current_version):
        raise UpdateError(
            f"Versão {manifest.version} não é mais nova que a instalada ({current_version})."
        )


def check_zip_file(zip_path: Path, manifest: Manifest) -> None:
    size = zip_path.stat().st_size
    if size != manifest.zip_size:
        raise UpdateError("Tamanho do pacote baixado não confere com o manifest.")
    if sha256_file(zip_path) != manifest.zip_sha256:
        raise UpdateError("Hash do pacote baixado não confere com o manifest.")


def check_staged_files(staging: Path, manifest: Manifest) -> None:
    """Confere de novo a pasta extraída antes de entregar ao atualizador."""
    found: set[str] = set()
    for path in staging.rglob("*"):
        if path.is_symlink():
            raise UpdateError(f"Link simbólico na pasta da atualização: {path.name}")
        if path.is_file():
            found.add(path.relative_to(staging).as_posix())
    if found != set(manifest.files):
        raise UpdateError("A pasta da atualização mudou depois da conferência.")
    for rel, expected in manifest.files.items():
        if sha256_file(staging / rel) != expected:
            raise UpdateError(f"Hash não confere na pasta da atualização: {rel}")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def extract_verified(zip_path: Path, dest: Path, manifest: Manifest) -> None:
    """Extrai em dest só o que o manifest assinado lista, com o hash certo.

    Em qualquer problema, dest é apagada e nada sobra pela metade.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise UpdateError("Pacote com arquivos demais.")
            total = 0
            seen: set[str] = set()
            for info in infos:
                if info.is_dir():
                    safe_relative_path(info.filename)
                    continue
                if _is_symlink(info):
                    raise UpdateError(f"Link simbólico recusado: {info.filename}")
                rel = safe_relative_path(info.filename)
                if rel in seen:
                    raise UpdateError(f"Arquivo repetido no pacote: {rel}")
                expected = manifest.files.get(rel)
                if expected is None:
                    raise UpdateError(f"Arquivo fora do manifest: {rel}")
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise UpdateError("Pacote grande demais depois de extraído.")
                target = (dest / rel).resolve()
                if root not in target.parents:
                    raise UpdateError(f"Caminho recusado no pacote: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with archive.open(info) as src, open(target, "wb") as out:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > info.file_size:
                            raise UpdateError(f"Tamanho falso no pacote: {rel}")
                        digest.update(chunk)
                        out.write(chunk)
                if digest.hexdigest() != expected:
                    raise UpdateError(f"Hash não confere: {rel}")
                seen.add(rel)
            missing = set(manifest.files) - seen
            if missing:
                raise UpdateError(f"Faltam arquivos no pacote: {sorted(missing)[:3]}")
    except zipfile.BadZipFile as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise UpdateError("Pacote .zip corrompido.") from exc
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
