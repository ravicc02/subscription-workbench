"""Isolated acceptance run: public RSS -> real model -> three local HTTP receivers.
No customer chat is used. The sink is an actual HTTP endpoint, not a product delivery mode.
"""

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.core import Store
from backend.app.network import Network
from backend.app.service import Workbench

project = Path(__file__).resolve().parents[1]


class Sink(BaseHTTPRequestHandler):
    receipts = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        Sink.receipts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')


async def main():
    root = project / "runs/verification" / ("live-case-" + str(int(time.time())))
    w = Workbench(root, network=Network(["127.0.0.1"]))
    main_store = Store(project)
    old_model = main_store.all("model")[0]
    model = w.store.put(
        "model",
        {
            **{k: v for k, v in old_model.items() if k not in ("id", "revision", "secret")},
            "secret": w.store.seal(main_store.open(old_model["secret"])),
        },
    )
    main_store.db.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:" + str(server.server_port) + "/receive"
    try:
        bots = []
        for name, mode in [
            ("Shared A", "shared"),
            ("Shared B", "shared"),
            ("Independent C", "independent"),
        ]:
            bots.append(
                await w.create_bot(
                    {
                        "name": name,
                        "type": "webhook",
                        "config": {"url": url},
                        "workspace_mode": mode,
                    }
                )
            )
        independent = w.store.get("workspace", bots[2]["workspace_id"])
        profile = w.workspace_settings(independent["id"])["ai_profile"]
        profile["processing_prompt"] = (
            "用中文制作紧凑的三点更新：事实、背景、后续关注。保留来源链接和证据范围，不编造实时价格。"
        )
        w.save_workspace_settings(
            independent["id"], {"revision": independent["revision"], "ai_profile": profile}
        )
        profiles = json.loads((project / "docs/imported-sources.json").read_text(encoding="utf-8"))[
            "feeds"
        ]
        source = next(x for x in profiles if x["id"] == "fed")
        source = w.store.put(
            "source",
            {**source, "id": "source-live-fed", "type": "rss", "enabled": True},
        )
        for bot in (bots[0], bots[2]):
            w.save_subscription(
                {
                    "name": "Real RSS acceptance",
                    "workspace_id": bot["workspace_id"],
                    "source_id": source["id"],
                    "mode": "agent",
                    "model_id": model["id"],
                    "instructions": "本次为历史资料验证。保留原始发布时间，不称为刚发布，不使用今日或最新来描述旧资料。",
                    "enabled": True,
                }
            )
        rows = await w.sources.discover(source)
        # This explicitly selected article is an acceptance input, not normal historical replay.
        selected = next(
            (
                r
                for r in rows
                if "gold" in r["title"].lower()
                and not any(
                    x in r["title"].lower()
                    for x in ("drill", "intersect", "resource", "announce", "mining")
                )
            ),
            rows[0],
        )
        w.ingest(source, [selected], external=True, allow_historical=True)
        print(
            json.dumps(
                {
                    "phase": "captured_rss",
                    "title": selected["title"],
                    "url": selected["url"],
                }
            ),
            flush=True,
        )
        for _ in range(2):
            await w.process_one()
            print(
                json.dumps({"phase": "model_job", "states": w.overview()["stats"]["jobs"]}),
                flush=True,
            )
        for _ in range(3):
            await w.deliver_one()
        jobs = [
            dict(r)
            for r in w.store.db.execute("SELECT id,workspace_id,state,error,paths FROM jobs")
        ]
        deliveries = [
            dict(r)
            for r in w.store.db.execute(
                "SELECT id,bot_id,state,attempts,part_index FROM deliveries"
            )
        ]
        result = {
            "instance_root": str(root),
            "article": {"title": selected["title"], "url": selected["url"]},
            "real_model": model["model"],
            "body_captures": w.store.db.execute(
                "SELECT COUNT(*) FROM audit WHERE state='CAPTURED'"
            ).fetchone()[0],
            "jobs": jobs,
            "deliveries": deliveries,
            "http_receiver_requests": len(Sink.receipts),
            "same_workspace_reports_identical": len(Sink.receipts) >= 2
            and Sink.receipts[0]["text"] == Sink.receipts[1]["text"],
            "real_wechat_send_performed": False,
            "source_items_observed": len(rows),
            "test_targets": "isolated local HTTP receivers",
            "historical_validation_replay": True,
            "all_jobs_rendered": all(j["state"] == "RENDERED" for j in jobs),
            "all_deliveries_accepted": all(d["state"] == "ADAPTER_ACCEPTED" for d in deliveries),
            "original_publication": selected.get("published"),
        }
        folder = project / "runs/verification"
        folder.mkdir(exist_ok=True, parents=True)
        (folder / "live-pipeline.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        w.store.db.close()


asyncio.run(main())
