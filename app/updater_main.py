"""AcessoPiscinas-Atualizador.exe: troca os arquivos com o programa fechado.

Usa só a biblioteca padrão. Roda a partir de _update/runner, então pode
trocar a cópia de si mesmo que fica na pasta do programa.

Passos: espera o programa fechar, guarda o banco e os arquivos atuais,
copia a versão nova, abre o programa e espera o /saude responder com a
versão nova. Se não responder, desfaz tudo e abre a versão anterior.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

PROTECTED_NAMES = frozenset({"data", ".env", "env.txt", "_update"})
DB_FILES = ("acessopiscinas.db", "acessopiscinas.db-wal", "acessopiscinas.db-shm")
WAIT_EXIT_SECONDS = 120
HEALTH_SECONDS = 90

_log_path: Optional[Path] = None


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    if _log_path is not None:
        try:
            with open(_log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 — sem console no .exe
        pass


class Plan:
    def __init__(self, data: dict) -> None:
        self.install_dir = Path(data["install_dir"]).resolve()
        update_dir = self.install_dir / "_update"
        self.staging_dir = Path(data["staging_dir"]).resolve()
        self.backup_root = Path(data["backup_root"]).resolve()
        self.result_file = Path(data["result_file"]).resolve()
        self.log_file = Path(data["log_file"]).resolve()
        for path in (self.staging_dir, self.backup_root, self.result_file, self.log_file):
            if update_dir.resolve() not in path.parents:
                raise ValueError(f"Caminho fora de _update: {path}")
        self.app_exe = str(data.get("app_exe") or "AcessoPiscinas.exe")
        if Path(self.app_exe).name != self.app_exe:
            raise ValueError("Nome do programa inválido no plano")
        self.pid = int(data["pid"])
        self.port = int(data["port"])
        self.from_version = str(data.get("from_version") or "")
        self.to_version = str(data.get("to_version") or "")


def wait_for_exit(pid: int, timeout: float) -> bool:
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return True
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return result == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def _retry(action: Callable[[], None], what: str, attempts: int = 10) -> None:
    """Antivírus e o Windows seguram arquivos por alguns segundos."""
    for attempt in range(attempts):
        try:
            action()
            return
        except OSError as exc:
            if attempt + 1 == attempts:
                raise
            log(f"{what} ocupado ({exc}); tentando de novo")
            time.sleep(1.0)


def backup_database(install_dir: Path, backup_dir: Path) -> list[str]:
    saved: list[str] = []
    data_dir = install_dir / "data"
    target = backup_dir / "data"
    for name in DB_FILES:
        source = data_dir / name
        if source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
            saved.append(name)
    return saved


def restore_database(install_dir: Path, backup_dir: Path, saved: list[str]) -> None:
    data_dir = install_dir / "data"
    for name in DB_FILES:
        current = data_dir / name
        if name in saved:
            _retry(lambda n=name: shutil.copy2(backup_dir / "data" / n, data_dir / n), name)
        elif current.exists():
            # -wal/-shm criados pela versão nova não combinam com o banco antigo.
            _retry(lambda c=current: c.unlink(), name)


class Swap:
    """Troca arquivo por arquivo e lembra o que fazer para desfazer.

    Não move pastas: o Windows recusa mover uma pasta aberta no Explorador
    ou sendo lida pelo antivírus, mas aceita trocar os arquivos dentro dela.
    """

    def __init__(self, install_dir: Path, staging_dir: Path, backup_dir: Path) -> None:
        self.install_dir = install_dir
        self.staging_dir = staging_dir
        self.app_backup = backup_dir / "app"
        self.replaced: list[str] = []
        self.added: list[str] = []
        self.removed: list[str] = []

    def names(self) -> list[str]:
        names = sorted(item.name for item in self.staging_dir.iterdir())
        for name in names:
            if name.lower() in PROTECTED_NAMES or name.lower().endswith(".db"):
                raise ValueError(f"Item protegido na pasta da atualização: {name}")
        return names

    def new_files(self) -> list[str]:
        files: list[str] = []
        for name in self.names():
            source = self.staging_dir / name
            if source.is_dir():
                files.extend(
                    path.relative_to(self.staging_dir).as_posix()
                    for path in sorted(source.rglob("*"))
                    if path.is_file()
                )
            else:
                files.append(name)
        return files

    def _put(self, source: Path, target: Path, what: str) -> None:
        """Copia ao lado e troca de nome: o arquivo nunca fica pela metade."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".novo")
        shutil.copy2(source, temp)
        _retry(lambda: os.replace(temp, target), what)

    def apply(self) -> None:
        self.app_backup.mkdir(parents=True, exist_ok=True)
        new_files = self.new_files()
        for rel in new_files:
            target = self.install_dir / rel
            if target.is_file():
                backup = self.app_backup / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                self.replaced.append(rel)
            else:
                self.added.append(rel)
            self._put(self.staging_dir / rel, target, rel)
        log(f"Arquivos trocados: {len(self.replaced)}, novos: {len(self.added)}")

        # Sobras da versão anterior dentro das pastas trocadas (ex.: _internal).
        keep = set(new_files)
        for name in self.names():
            folder = self.install_dir / name
            if not (self.staging_dir / name).is_dir() or not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                rel = path.relative_to(self.install_dir).as_posix()
                if not path.is_file() or rel in keep:
                    continue
                backup = self.app_backup / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(path, backup)
                    self.removed.append(rel)
                except OSError as exc:
                    log(f"Sobra antiga mantida ({rel}): {exc}")

    def rollback(self) -> None:
        for rel in reversed(self.added):
            target = self.install_dir / rel
            if target.exists():
                _retry(lambda t=target: t.unlink(), rel)
        for rel in reversed(self.replaced):
            self._put(self.app_backup / rel, self.install_dir / rel, rel)
        for rel in reversed(self.removed):
            target = self.install_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _retry(lambda r=rel, t=target: os.replace(self.app_backup / r, t), rel)
        # Pastas que só a versão nova criou.
        for rel in sorted({str(Path(r).parent) for r in self.added}, key=len, reverse=True):
            folder = self.install_dir / rel
            while folder != self.install_dir and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
        log("Arquivos da versão anterior restaurados")


def start_app(install_dir: Path, app_exe: str) -> Optional[subprocess.Popen]:
    exe = install_dir / app_exe
    if not exe.is_file():
        log(f"Programa não encontrado: {exe}")
        return None
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([str(exe)], cwd=str(install_dir), creationflags=flags)


def health_ok(
    port: int,
    version: str,
    timeout: float,
    process: Optional[subprocess.Popen] = None,
) -> bool:
    url = f"http://127.0.0.1:{port}/saude?rapido=1"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            log(f"O programa novo fechou sozinho (código {process.returncode})")
            return False
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("app") == "ok" and data.get("version") == version:
                return True
            log(f"/saude respondeu outra versão: {data.get('version')}")
        except Exception:  # noqa: BLE001 — ainda subindo
            pass
        time.sleep(2.0)
    return False


def _stop(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.kill()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log("O programa novo não fechou depois do kill")


def _write_result(plan: Plan, ok: bool, message: str) -> None:
    data = {
        "ok": ok,
        "from_version": plan.from_version,
        "to_version": plan.to_version,
        "message": message,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    plan.result_file.parent.mkdir(parents=True, exist_ok=True)
    plan.result_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_after_success(plan: Plan, keep: Path) -> None:
    shutil.rmtree(plan.staging_dir, ignore_errors=True)
    flag = plan.staging_dir.parent / f"{plan.staging_dir.name}.ok"
    flag.unlink(missing_ok=True)
    if plan.backup_root.is_dir():
        for old in plan.backup_root.iterdir():
            if old.is_dir() and old != keep:
                shutil.rmtree(old, ignore_errors=True)


def run(plan: Plan, *, health: Callable[..., bool] = health_ok) -> bool:
    log(f"Atualizando {plan.from_version} -> {plan.to_version} em {plan.install_dir}")
    if not wait_for_exit(plan.pid, WAIT_EXIT_SECONDS):
        message = "O programa não fechou a tempo. Nada foi trocado."
        log(message)
        _write_result(plan, False, message)
        return False
    time.sleep(1.0)

    backup_dir = plan.backup_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    saved = backup_database(plan.install_dir, backup_dir)
    log(f"Banco guardado em {backup_dir} ({', '.join(saved) or 'sem banco'})")

    swap = Swap(plan.install_dir, plan.staging_dir, backup_dir)
    process: Optional[subprocess.Popen] = None
    try:
        swap.apply()
        process = start_app(plan.install_dir, plan.app_exe)
        if process is None:
            raise RuntimeError("programa novo não encontrado")
        if not health(plan.port, plan.to_version, HEALTH_SECONDS, process):
            raise RuntimeError("o programa novo não respondeu no /saude")
    except Exception as exc:  # noqa: BLE001 — qualquer falha desfaz a troca
        log(f"Falha na atualização: {exc}. Desfazendo.")
        _stop(process)
        try:
            swap.rollback()
            restore_database(plan.install_dir, backup_dir, saved)
        except Exception as rollback_exc:  # noqa: BLE001
            message = (
                f"Falha ao desfazer a atualização: {rollback_exc}. "
                f"Os arquivos anteriores estão em {backup_dir}."
            )
            log(message)
            _write_result(plan, False, message)
            return False
        _write_result(
            plan,
            False,
            f"A versão {plan.to_version} não abriu ({exc}). "
            f"A versão {plan.from_version} foi restaurada.",
        )
        start_app(plan.install_dir, plan.app_exe)
        return False

    _write_result(plan, True, f"Programa atualizado para a versão {plan.to_version}.")
    _cleanup_after_success(plan, backup_dir)
    log("Atualização concluída")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    global _log_path
    parser = argparse.ArgumentParser(description="Atualizador do Acesso Piscinas")
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    try:
        plan = Plan(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        print(f"Plano inválido: {exc}", file=sys.stderr)
        return 2
    _log_path = plan.log_file
    return 0 if run(plan) else 1


if __name__ == "__main__":
    sys.exit(main())
