import asyncio

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
