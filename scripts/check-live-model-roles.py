"""Exercise configured publication and conversation models without touching live bots."""

import argparse
import asyncio
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core import dumps
from backend.app.network import Network
from backend.app.service import Workbench


class Sink(BaseHTTPRequestHandler):
    receipts = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.__class__.receipts.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')


async def run(source_root, output):
    source_root = Path(source_root).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(
        "file:" + str(source_root / "state/workbench.sqlite3") + "?mode=ro", uri=True
    )
    key = Fernet((source_root / "state/vault.key").read_bytes().strip())
    models = [
        dict(json.loads(data), id=ident)
        for ident, data in db.execute("SELECT id,data FROM objects WHERE kind='model'")
    ]
    db.close()
    roles = {}
    for role, marker in (("publication", "处理"), ("conversation", "对话")):
        matching = [model for model in models if marker in model["name"]]
        if len(matching) != 1:
            raise ValueError("Expected exactly one model marked for " + role)
        roles[role] = matching[0]

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    workbench = Workbench(output, Network(["127.0.0.1"]))
    result = {"isolated_root": str(output), "roles": {}, "local_receipts": 0}
    try:
        for role, configured in roles.items():
            secret = json.loads(key.decrypt(configured["secret"].encode()))
            copy = {**configured, "secret": workbench.store.seal(secret)}
            del secret
            result["roles"][role] = {"name": copy["name"], "model": copy["model"]}
            workbench.store.put("model", copy)
        publish = roles["publication"]
        chat = roles["conversation"]
        workbench.store.set_setting("conversation_model_id", chat["id"])
        workspace = workbench.workspaces.bind("verification", "independent")
        config = {
            "url": f"http://127.0.0.1:{sink.server_port}/receive",
            "targets": [{"id": "endpoint", "name": "本机验收", "selected": True}],
        }
        bot = workbench.store.put(
            "bot",
            {
                "name": "Local validation",
                "type": "webhook",
                "enabled": True,
                "status": "READY",
                "secret": workbench.store.seal(config),
                "workspace_id": workspace["id"],
                "binding_revision": 1,
            },
        )
        source = workbench.store.put(
            "source", {"name": "Local synthetic input", "type": "webhook", "enabled": True}
        )
        workbench.store.put(
            "subscription",
            {
                "name": "Agent role validation",
                "source_id": source["id"],
                "source_ids": [source["id"]],
                "workspace_id": workspace["id"],
                "mode": "agent",
                "model_id": publish["id"],
                "enabled": True,
            },
        )
        workbench.ingest(
            source,
            [
                {
                    "item_key": "model-validation",
                    "title": "公开项目发布版本 1.0",
                    "text": "公开项目在 2026-10-24 发布版本 1.0，并修复了两个已记录的兼容性问题。本条为合成测试输入。",
                    "content_type": "event",
                }
            ],
            external=True,
        )
        await workbench.process_one()
        job = workbench.store.db.execute("SELECT state,error,paths FROM jobs").fetchone()
        result["roles"]["publication"].update(state=job["state"], error=job["error"])
        if job["state"] == "RENDERED":
            paths = json.loads(job["paths"])
            result["roles"]["publication"]["report"] = workbench.workspaces.file(
                workspace["path"], paths["report"]
            ).read_text(encoding="utf-8")[:3000]
            await workbench.deliver_one()
        workbench.accept_inbound(
            bot,
            config,
            {
                "id": "local-question",
                "target": "endpoint",
                "sender": "local",
                "text": "你刚才推送了什么？请只依据已有记录回答。",
            },
        )
        await workbench.chat_one()
        inbound = workbench.store.db.execute(
            "SELECT state,error,response,model_id FROM inbound_messages"
        ).fetchone()
        result["roles"]["conversation"].update(
            state=inbound["state"], error=inbound["error"], response=inbound["response"]
        )
        if inbound["state"] == "READY":
            await workbench.reply_one()
            result["roles"]["conversation"]["send_state"] = workbench.store.db.execute(
                "SELECT state FROM inbound_messages"
            ).fetchone()[0]
        result["local_receipts"] = len(Sink.receipts)
        (output / "result.json").write_text(dumps(result), encoding="utf-8")
        return result
    finally:
        await workbench.stop()
        sink.shutdown()
        sink.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.source, args.out)), ensure_ascii=False))
