"""Exercise subscription routing with the configured model and optional real bot delivery."""

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.api import create_app
from backend.app.core import Store
from backend.app.network import Network

CASES = (
    "independent_same_agent",
    "independent_distinct_agent",
    "shared_single_agent",
    "shared_multi_agent",
    "independent_mixed_modes",
    "shared_filter_and_duplicate",
    "shared_pause_resume",
)
PROFILE = {
    "focus_prompt": "只处理明确标记为系统验收测试的合成事件；不是市场新闻。",
    "excluded_topics": "不要补充任何实时行情、新闻、价格或投资建议。",
    "processing_prompt": "逐字保留场景码和输入中的测试编号；只归纳给出的路由验收事实。不得跳过有效测试事件。",
    "output_template": "【系统验收测试，非真实资讯】\n场景码：\n工作区标记：\n测试事实：\n来源：",
    "language": "中文",
    "additional_instructions": "正文仅用于核对路由、模型调用与推送，不得描述为新发布的新闻。",
}


class Sink(BaseHTTPRequestHandler):
    receipts = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.__class__.receipts.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_):
        pass


def post(client, path, data, headers=None):
    response = client.post("/api/" + path, json=data, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def poll_until(predicate, timeout=450):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.5)
    raise TimeoutError("Scenario did not reach its expected terminal state")


def seed_model(source, clone):
    key = source.setting("default_model")
    assert key, "Configured model is required"
    row = source.get("model", key)
    original_secret = source.open(row["secret"])
    assert original_secret.get("api_key"), "Configured model has no API key"
    clone.store.put("model", {**row, "secret": clone.store.seal(original_secret)})
    clone.store.set_setting("default_model", key)
    stored_proxy = source.setting("collection_proxy")
    if stored_proxy:
        clone.store.set_setting(
            "collection_proxy",
            {**stored_proxy, "secret": clone.store.seal(source.open(stored_proxy["secret"]))},
        )


def create_bots(client, clone, source_store, originals, shared, transport, sink_url):
    bots = []
    for index, original in enumerate(originals):
        if transport == "local":
            bot = post(
                client,
                "bots",
                {
                    "name": f"Validation {index + 1}",
                    "type": "webhook",
                    "config": {"url": sink_url},
                    "workspace_mode": "shared" if shared else "independent",
                },
            )
        else:
            config = source_store.open(original["secret"])
            assert original["status"] == "CONNECTED" and original["enabled"]
            assert any(target.get("selected") for target in config.get("targets", []))
            workspace = clone.workspaces.bind(original["id"], "shared" if shared else "independent")
            saved = clone.store.put(
                "bot",
                {
                    "id": original["id"],
                    "name": original["name"],
                    "type": original["type"],
                    "enabled": True,
                    "status": "CONNECTED",
                    "workspace_id": workspace["id"],
                    "binding_revision": 1,
                    "secret": clone.store.seal(config),
                    "created": time.time(),
                },
            )
            bot = clone.public_bot(saved)
        bots.append(bot)
    for index, workspace_id in enumerate(dict.fromkeys(b["workspace_id"] for b in bots)):
        workspace = clone.store.get("workspace", workspace_id)
        workspace["model_id"] = clone.store.setting("default_model")
        workspace["ai_profile"] = {
            **PROFILE,
            "additional_instructions": PROFILE["additional_instructions"]
            + f" 工作区标记：VALIDATION-SPACE-{index + 1}。",
        }
        clone.store.put("workspace", workspace, workspace["revision"])
    return bots


def sources_and_subscriptions(client, bots, scenario):
    shared = scenario.startswith("shared_")
    modes = (
        ("agent", "forward", "rules") if scenario == "independent_mixed_modes" else ("agent",) * 3
    )
    count = 3 if scenario == "independent_distinct_agent" else 2
    sources = [
        post(client, "sources", {"name": f"Validation feed {i + 1}", "type": "webhook"})
        for i in range(count)
    ]
    if shared:
        selected = (
            [s["id"] for s in sources] if scenario == "shared_multi_agent" else [sources[0]["id"]]
        )
        post(
            client,
            "subscriptions",
            {
                "name": scenario,
                "workspace_id": bots[0]["workspace_id"],
                "source_ids": selected,
                "mode": "agent",
                "keywords": ["VALIDATION-MATCH"]
                if scenario == "shared_filter_and_duplicate"
                else [],
                "enabled": True,
            },
        )
    else:
        for i, bot in enumerate(bots):
            post(
                client,
                "subscriptions",
                {
                    "name": f"{scenario}-{i + 1}",
                    "workspace_id": bot["workspace_id"],
                    "source_ids": [
                        sources[i]["id"]
                        if scenario == "independent_distinct_agent"
                        else sources[0]["id"]
                    ],
                    "mode": modes[i],
                    "keywords": ["VALIDATION-MATCH"] if modes[i] == "rules" else [],
                    "enabled": True,
                },
            )
    return sources


def emit(client, source, case, suffix, match=True):
    text = (
        f"【系统验收测试，不是真实新闻】场景码 {case}；编号 {suffix}；"
        "这条合成事件只用于验证订阅、模型处理和消息路由，不含市场事实。"
        + (" VALIDATION-MATCH" if match else " VALIDATION-NOMATCH")
    )
    return post(
        client,
        "hooks/" + source["id"],
        {"id": case + "-" + suffix, "title": "【系统验收测试】" + case, "text": text},
        {"Authorization": "Bearer " + source["hook_token"]},
    )


def expected_counts(case):
    return {
        "independent_same_agent": (3, 3, 3),
        "independent_distinct_agent": (3, 3, 3),
        "shared_single_agent": (1, 3, 1),
        "shared_multi_agent": (2, 6, 2),
        "independent_mixed_modes": (3, 3, 1),
        "shared_filter_and_duplicate": (1, 3, 1),
        "shared_pause_resume": (1, 3, 1),
    }[case]


def execute_case(output, case, transport, source_store, originals, sink_url):
    root = output / case
    root.mkdir(parents=True, exist_ok=False)
    app = create_app(root, worker=True, network=Network(["127.0.0.1"]))
    w = app.state.workbench
    seed_model(source_store, w)

    # The production instance owns incoming updates. This clone only processes and sends test events.
    async def parked_receiver(_):
        await asyncio.sleep(3600)

    w.receiver = parked_receiver
    started = time.time()
    initial_receipts = len(Sink.receipts)
    shared = case.startswith("shared_")
    with TestClient(app) as client:
        password = secrets.token_urlsafe(25)
        post(client, "auth/setup", {"password": password}, {"X-Requested-With": "workbench"})
        client.headers["X-Requested-With"] = "workbench"
        bots = create_bots(client, w, source_store, originals, shared, transport, sink_url)
        sources = sources_and_subscriptions(client, bots, case)

        if case == "shared_filter_and_duplicate":
            emit(client, sources[0], case, "filtered", match=False)
            assert w.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
            emit(client, sources[0], case, "accepted")
            emit(client, sources[0], case, "accepted")
            assert w.store.db.execute("SELECT count(*) FROM inputs").fetchone()[0] == 2
        else:
            event_sources = (
                sources[:2]
                if case == "shared_multi_agent"
                else sources[:3]
                if case == "independent_distinct_agent"
                else sources[:1]
            )
            for index, source in enumerate(event_sources):
                emit(client, source, case, str(index + 1))

        if case == "shared_pause_resume":
            paused = w.store.get("bot", bots[-1]["id"])
            post(
                client,
                "bots/" + paused["id"] + "/update",
                {"revision": paused["revision"], "enabled": False},
            )

        expected_jobs, expected_deliveries, expected_agents = expected_counts(case)

        def complete():
            jobs = list(w.store.db.execute("SELECT state FROM jobs"))
            deliveries = list(w.store.db.execute("SELECT state FROM deliveries"))
            if len(jobs) != expected_jobs or len(deliveries) != expected_deliveries:
                return False
            return all(j[0] not in ("QUEUED", "RUNNING") for j in jobs) and all(
                d[0] not in ("WAITING_CONTENT", "READY", "SENDING", "WAITING_CONTEXT")
                for d in deliveries
            )

        poll_until(complete)
        if case == "shared_pause_resume":
            before = [row[0] for row in w.store.db.execute("SELECT state FROM deliveries")]
            assert before.count("PAUSED") == 1, before
            bot = w.store.get("bot", bots[-1]["id"])
            post(
                client,
                "bots/" + bot["id"] + "/update",
                {"revision": bot["revision"], "enabled": True},
            )
            poll_until(
                lambda: (
                    not w.store.db.execute(
                        "SELECT 1 FROM deliveries WHERE state='PAUSED'"
                    ).fetchone()
                ),
                timeout=60,
            )

        jobs = [dict(row) for row in w.store.db.execute("SELECT * FROM jobs ORDER BY created,id")]
        deliveries = [
            dict(row) for row in w.store.db.execute("SELECT * FROM deliveries ORDER BY created,id")
        ]
        agent_jobs = []
        rendered = []
        for job in jobs:
            snap = json.loads(job["snapshot"])
            paths = json.loads(job["paths"]) if job["paths"] else {}
            folder = Path(snap["workspace"]["path"])
            report_path = folder / paths["report"] if paths else None
            trace_path = folder / paths["trace"] if paths else None
            report = (
                report_path.read_text(encoding="utf-8")
                if report_path and report_path.exists()
                else ""
            )
            trace = (
                json.loads(trace_path.read_text(encoding="utf-8"))
                if trace_path and trace_path.exists()
                else {}
            )
            if snap["mode"] == "agent":
                agent_jobs.append(job)
                assert trace.get("usage") and trace.get("tools"), (
                    "Agent never called the model/tools"
                )
                assert case in report and "测试" in report, (
                    "Agent report lost the synthetic-test marker"
                )
            else:
                assert trace.get("model") is None and not trace.get("tools"), (
                    "Non-Agent mode called tools"
                )
            rendered.append(
                {
                    "id": job["id"],
                    "mode": snap["mode"],
                    "workspace_id": job["workspace_id"],
                    "state": job["state"],
                    "attempts": job["attempts"],
                    "report_sha256": hashlib.sha256(report.encode()).hexdigest()
                    if report
                    else None,
                    "model_calls": len(trace.get("usage", [])),
                    "tools": len(trace.get("tools", [])),
                    "error": job["error"],
                }
            )
        assert len(agent_jobs) == expected_agents
        if shared:
            assert len({b["workspace_id"] for b in bots}) == 1
        else:
            assert len({b["workspace_id"] for b in bots}) == 3
        states = [d["state"] for d in deliveries]
        assert len(deliveries) == expected_deliveries and all(
            s == "ADAPTER_ACCEPTED" for s in states
        ), states
        assert all(j["state"] == "RENDERED" for j in jobs), rendered
        if transport == "local":
            assert len(Sink.receipts) - initial_receipts == expected_deliveries
        assert all(len(json.loads(d["receipts"])) >= 1 and d["attempts"] == 1 for d in deliveries)

        result = {
            "case": case,
            "transport": transport,
            "expected": {
                "jobs": expected_jobs,
                "deliveries": expected_deliveries,
                "agent_jobs": expected_agents,
            },
            "actual": {
                "jobs": len(jobs),
                "deliveries": len(deliveries),
                "agent_jobs": len(agent_jobs),
            },
            "rendered": rendered,
            "delivery_states": {state: states.count(state) for state in set(states)},
            "bot_types": [b["type"] for b in bots],
            "elapsed_seconds": round(time.time() - started, 2),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-delivery",
        action="store_true",
        help="Send marked test reports to configured bot targets",
    )
    parser.add_argument("--cases", default=",".join(CASES), help="Comma-separated scenario names")
    args = parser.parse_args()
    selected = args.cases.split(",")
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(case not in CASES for case in selected)
    ):
        parser.error("Specify unique known cases: " + ", ".join(CASES))
    transport = "configured" if args.real_delivery else "local"
    out = ROOT / "runs/verification" / ("combinations-" + time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = Store(ROOT)
    originals = source.all("bot")
    assert len(originals) == 3, "This acceptance run requires the current three configured bots"
    assert all(b["status"] == "CONNECTED" and b.get("workspace_id") for b in originals)
    summary = {"transport": transport, "cases": [], "limitations": []}
    try:
        for case in selected:
            print("START", transport, case, flush=True)
            try:
                result = execute_case(
                    out,
                    case,
                    transport,
                    source,
                    originals,
                    f"http://127.0.0.1:{server.server_port}/receive",
                )
                summary["cases"].append(result)
                print(
                    "PASS", case, result["actual"], "seconds", result["elapsed_seconds"], flush=True
                )
            except Exception as exc:
                summary["cases"].append(
                    {"case": case, "error": type(exc).__name__ + ": " + str(exc)[:300]}
                )
                print("FAIL", case, type(exc).__name__, str(exc)[:300], flush=True)
                raise
            finally:
                (out / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    finally:
        source.db.close()
        server.shutdown()
        server.server_close()
    print("REPORT", out / "summary.json", flush=True)


if __name__ == "__main__":
    main()
