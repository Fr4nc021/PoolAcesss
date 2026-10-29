"""Corrige nos faciais o que a conferência de fotos acha e tem uma resposta certa.

Seguro de corrigir sozinho, sempre com a foto salva no sistema:
- FaceID repetido, sem modelo do rosto ou fora do facial: recria o sócio,
  que ganha um FaceID novo;
- vencido que continua no facial: remove;
- nome diferente: acerta o nome.

Fica para uma pessoa decidir: foto do facial diferente da do sistema,
sócio sem foto neste PC e usuário do facial sem cadastro no sistema.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app import db, face_audit, updater
from app.config import registration_pools, selected_pools
from app.intelbras_client import DeviceError, IntelbrasClient, face_photo, public_media_url
from app.netcheck import lan_ip_problem

logger = logging.getLogger(__name__)

RECREATE_CODES = {"shared", "no_model", "missing"}
REASONS = {
    "shared": "FaceID repetido",
    "no_model": "sem modelo do rosto",
    "missing": "não estava no facial",
    "expired_on_device": "vencido ainda no facial",
    "name": "nome diferente",
}


@dataclass
class Fix:
    url: str
    label: str
    member_id: int
    name: str
    action: str  # "recriar", "remover" ou "nome"
    reason: str


_lock = threading.Lock()
_state: dict[str, Any] = {"running": False}


def status() -> dict[str, Any]:
    with _lock:
        return {
            **_state,
            "results": list(_state.get("results", [])),
            "pending": list(_state.get("pending", [])),
        }


def plan(report: dict[str, Any]) -> tuple[list[Fix], list[dict[str, str]]]:
    """O que corrigir sozinho e o que fica para decidir, a partir da conferência."""
    fixes: list[Fix] = []
    pending: list[dict[str, str]] = []
    for device in report.get("devices", []):
        label = device["label"]
        if not device.get("ok"):
            pending.append({"name": label, "label": label, "message": device.get("error") or "Facial fora do ar."})
            continue
        for row in device["rows"]:
            codes = set(row.get("codes") or [])
            recreate = codes & RECREATE_CODES
            if recreate and "no_local_photo" in codes:
                pending.append({
                    "name": row["name"],
                    "label": label,
                    "message": "Precisa de foto nova pelo botão Foto: a do sistema não está neste PC.",
                })
            elif recreate:
                reason = ", ".join(REASONS[code] for code in ("shared", "no_model", "missing") if code in recreate)
                fixes.append(Fix(device["url"], label, row["member_id"], row["name"], "recriar", reason))
            elif "expired_on_device" in codes:
                fixes.append(Fix(device["url"], label, row["member_id"], row["name"], "remover", REASONS["expired_on_device"]))
            elif "name" in codes:
                fixes.append(Fix(device["url"], label, row["member_id"], row["name"], "nome", REASONS["name"]))
            if "photo_diff" in codes and not recreate:
                pending.append({
                    "name": row["name"],
                    "label": label,
                    "message": "A foto do facial parece diferente da do sistema: compare e escolha qual enviar.",
                })
        for orphan in device.get("orphans", []):
            message = "Está no facial, mas não tem cadastro desta piscina no sistema. Confira no painel do Intelbras."
            if orphan.get("shared_with"):
                message += f" Divide o FaceID com {', '.join(orphan['shared_with'])}."
            pending.append({"name": orphan.get("name") or orphan.get("registration") or "?", "label": label, "message": message})
    return fixes, pending


def recreate_on_device(client: IntelbrasClient, member: db.Member, photo: Path) -> None:
    """Apaga o sócio do facial e cadastra de novo com a foto: ele ganha um FaceID novo."""
    face_url = public_media_url(photo.name)
    client.delete_user(registration=member.registration)
    client.create_user_with_face(
        member.name,
        photo,
        registration=member.registration,
        face_public_url=face_url,
    )


def pool_urls(member: db.Member) -> dict[str, str]:
    """URL do facial -> nome da piscina. Sem piscina gravada vale as duas."""
    chosen = selected_pools(member.pool) or registration_pools()
    return {item["url"]: item["label"] for item in chosen}


def recreate_member(member: db.Member, photo: Path) -> tuple[list[str], Optional[str]]:
    """Recria o sócio em cada facial da piscina dele, ao mesmo tempo.

    Devolve as piscinas refeitas e o erro, se algum facial falhou.
    """
    urls = pool_urls(member)

    def run(item: tuple[str, str]) -> tuple[str, Optional[str]]:
        url, label = item
        try:
            recreate_on_device(IntelbrasClient(base_url=url), member, photo)
            return label, None
        except DeviceError as exc:
            logger.exception("Falha ao refazer %s em %s", member.registration, url)
            return label, f"{label}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, len(urls))) as executor:
        outcomes = list(executor.map(run, urls.items()))
    done = [label for label, error in outcomes if error is None]
    errors = [error for _label, error in outcomes if error]
    return done, ("; ".join(errors) if errors else None)


def _apply(fix: Fix) -> tuple[bool, str]:
    member = db.get_member(fix.member_id)
    if member is None:
        return False, "Cadastro não existe mais."
    client = IntelbrasClient(base_url=fix.url)
    if fix.action == "remover":
        client.delete_user(registration=member.registration)
        return True, "Removido do facial (exame vencido)."
    if fix.action == "nome":
        client.rename_user(member.registration, member.name)
        return True, "Nome acertado no facial."
    photo = Path(member.photo_path) if member.photo_path else None
    if photo is None or not photo.is_file():
        return False, "A foto do sistema não está neste PC: use o botão Foto."
    recreate_on_device(client, member, face_photo(photo))
    db.log_access_event(
        result="photo_updated",
        member_name=member.name,
        registration=member.registration,
        detail=f"Corrigido no facial ({fix.label}): {fix.reason}",
    )
    return True, f"Refeito com a foto do sistema ({fix.reason})."


def _record(fix: Fix, ok: bool, message: str) -> None:
    with _lock:
        _state["done"] += 1
        _state["results"].append(
            {"name": fix.name, "label": fix.label, "action": fix.action, "ok": ok, "message": message}
        )
        _state["current"] = None


def _run_device(fixes: list[Fix]) -> None:
    for fix in fixes:
        with _lock:
            _state["current"] = f"{fix.name} ({fix.label})"
        try:
            ok, message = _apply(fix)
        except DeviceError as exc:
            ok, message = False, str(exc)
        except Exception as exc:  # noqa: BLE001 — um sócio não pode parar a correção dos outros
            logger.exception("Erro inesperado ao corrigir %s", fix.name)
            ok, message = False, f"Erro inesperado: {exc}"
        _record(fix, ok, message)


def _run() -> None:
    fixed = 0
    try:
        with updater.busy_operation():
            report = face_audit.run_audit(thumbnails=False)
            fixes, pending = plan(report)
            with _lock:
                _state["total"] = len(fixes)
                _state["pending"] = pending
                _state["phase"] = "corrigindo"
            by_device: dict[str, list[Fix]] = {}
            for fix in fixes:
                by_device.setdefault(fix.url, []).append(fix)
            # Cada facial corrige a sua fila; os dois trabalham ao mesmo tempo.
            with ThreadPoolExecutor(max_workers=max(1, len(by_device))) as executor:
                list(executor.map(_run_device, by_device.values()))
            fixed = sum(1 for item in status()["results"] if item["ok"])
    except updater.Busy as exc:
        with _lock:
            _state["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Correção das fotos falhou")
        with _lock:
            _state["error"] = f"A correção parou: {exc}"
    finally:
        with _lock:
            _state["running"] = False
            _state["current"] = None
            _state["phase"] = "fim"
            _state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if fixed:
            from app.sync import request_sync

            request_sync()


def start() -> tuple[bool, str]:
    """Começa a correção em segundo plano. Falso com o motivo se não deu para começar."""
    problem = lan_ip_problem()
    if problem:
        return False, problem
    with _lock:
        if _state.get("running"):
            return False, "Já tem uma correção em andamento."
        _state.clear()
        _state.update(
            {
                "running": True,
                "phase": "conferindo",
                "total": 0,
                "done": 0,
                "current": None,
                "results": [],
                "pending": [],
                "error": None,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
            }
        )
    threading.Thread(target=_run, name="face-fix", daemon=True).start()
    return True, "Correção iniciada."
