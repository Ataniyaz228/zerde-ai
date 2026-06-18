import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _get_app():
    from app import app
    return app


def test_upload_happy_path(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"some content", "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "analysis_id" in data
        assert data["status"] == "queued"

        stored = list(app_env.upload_dir.iterdir())
        assert len(stored) == 1
        assert stored[0].name == f"{data['analysis_id']}.txt"


def test_path_traversal_filename(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("../../etc/passwd.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        stored = list(app_env.upload_dir.iterdir())
        assert len(stored) == 1
        assert stored[0].name == f"{data['analysis_id']}.txt"
        assert stored[0].parent == app_env.upload_dir


def test_disallowed_suffix(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("malware.exe", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 400


def test_oversize_upload(app_env, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 10)  # 10 bytes

    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"x" * 1000, "text/plain")},
        )
        assert resp.status_code == 413
        # partial file removed
        assert list(app_env.upload_dir.iterdir()) == []


def test_unknown_status_id(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.get("/api/analyze/does-not-exist")
        assert resp.status_code == 404


def test_status_lifecycle_completed(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"some content", "text/plain")},
        )
        analysis_id = resp.json()["analysis_id"]

        # Drain WS to let background task run to completion (TestClient
        # portal runs the loop while we wait on messages).
        with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws:
            done = False
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "done":
                    done = True
                    break
            assert done

        status_resp = client.get(f"/api/analyze/{analysis_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "completed"
        assert data["report_id"]
        assert data["score"] == 42


def test_two_concurrent_analyses(app_env):
    app = _get_app()
    with TestClient(app) as client:
        ids = []
        for _ in range(2):
            resp = client.post(
                "/api/analyze",
                files={"file": ("bill.txt", b"some content", "text/plain")},
            )
            ids.append(resp.json()["analysis_id"])

        for analysis_id in ids:
            with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws:
                done = False
                for _ in range(20):
                    msg = ws.receive_json()
                    if msg["type"] == "done":
                        done = True
                        break
                assert done

        for analysis_id in ids:
            status_resp = client.get(f"/api/analyze/{analysis_id}")
            assert status_resp.json()["status"] == "completed"


def test_ws_event_shapes(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"some content", "text/plain")},
        )
        analysis_id = resp.json()["analysis_id"]

        seen_types = []
        with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws:
            for _ in range(20):
                msg = ws.receive_json()
                seen_types.append(msg["type"])
                if msg["type"] == "done":
                    assert "report" in msg
                    assert "score" in msg
                    assert "report_id" in msg
                    break
                if msg["type"] == "stage_start":
                    assert "stage" in msg
                    assert msg["stage"] in {"extract", "search", "verify", "report"}
                    assert "message" in msg
                if msg["type"] == "stage_done":
                    assert msg["stage"] in {"extract", "search", "verify", "report"}

        assert "stage_start" in seen_types
        assert "stage_done" in seen_types
        assert "done" in seen_types


def test_ws_two_subscribers(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"some content", "text/plain")},
        )
        analysis_id = resp.json()["analysis_id"]

        with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws1, \
                client.websocket_connect(f"/ws/progress/{analysis_id}") as ws2:
            done1 = done2 = False
            for _ in range(20):
                if not done1:
                    msg = ws1.receive_json()
                    if msg["type"] == "done":
                        done1 = True
                if not done2:
                    msg = ws2.receive_json()
                    if msg["type"] == "done":
                        done2 = True
                if done1 and done2:
                    break
            assert done1
            assert done2


def test_ws_late_connect_replays_events(app_env):
    app = _get_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/analyze",
            files={"file": ("bill.txt", b"some content", "text/plain")},
        )
        analysis_id = resp.json()["analysis_id"]

        # First connection: drain to done so events are persisted.
        with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws:
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "done":
                    break

        # Late connection should immediately replay persisted events.
        with client.websocket_connect(f"/ws/progress/{analysis_id}") as ws2:
            msg = ws2.receive_json()
            assert msg["type"] == "stage_start"


def test_jobs_persist_across_init_db(app_env):
    from services import jobs

    async def _run():
        await jobs.init_db()
        await jobs.create_job("abc-123", "test.txt", "/tmp/abc-123.txt")
        await jobs.init_db()  # re-init, simulating reload
        job = await jobs.get_job("abc-123")
        assert job is not None
        assert job["status"] == "queued"
        assert job["filename"] == "test.txt"

    asyncio.run(_run())


def test_reconcile_interrupted_fails_running_jobs(app_env):
    """A restart leaves jobs 'running'; reconcile flips them to error with a
    terminal error event so resuming clients stop spinning."""
    from services import jobs

    async def _run():
        await jobs.init_db()
        await jobs.create_job("zombie-1", "bill.docx", "/tmp/zombie-1.docx")
        await jobs.set_status("zombie-1", "running")
        await jobs.append_event("zombie-1", {"type": "stage_start", "stage": "verify"})
        await jobs.create_job("done-1", "ok.docx", "/tmp/ok-1.docx")
        await jobs.set_status("done-1", "completed", report_id="r.md", score=42)

        n = await jobs.reconcile_interrupted()
        assert n == 1  # only the running job

        zombie = await jobs.get_job("zombie-1")
        assert zombie["status"] == "error"
        events = await jobs.get_events("zombie-1")
        assert events[-1]["type"] == "error"

        # Completed jobs are untouched.
        done = await jobs.get_job("done-1")
        assert done["status"] == "completed"

    asyncio.run(_run())


def test_api_key_required_when_set(app_env, monkeypatch):
    """С заданным ZERDE_API_KEY запрос без верного X-API-Key получает 401."""
    from config import settings
    monkeypatch.setattr(settings, "api_key", "secret-key")

    app = _get_app()
    with TestClient(app) as client:
        files = {"file": ("bill.txt", b"content", "text/plain")}
        assert client.post("/api/analyze", files=files).status_code == 401
        bad = client.post("/api/analyze", files=files, headers={"X-API-Key": "wrong"})
        assert bad.status_code == 401
        ok = client.post("/api/analyze", files=files, headers={"X-API-Key": "secret-key"})
        assert ok.status_code == 200


def test_rate_limit_blocks_excess(app_env, monkeypatch):
    """Сверх лимита запусков с одного клиента — 429."""
    from config import settings
    monkeypatch.setattr(settings, "analyze_rate_limit", 2)
    monkeypatch.setattr(settings, "analyze_rate_window_s", 3600)

    app = _get_app()
    with TestClient(app) as client:
        files = {"file": ("bill.txt", b"content", "text/plain")}
        assert client.post("/api/analyze", files=files).status_code == 200
        assert client.post("/api/analyze", files=files).status_code == 200
        blocked = client.post("/api/analyze", files=files)
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


def test_resume_queued_lists_pending_and_skips_running(app_env):
    """list_queued видит непустартовавшие задачи; reconcile их не трогает."""
    from services import jobs

    async def _run():
        await jobs.init_db()
        await jobs.create_job("pending-1", "p.txt", "/tmp/p.txt")
        await jobs.create_job("zombie-1", "z.txt", "/tmp/z.txt")
        await jobs.set_status("zombie-1", "running")

        n = await jobs.reconcile_interrupted()
        assert n == 1  # только running
        assert (await jobs.get_job("pending-1"))["status"] == "queued"

        queued = await jobs.list_queued()
        assert [j["analysis_id"] for j in queued] == ["pending-1"]

    asyncio.run(_run())


def test_translate_progress_sequence():
    """on_progress's translation: pipeline ProgressEvents -> frozen WS shapes.

    extract -> search -> verify -> report, each transition closes the
    previous stage (stage_done) and opens the next (stage_start). Pure
    function, no event loop / pipeline / torch needed.
    """
    from services.analysis import translate_progress

    prev_stage = "extract"
    all_events: list[dict] = []

    # First event from run_pipeline is always ("extract", ...) — same as the
    # stage_start(extract) already emitted by _run_analysis before the
    # pipeline starts, so this must be a no-op.
    events, prev_stage = translate_progress(
        SimpleNamespace(stage="extract", message="Загрузка и извлечение тезисов"), prev_stage
    )
    assert events == []
    assert prev_stage == "extract"

    for stage, message in [
        ("search", "Поиск в базе НПА Казахстана"),
        ("verify", "Верификация и анализ коллизий"),
        ("report", "Формирование отчёта"),
    ]:
        events, prev_stage = translate_progress(SimpleNamespace(stage=stage, message=message), prev_stage)
        all_events.extend(events)
        assert prev_stage == stage

    seen_types = [(e["type"], e["stage"]) for e in all_events]
    assert seen_types == [
        ("stage_done", "extract"),
        ("stage_start", "search"),
        ("stage_done", "search"),
        ("stage_start", "verify"),
        ("stage_done", "verify"),
        ("stage_start", "report"),
    ]
    for e in all_events:
        if e["type"] == "stage_start":
            assert e["stage"] in {"extract", "search", "verify", "report"}
            assert "message" in e
        if e["type"] == "stage_done":
            assert e["stage"] in {"extract", "search", "verify", "report"}


def test_translate_progress_matches_real_progress_event():
    """Sanity check translate_progress against the real ProgressEvent dataclass."""
    from services.analysis import translate_progress

    from zerde.pipeline import ProgressEvent

    events, new_prev = translate_progress(ProgressEvent("search", "Поиск в базе НПА Казахстана"), "extract")
    assert events == [
        {"type": "stage_done", "stage": "extract"},
        {"type": "stage_start", "stage": "search", "message": "Поиск в базе НПА Казахстана"},
    ]
    assert new_prev == "search"


# ── Localized report endpoint (RU canonical / KZ safe machine translation) ──

def test_report_lang_ru_is_canonical_no_translation(app_env, monkeypatch):
    (app_env.output_dir / "r.md").write_text("# RU отчёт\n\nст. 14 · 92%\n", encoding="utf-8")

    # Перевод НЕ должен вызываться на RU-пути.
    import zerde.stages.s7_translate as s7t
    async def _boom(*a, **k):  # pragma: no cover - не должно вызваться
        raise AssertionError("translation must not run for lang=ru")
    monkeypatch.setattr(s7t, "translate_report_md", _boom)

    app = _get_app()
    with TestClient(app) as client:
        resp = client.get("/api/reports/r.md")  # default lang=ru
        assert resp.status_code == 200
        assert "# RU отчёт" in resp.json()["content"]


def test_report_lang_kz_translates_and_caches(app_env, monkeypatch):
    (app_env.output_dir / "r.md").write_text("# RU отчёт\n\nст. 14 · 92%\n", encoding="utf-8")

    import zerde.stages.s7_translate as s7t
    calls = {"n": 0}
    async def fake_tr(md_ru, target_lang="kz", settings=None):
        calls["n"] += 1
        return "# KZ есеп\n\nст. 14 · 92%\n"
    monkeypatch.setattr(s7t, "translate_report_md", fake_tr)

    app = _get_app()
    with TestClient(app) as client:
        resp = client.get("/api/reports/r.md?lang=kz")
        assert resp.status_code == 200
        assert "KZ есеп" in resp.json()["content"]
        # sidecar записан → второй запрос из кэша (перевод не вызывается снова)
        assert (app_env.output_dir / "r.md.kz.md").exists()
        resp2 = client.get("/api/reports/r.md?lang=kz")
        assert "KZ есеп" in resp2.json()["content"]
        assert calls["n"] == 1


def test_report_lang_invalid_rejected(app_env):
    (app_env.output_dir / "r.md").write_text("# RU\n", encoding="utf-8")
    app = _get_app()
    with TestClient(app) as client:
        assert client.get("/api/reports/r.md?lang=en").status_code == 422
