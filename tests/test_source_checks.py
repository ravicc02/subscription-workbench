"""Persistent batch checks must not depend on the originating browser request."""

import asyncio
import time

from fastapi.testclient import TestClient

from backend.app.api import create_app
from backend.app.service import Workbench


def feed(workbench, name, *, kind="rss", enabled=True):
    return workbench.store.put(
        "source",
        {
            "name": name,
            "type": kind,
            "url": "https://" + name + ".example/feed" if kind != "webhook" else "",
            "poll_minutes": 5,
            "enabled": enabled,
        },
    )


def test_batch_api_scope_auth_conflict_and_per_source_results(tmp_path):
    app = create_app(tmp_path, worker=False)
    w = app.state.workbench
    first = feed(w, "first")
    second = feed(w, "second")
    feed(w, "paused", enabled=False)
    feed(w, "events", kind="webhook")
    with TestClient(app) as client:
        assert client.get("/api/source-checks").status_code == 401
        assert (
            client.post(
                "/api/auth/setup",
                json={"password": "testing-password-123"},
                headers={"X-Requested-With": "workbench"},
            ).status_code
            == 200
        )
        assert client.post("/api/source-checks", json={"scope": "all"}).status_code == 403
        client.headers["X-Requested-With"] = "workbench"
        assert (
            client.post(
                "/api/source-checks", json={"scope": "selected", "source_ids": ["absent"]}
            ).status_code
            == 400
        )
        created = client.post("/api/source-checks", json={"scope": "all"})
        assert created.status_code == 200
        batch = created.json()
        assert batch["total"] == 2 and batch["skipped"] == 2
        assert [row["id"] for row in batch["rows"]] == [first["id"], second["id"]]
        assert client.post("/api/source-checks", json={"scope": "all"}).status_code == 409

        calls = []

        async def poll(key):
            calls.append(key)
            if key == first["id"]:
                return {"items": 3, "new_events": 0, "baseline_created": True}
            return {"deferred": True, "retry_at": time.time() + 600}

        w.poll = poll
        assert asyncio.run(w.source_check_one())
        assert not asyncio.run(w.source_check_one())
        result = client.get("/api/source-checks/" + batch["id"]).json()
        assert result["state"] == "COMPLETED"
        assert result["done"] == 2 and result["warning"]
        assert result["counts"] == {"BASELINE": 1, "DEFERRED": 1}
        assert calls == [first["id"], second["id"]]
        assert client.get("/api/source-checks?limit=1").json()["batches"][0]["id"] == batch["id"]
        assert "rows" not in client.get("/api/source-checks?limit=1").json()["batches"][0]


def test_queued_batch_survives_browser_session_and_process_restart(tmp_path):
    first = Workbench(tmp_path)
    source = feed(first, "durable")
    batch = first.create_source_check({"scope": "selected", "source_ids": [source["id"]]})
    first.store.db.close()

    restored = Workbench(tmp_path)
    restored.store.recover()
    calls = []

    async def poll(key):
        calls.append(key)
        return {"items": 1, "new_events": 0, "baseline_created": True}

    restored.poll = poll
    assert asyncio.run(restored.source_check_one())
    result = restored.source_check(batch["id"])
    assert result["state"] == "COMPLETED" and result["counts"] == {"BASELINE": 1}
    assert calls == [source["id"]]
    restored.store.db.close()


def test_stop_cancels_only_unstarted_items(tmp_path):
    w = Workbench(tmp_path)
    feeds = [feed(w, "feed" + str(i)) for i in range(5)]
    batch = w.create_source_check({"scope": "all"})
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def poll(key):
        calls.append(key)
        if len(calls) == 3:
            started.set()
        await release.wait()
        return {"items": 0, "new_events": 0, "baseline_created": False}

    w.poll = poll

    async def scenario():
        task = asyncio.create_task(w.source_check_one())
        await asyncio.wait_for(started.wait(), 1)
        stopped = w.stop_source_check(batch["id"])
        assert stopped["stop_requested"] and stopped["counts"]["CANCELLED"] == 2
        release.set()
        await task

    asyncio.run(scenario())
    result = w.source_check(batch["id"])
    assert result["state"] == "CANCELLED" and result["done"] == 5
    assert result["counts"] == {"OK": 3, "CANCELLED": 2}
    assert set(calls) == {source["id"] for source in feeds[:3]}
    w.store.db.close()


def test_inflight_at_restart_is_not_automatically_retried(tmp_path):
    w = Workbench(tmp_path)
    feeds = [feed(w, "restart" + str(i)) for i in range(3)]
    batch = w.create_source_check({"scope": "all"})
    w.store.db.execute("UPDATE source_check_batches SET state='RUNNING' WHERE id=?", (batch["id"],))
    w.store.db.execute(
        "UPDATE source_check_items SET state='RUNNING' WHERE batch_id=? AND ordinal=0",
        (batch["id"],),
    )
    w.store.db.close()
    restored = Workbench(tmp_path)
    restored.store.recover()
    calls = []

    async def poll(key):
        calls.append(key)
        return {"items": 0, "new_events": 0, "baseline_created": False}

    restored.poll = poll
    assert asyncio.run(restored.source_check_one())
    result = restored.source_check(batch["id"])
    assert result["state"] == "COMPLETED" and result["warning"]
    assert result["counts"] == {"INTERRUPTED": 1, "OK": 2}
    assert set(calls) == {source["id"] for source in feeds[1:]}
    restored.store.db.close()
