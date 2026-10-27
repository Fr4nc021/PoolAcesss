"""Testa a junção de cadastros e a troca local, sem facial e sem nuvem."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SYNC_TOKEN", "token-teste")

import app.config as config
import app.db as db
from app.sync import (
    MemberSnapshot,
    _apply_event,
    _apply_remote_member,
    _labels,
    _set_cloud,
    _snapshot_from_payload,
    _status_lock,
    event_payload,
    member_payload,
    merge_snapshots,
    run_sync_cycle,
)

config.reload_env = lambda: None  # noqa: E731
os.environ["SYNC_PEER_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["SYNC_TOKEN"] = "token-teste"


def _snap(
    *,
    name: str = "Ana",
    registration: str = "AP00000001",
    expires: datetime,
    updated: datetime,
    blocked: bool = False,
    photo_sha256: str | None = None,
    created: datetime | None = None,
) -> MemberSnapshot:
    return MemberSnapshot(
        name=name,
        registration=registration,
        device_user_id=10,
        photo_sha256=photo_sha256,
        photo_filename=f"{photo_sha256}.jpg" if photo_sha256 else None,
        medical_exam_expires_at=expires,
        access_blocked=blocked,
        created_at=created or updated,
        updated_at=updated,
    )


def test_renewal_clears_stale_block() -> None:
    now = datetime.now().replace(microsecond=0)
    local = _snap(expires=now - timedelta(days=1), updated=now, blocked=True)
    remote = _snap(expires=now + timedelta(days=30), updated=now - timedelta(hours=1), blocked=False)
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.medical_exam_expires_at == remote.medical_exam_expires_at
    assert merged.access_blocked is False
    assert needs_unblock is True


def test_manual_block_with_same_expiry_wins() -> None:
    now = datetime.now().replace(microsecond=0)
    expires = now + timedelta(days=10)
    local = _snap(expires=expires, updated=now - timedelta(hours=2), blocked=False)
    remote = _snap(expires=expires, updated=now, blocked=True, name="Ana Bloqueada")
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.access_blocked is True
    assert merged.name == "Ana Bloqueada"
    assert needs_unblock is False


def test_local_renewal_survives_newer_stale_block() -> None:
    now = datetime.now().replace(microsecond=0)
    local = _snap(expires=now + timedelta(days=30), updated=now - timedelta(minutes=5), blocked=False)
    remote = _snap(expires=now - timedelta(days=1), updated=now, blocked=True)
    merged, needs_unblock = merge_snapshots(local, remote)
    assert merged.medical_exam_expires_at == local.medical_exam_expires_at
    assert merged.access_blocked is False
    assert needs_unblock is False


def test_newer_photo_wins() -> None:
    now = datetime.now().replace(microsecond=0)
    expires = now + timedelta(days=10)
    local = _snap(expires=expires, updated=now - timedelta(hours=1), photo_sha256="aaa")
    remote = _snap(expires=expires, updated=now, photo_sha256="bbb")
    merged, _needs = merge_snapshots(local, remote)
    assert merged.photo_sha256 == "bbb"


def test_two_databases_share_member_photo_and_event() -> None:
    now = datetime.now().replace(microsecond=0)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        photos = root / "photos"
        photos.mkdir()
        config.DATA_DIR = root
        config.PHOTOS_DIR = photos
        config.DB_PATH = root / "a.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()

        photo = photos / "ana.jpg"
        photo.write_bytes(b"foto-ana")
        created = db.create_member(
            name="Ana",
            registration="AP00000001",
            device_user_id=4,
            photo_path=str(photo),
            expires_at=now + timedelta(days=15),
        )
        payload = member_payload(created)
        events = [event_payload(event) for event in db.list_events_for_sync()]
        assert events

        config.DB_PATH = root / "b.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        remote = _snapshot_from_payload(payload)
        assert remote is not None
        _apply_remote_member(remote, lambda: photo.read_bytes())
        for event in events:
            _apply_event(event)
            _apply_event(event)

        copied = db.get_member_by_registration("AP00000001")
        assert copied is not None
        assert copied.name == "Ana"
        assert copied.medical_exam_expires_at == created.medical_exam_expires_at
        assert copied.photo_sha256 == db.file_sha256(str(photo))
        assert copied.photo_path is not None
        assert Path(copied.photo_path).read_bytes() == b"foto-ana"
        assert len(db.list_events_for_sync()) == len(events)

        db.DB_PATH = root / "a.db"
        config.DB_PATH = db.DB_PATH
        origin = db.get_member_by_registration("AP00000001")
        assert origin is not None
        assert db.delete_member(origin.id)
        deletions = [
            {"registration": registration, "deleted_at": deleted_at.isoformat(timespec="seconds")}
            for registration, deleted_at in db.list_deletions()
        ]
        assert deletions

        db.DB_PATH = root / "b.db"
        config.DB_PATH = db.DB_PATH
        from app.sync import _apply_deletion_list

        _apply_deletion_list(deletions)
        assert db.get_member_by_registration("AP00000001") is None
        _apply_remote_member(remote, lambda: photo.read_bytes())
        assert db.get_member_by_registration("AP00000001") is None


def test_cloud_failure_sets_adm_alert() -> None:
    import httpx
    from app.sync import _remember_cloud_failure, _set_cloud, status_view

    os.environ["SUPABASE_URL"] = "https://example.supabase.co"
    os.environ["SUPABASE_KEY"] = "service-role-test"
    try:
        _remember_cloud_failure(httpx.ConnectError("offline"), action="enviar para a nuvem")
        status = status_view()
        assert status["cloud"] == "sem internet"
        assert "Sem conexão com a internet" in status["cloud_alert"]
        assert "dados locais" in status["cloud_alert"]
    finally:
        os.environ["SUPABASE_URL"] = ""
        os.environ["SUPABASE_KEY"] = ""
        _set_cloud("não configurada", "is-muted", "")


def _reset_sync_labels() -> None:
    with _status_lock:
        _labels["peer"] = None
        _labels["peer_class"] = None
    _set_cloud("não configurada", "is-muted", "")


def _use_temp_db(root: Path) -> None:
    config.DATA_DIR = root
    config.PHOTOS_DIR = root / "photos"
    config.PHOTOS_DIR.mkdir(exist_ok=True)
    config.DB_PATH = root / "sync.db"
    db.DB_PATH = config.DB_PATH
    db.init_db()


def test_sync_button_reports_missing_config() -> None:
    os.environ["SYNC_PEER_URL"] = ""
    os.environ["SUPABASE_URL"] = ""
    os.environ["SUPABASE_KEY"] = ""
    os.environ["SYNC_TOKEN"] = "token-teste"
    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(Path(tmp))
        try:
            report = run_sync_cycle()
            assert report["peer"]["configured"] is False
            assert "SYNC_PEER_URL" in report["peer"]["detail"]
            assert report["cloud"]["configured"] is False
            assert "SUPABASE_URL" in report["cloud"]["detail"]
        finally:
            _reset_sync_labels()


def test_sync_button_explains_rejected_token() -> None:
    import httpx

    os.environ["SYNC_PEER_URL"] = "http://192.0.2.10:5000"
    os.environ["SYNC_TOKEN"] = "token-teste"
    os.environ["SUPABASE_URL"] = ""
    os.environ["SUPABASE_KEY"] = ""
    request = httpx.Request("GET", "http://192.0.2.10:5000/sync/estado")
    response = httpx.Response(401, request=request)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return response

    import app.sync as sync

    original = sync.httpx.Client
    sync.httpx.Client = FakeClient
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _use_temp_db(Path(tmp))
            report = run_sync_cycle()
        assert report["peer"]["ok"] is False
        assert report["peer"]["label"] == "token recusado"
        assert "SYNC_TOKEN" in report["peer"]["detail"]
        assert report["cloud"]["configured"] is False
    finally:
        sync.httpx.Client = original
        os.environ["SYNC_PEER_URL"] = ""
        _reset_sync_labels()


def test_http_estado_requires_token() -> None:
    from app.expiry import stop_expiry_worker
    from app.main import create_app
    from app.sync import stop_sync_worker

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config.DATA_DIR = root
        config.PHOTOS_DIR = root / "photos"
        config.DB_PATH = root / "http.db"
        db.DB_PATH = config.DB_PATH
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        denied = client.get("/sync/estado")
        assert denied.status_code == 401
        allowed = client.get(
            "/sync/estado",
            headers={"Authorization": "Bearer token-teste"},
        )
        assert allowed.status_code == 200
        body = allowed.get_json()
        assert body["members"] == []
        assert "events" in body
        page = client.get("/cadastrar")
        try:
            assert page.status_code == 200
            html = page.get_data(as_text=True)
            assert "Outro PC:" in html
            assert "Nuvem:" in html
            assert "não configurado" in html
            assert "não configurada" in html
            adm = client.get("/adm")
            assert adm.status_code == 200
            adm_html = adm.get_data(as_text=True)
            assert 'id="cloud-alert" class="flash flash-error" role="alert" hidden' in adm_html
            assert "Testar outro PC e nuvem" in adm_html, "teste fica na aba Configurações"
            assert 'id="btn-test-sync"' in adm_html
            tested = client.post("/adm/testar-sync")
            assert tested.status_code == 200
            tested_body = tested.get_json()
            assert tested_body["ok"] is True
            assert tested_body["peer"]["configured"] is False
            assert "SYNC_PEER_URL" in tested_body["peer"]["detail"]
            assert tested_body["cloud"]["configured"] is False
            painel = client.get("/sync/painel")
            assert painel.status_code == 200
            status = painel.get_json()
            assert status["cloud"] == "não configurada"
            assert status["cloud_alert"] == ""
        finally:
            stop_sync_worker()
            stop_expiry_worker()
            from app import expiry, sync

            if sync._thread is not None:
                sync._thread.join(timeout=3)
            if expiry._thread is not None:
                expiry._thread.join(timeout=3)


class _FakeCloud:
    """Supabase de mentira: tabela members e bucket de fotos em memória."""

    def __init__(self, members: list[dict], objects: dict[str, str]) -> None:
        self.members = members
        self.objects = objects
        self.deleted: list[str] = []

    def handler(self, request):
        import httpx

        path = request.url.path
        params = dict(request.url.params)
        if path == "/rest/v1/members" and request.method == "GET":
            rows = self.members
            if "registration" in params:
                rows = [r for r in rows if f"eq.{r['registration']}" == params["registration"]]
            if "photo_sha256" in params:
                rows = [r for r in rows if f"eq.{r.get('photo_sha256')}" == params["photo_sha256"]]
            if params.get("offset") not in (None, "0"):
                rows = []
            return httpx.Response(200, json=rows)
        if path == "/rest/v1/members" and request.method == "DELETE":
            reg = params["registration"][3:]
            self.members = [r for r in self.members if r["registration"] != reg]
            return httpx.Response(204)
        if path == "/storage/v1/object/list/fotos":
            return httpx.Response(
                200,
                json=[{"name": name, "created_at": created} for name, created in self.objects.items()],
            )
        if path.startswith("/storage/v1/object/fotos/") and request.method == "DELETE":
            name = path.rsplit("/", 1)[1]
            self.objects.pop(name, None)
            self.deleted.append(name)
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def test_cloud_photo_is_deleted_with_member() -> None:
    import httpx

    import app.sync as sync

    shared, own = "a" * 64, "b" * 64
    cloud = _FakeCloud(
        [
            {"registration": "AP1", "photo_sha256": own},
            {"registration": "AP2", "photo_sha256": shared},
            {"registration": "AP3", "photo_sha256": shared},
        ],
        {f"{own}.jpg": "2026-01-01T00:00:00Z", f"{shared}.jpg": "2026-01-01T00:00:00Z"},
    )
    with httpx.Client(transport=httpx.MockTransport(cloud.handler), base_url="https://x") as client:
        sync._delete_cloud_member(client, "https://x", "chave", "AP1")
        assert cloud.deleted == [f"{own}.jpg"], cloud.deleted
        sync._delete_cloud_member(client, "https://x", "chave", "AP2")
        assert f"{shared}.jpg" in cloud.objects, "AP3 ainda usa a mesma foto"
        sync._delete_cloud_member(client, "https://x", "chave", "AP3")
        assert f"{shared}.jpg" not in cloud.objects


def test_orphan_photo_sweep() -> None:
    import httpx

    import app.sync as sync

    used, old_orphan, new_orphan = "c" * 64, "d" * 64, "e" * 64
    now = datetime(2026, 10, 15, 12, 0, 0).timestamp()
    cloud = _FakeCloud(
        [{"registration": "AP1", "photo_sha256": used}],
        {
            f"{used}.jpg": "2026-01-01T00:00:00Z",
            f"{old_orphan}.jpg": "2026-01-01T00:00:00Z",
            f"{new_orphan}.jpg": datetime.fromtimestamp(now - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outro-arquivo.txt": "2026-01-01T00:00:00Z",
        },
    )
    sync._last_orphan_sweep = 0.0
    with httpx.Client(transport=httpx.MockTransport(cloud.handler), base_url="https://x") as client:
        assert sync._sweep_orphan_photos(client, "https://x", "chave", now=now) == 1
        assert cloud.deleted == [f"{old_orphan}.jpg"]
        assert sync._sweep_orphan_photos(client, "https://x", "chave", now=now + 60) == 0, "só uma vez por dia"
    sync._last_orphan_sweep = 0.0


def main() -> None:
    test_renewal_clears_stale_block()
    test_manual_block_with_same_expiry_wins()
    test_local_renewal_survives_newer_stale_block()
    test_newer_photo_wins()
    test_two_databases_share_member_photo_and_event()
    test_cloud_failure_sets_adm_alert()
    test_sync_button_reports_missing_config()
    test_sync_button_explains_rejected_token()
    test_http_estado_requires_token()
    test_cloud_photo_is_deleted_with_member()
    test_orphan_photo_sweep()
    print("sync ok")


if __name__ == "__main__":
    main()
