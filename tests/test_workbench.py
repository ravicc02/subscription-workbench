import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import create_app
from backend.app.core import Store, Workspaces, dumps
from backend.app.network import Network
from backend.app.service import Workbench
from backend.app.sources import feed_items
from backend.app.subscriptions import resolved_source_ids


class Handler(BaseHTTPRequestHandler):
    receipts = []
    requests = []
    version = 1
    model_calls = 0

    def log_message(self, *_):
        pass

    def reply(self, value, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self):
        self.__class__.requests.append(self.path)
        if self.path == "/data":
            self.reply(
                {
                    "id": "metric",
                    "title": "Service status",
                    "value": self.__class__.version,
                }
            )
        elif self.path == "/rate":
            self.send_response(429)
            self.send_header("Retry-After", "120")
            self.end_headers()
        elif self.path == "/get_login_info":
            self.reply({"status": "ok", "retcode": 0, "data": {"user_id": 98765}})
        else:
            self.reply({"ok": True})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw or b"{}")
        if self.path == "/chat/completions":
            self.__class__.model_calls += 1
            msgs = body["messages"]
            tools = [m for m in msgs if m["role"] == "tool"]

            def call(i, name, args):
                return {
                    "id": str(i),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }

            if not tools:
                import re

                path = re.search(r"Event: ([^\n]+)", msgs[1]["content"]).group(1)
                calls = [
                    call(1, "read_file", {"path": "docs/rules.md"}),
                    call(2, "read_file", {"path": path}),
                    call(3, "read_file", {"path": "../../state/vault.key"}),
                ]
            else:
                event = next(
                    (
                        json.loads(m["content"])
                        for m in tools
                        if m["content"].startswith("{") and "raw_path" in m["content"]
                    ),
                    None,
                )
                if not any(m["content"] == "原始业务事件内容" for m in tools):
                    calls = [call(4, "read_file", {"path": event["raw_path"]})]
                elif not any(m["content"].startswith("Saved ") for m in tools):
                    calls = [
                        call(
                            5,
                            "write_file",
                            {
                                "path": event["outputs"]["processed"],
                                "content": "输入证据：" + event["raw_path"] + "\n原始业务事件内容",
                            },
                        ),
                        call(
                            6,
                            "write_file",
                            {
                                "path": event["outputs"]["report"],
                                "content": "# 业务更新\n\n摘要：已收到真实业务事件输入，按工作区规则整理并生成推送内容。\n来源：业务系统",
                            },
                        ),
                        call(
                            7,
                            "write_file",
                            {
                                "path": event["outputs"]["note"],
                                "content": "Completed with source evidence.",
                            },
                        ),
                    ]
                else:
                    calls = []
            self.reply(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None if calls else "完成",
                                "tool_calls": calls,
                            }
                            if calls
                            else {"role": "assistant", "content": "完成"}
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                }
            )
        else:
            self.__class__.receipts.append(body)
            if self.path == "/disconnect":
                self.close_connection = True
                return
            if self.path == "/send_msg":
                self.reply({"status": "ok", "retcode": 0, "data": {"message_id": 13579}})
                return
            self.reply({"accepted": True})


@pytest.fixture
def server():
    Handler.receipts = []
    Handler.requests = []
    Handler.version = 1
    Handler.model_calls = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(srv.server_port)
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def wb(tmp_path, server):
    app = create_app(tmp_path, worker=False, network=Network(["127.0.0.1"]))
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/setup",
            json={"password": "testing-password-123"},
            headers={"X-Requested-With": "workbench"},
        )
        assert r.status_code == 200
        client.headers["X-Requested-With"] = "workbench"
        yield client, app.state.workbench


def post(c, path, body):
    r = c.post("/api/" + path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def bot(c, url, mode="shared"):
    return post(
        c,
        "bots",
        {
            "name": "Delivery Bot",
            "type": "webhook",
            "config": {"url": url + "/receive", "bearer": "private-token"},
            "workspace_mode": mode,
        },
    )


def source(c):
    return post(c, "sources", {"name": "Business events", "type": "webhook", "poll_minutes": 15})


def sub(c, b, s, mode="forward", model=None):
    return post(
        c,
        "subscriptions",
        {
            "name": "Event subscription",
            "workspace_id": b["workspace_id"],
            "source_id": s["id"],
            "mode": mode,
            "model_id": model,
            "enabled": True,
        },
    )


def hook(c, s, event="event-1"):
    r = c.post(
        "/api/hooks/" + s["id"],
        json={"id": event, "title": "业务通知", "text": "原始业务事件内容"},
        headers={"Authorization": "Bearer " + s["hook_token"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_authentication_csrf_and_secret_vault(wb, server):
    c, w = wb
    b = bot(c, server)
    raw = c.get("/api/overview").text
    assert "private-token" not in raw and '"secret":"' not in raw and "vault.key" not in raw
    assert b["has_credentials"] and b["status"] == "READY"
    assert (
        c.post("/api/bots", json={}, headers={"Origin": "https://untrusted.invalid"}).status_code
        == 403
    )
    c.cookies.clear()
    assert c.get("/api/overview").status_code == 401
    assert w.store.check_password("testing-password-123")
    assert (
        "private-token"
        not in w.store.db.execute("SELECT data FROM objects WHERE kind='bot'").fetchone()[0]
    )


def test_real_http_shared_and_independent_delivery(wb, server):
    c, w = wb
    a = bot(c, server)
    b = bot(c, server)
    d = bot(c, server, "independent")
    assert a["workspace_id"] == b["workspace_id"] != d["workspace_id"]
    for x in (a, b, d):
        assert Path(x["workspace_path"], "AGENTS.md").is_file()
    s = source(c)
    sub(c, a, s)
    sub(c, d, s)
    assert hook(c, s)["new_events"] == 1
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert w.store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 3
    for _ in range(2):
        assert asyncio.run(w.process_one())
    for _ in range(3):
        assert asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 3
    assert Handler.receipts[0]["text"] == Handler.receipts[1]["text"]
    assert all(
        r[0] == "ADAPTER_ACCEPTED" for r in w.store.db.execute("SELECT state FROM deliveries")
    )
    assert (
        w.store.db.execute("SELECT COUNT(*) FROM audit WHERE state='CAPTURED'").fetchone()[0] == 1
    )
    assert hook(c, s)["new_events"] == 0
    assert len(Handler.receipts) == 3


def test_independent_agent_tool_loop_and_denied_escape(wb, server):
    c, w = wb
    b = bot(c, server, "independent")
    s = source(c)
    model = post(
        c,
        "models",
        {
            "name": "Protocol test model",
            "model": "fixture-tool-model",
            "base_url": server,
            "api_key": "model-secret",
        },
    )
    sub(c, b, s, "agent", model["id"])
    hook(c, s)
    job = w.store.db.execute("SELECT * FROM jobs").fetchone()
    snap = json.loads(job["snapshot"])
    Path(b["workspace_path"], "docs/rules.md").write_text(
        "Changed after queueing", encoding="utf-8"
    )
    assert snap["rules"]["docs/rules.md"] != "Changed after queueing"
    assert asyncio.run(w.process_one())
    job = w.store.db.execute("SELECT * FROM jobs").fetchone()
    assert job["state"] == "RENDERED", job["error"]
    assert Handler.model_calls == 4
    paths = json.loads(job["paths"])
    trace = json.loads(Path(b["workspace_path"], paths["trace"]).read_text(encoding="utf-8"))
    assert trace["rules"] == snap["rules"]
    assert any(
        "leaves workspace" in m.get("content", "").lower()
        for m in trace["messages"]
        if m["role"] == "tool"
    )
    assert "model-secret" not in dumps(trace)
    assert asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1


def test_workspace_confinement_and_no_overwrite(tmp_path):
    s = Store(tmp_path)
    ws = Workspaces(s)
    a = ws.bind("first", "shared")
    p = Path(a["path"], "docs/rules.md")
    p.write_text("Custom rules", encoding="utf-8")
    b = ws.bind("second", "shared")
    assert a["id"] == b["id"]
    assert p.read_text() == "Custom rules"
    with pytest.raises(ValueError):
        ws.bind("bad", custom_path=str(tmp_path / "outside"))
    with pytest.raises(ValueError):
        ws.file(a["path"], "../../state/vault.key")
    s.db.close()


def test_full_first_feed_window_and_api_change_detection(wb, server):
    c, w = wb
    b = bot(c, server)
    s = post(
        c,
        "sources",
        {"name": "Metrics", "url": server + "/data", "type": "api", "poll_minutes": 5},
    )
    sub(c, b, s)
    assert asyncio.run(w.poll(s["id"]))["baseline_created"]
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    Handler.version = 2
    w.policy.state["feeds"][s["url"]]["next_poll_at"] = 0
    w.policy.state["sites"][w.policy.site(s)]["next_request_at"] = 0
    r = asyncio.run(w.poll(s["id"]))
    assert r["new_events"] == 1
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_rss_and_atom_full_window():
    raw = (
        "<rss><channel>"
        + "".join(
            f"<item><guid>{i}</guid><title>Item {i}</title><link>https://example.com/{i}</link></item>"
            for i in range(80)
        )
        + "</channel></rss>"
    ).encode()
    assert len(feed_items(raw, "https://example.com/feed")) == 80
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>version-1</id><title>Release</title><link href="https://example.com/release"/><updated>2026-10-23T00:00:00Z</updated></entry></feed>'
    assert feed_items(atom, "https://example.com/feed")[0]["item_key"] == "version-1"
    with pytest.raises(ValueError):
        feed_items(b"<!DOCTYPE x><rss/>", "https://example.com")


def test_private_endpoints_are_rejected():
    with pytest.raises(ValueError):
        Network().resolve("https://127.0.0.1/private")
    with pytest.raises(ValueError):
        Network().resolve("https://user:pass@example.com/api")
    with pytest.raises(ValueError):
        Network().resolve("file:///etc/passwd")


def test_persistent_429_cooldown(wb, server):
    c, w = wb
    s = post(
        c,
        "sources",
        {
            "name": "Rate limited",
            "url": server + "/rate",
            "type": "rss",
            "poll_minutes": 5,
        },
    )
    r = asyncio.run(w.poll(s["id"]))
    assert r["deferred"] and r["retry_at"] > time.time() + 1700
    assert w.policy.available_at(s) > time.time()
    assert (w.root / "state/http-state.json").is_file()


def test_unknown_send_and_restart_never_auto_resends(wb, server):
    c, w = wb
    b = post(
        c,
        "bots",
        {
            "name": "Uncertain",
            "type": "webhook",
            "config": {"url": server + "/disconnect"},
        },
    )
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    asyncio.run(w.process_one())
    asyncio.run(w.deliver_one())
    row = w.store.db.execute("SELECT * FROM deliveries").fetchone()
    assert row["state"] == "UNKNOWN"
    assert not asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1
    w.store.db.execute("UPDATE deliveries SET state='SENDING'")
    restored = Store(w.root)
    restored.recover()
    assert restored.db.execute("SELECT state FROM deliveries").fetchone()[0] == "UNKNOWN"
    restored.db.close()


def test_rebinding_cancels_old_pending_targets(wb, server):
    c, w = wb
    b = bot(c, server)
    other = bot(c, server, "independent")
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    asyncio.run(w.process_one())
    b = w.public_bot(w.store.get("bot", b["id"]))
    post(
        c,
        "bots/" + b["id"] + "/update",
        {"revision": b["revision"], "workspace_id": other["workspace_id"]},
    )
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"
    assert not asyncio.run(w.deliver_one())
    assert not Handler.receipts


def test_webhook_auth_and_history_pagination(wb, server):
    c, w = wb
    s = source(c)
    assert c.post("/api/hooks/" + s["id"], json={"id": "a", "text": "value"}).status_code == 401
    for i in range(25):
        w.store.audit("test", "item-" + str(i), "DONE")
    r = c.get("/api/history?kind=audit&page=1&q=item-").json()
    assert r["total"] == 25 and len(r["rows"]) == 20
    assert len(c.get("/api/history?kind=audit&page=2&q=item-").json()["rows"]) == 5


def test_utf16_delivery_parts_are_bounded():
    parts = Workbench.split_text("😀" * 4000, 1500)
    assert len(parts) > 1 and all(len(p.encode("utf-16-le")) // 2 <= 1500 for p in parts)


class ScriptedNetwork:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.resolved = []

    def resolve(self, url):
        self.resolved.append(url)
        return None

    async def json(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


def test_weixin_qr_context_and_real_send_protocol():
    from backend.app.channels import Channels

    net = ScriptedNetwork(
        [
            {
                "qrcode": "registration-code",
                "qrcode_img_content": "https://example.com/login",
            },
            {
                "status": "confirmed",
                "bot_token": "private-weixin-token",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_bot_id": "bot-account",
            },
            {
                "ret": 0,
                "get_updates_buf": "cursor-1",
                "msgs": [{"from_user_id": "user-1", "context_token": "private-context"}],
            },
            {"ret": 0, "message_id": "message-1"},
        ]
    )

    async def scenario():
        channels = Channels(net)
        c, status, _ = await channels.begin("weixin", {})
        assert status == "WAITING_QR" and c["qr_image"].startswith("data:image/png;base64,")
        c, status, _ = await channels.qr_status(c)
        assert status == "WAITING_TARGET"
        c = await channels.receive("weixin", c)
        assert c["targets"][0]["selected"]
        result = await channels.send("weixin", c, "user-1", "Test message", "delivery-1")
        assert (
            result["accepted"] and net.calls[-1][0][2]["msg"]["context_token"] == "private-context"
        )
        assert net.calls[-1][0][2]["msg"]["client_id"] == "delivery-1"

    asyncio.run(scenario())


def test_weixin_login_cannot_redirect_credentials_to_another_host():
    from backend.app.channels import Channels

    net = ScriptedNetwork(
        [
            {
                "status": "confirmed",
                "bot_token": "private-token",
                "baseurl": "https://untrusted.example.com",
            }
        ]
    )
    with pytest.raises(ValueError):
        asyncio.run(
            Channels(net).qr_status(
                {"base_url": "https://ilinkai.weixin.qq.com", "qrcode": "registration"}
            )
        )
    assert len(net.calls) == 1


def test_telegram_does_not_take_over_an_existing_webhook():
    from backend.app.channels import Channels

    net = ScriptedNetwork(
        [
            {"ok": True, "result": {"username": "existing-bot"}},
            {"ok": True, "result": {"url": "https://example.com/old-hook"}},
        ]
    )
    with pytest.raises(ValueError):
        asyncio.run(Channels(net).begin("telegram", {"token": "fixture-token"}))
    assert len(net.calls) == 2 and not any("deleteWebhook" in c[0][1] for c in net.calls)


def test_discord_auth_channel_and_send_contract():
    from backend.app.channels import Channels

    channel = "123456789012345678"
    net = ScriptedNetwork(
        [
            {"id": "bot-1", "bot": True, "username": "subscription-bot"},
            {"id": channel, "name": "release-news"},
            {"id": "message-1"},
        ]
    )

    async def scenario():
        channels = Channels(net)
        config, status, info = await channels.begin(
            "discord", {"token": "private-token", "channel_id": channel}
        )
        assert status == "CONNECTED" and info["account"] == "subscription-bot"
        assert config["channel_id"] == channel
        result = await channels.send("discord", config, channel, "hello @everyone", "delivery-1")
        assert result == {"accepted": True, "message_id": "message-1"}

    asyncio.run(scenario())
    method, url, body = net.calls[-1][0][:3]
    assert method == "POST" and url.endswith("/channels/" + channel + "/messages")
    assert body["allowed_mentions"] == {"parse": []}
    assert body["content"] == "hello @everyone" and len(body["nonce"]) == 24
    assert net.calls[-1][1]["headers"] == {"Authorization": "Bot private-token"}


def test_slack_auth_send_rejection_and_target_validation():
    from backend.app.channels import Channels, PlatformRejected

    net = ScriptedNetwork(
        [
            {"ok": True, "bot_id": "B123", "user": "deploy-bot"},
            {"ok": True, "ts": "1730000000.001"},
            {"ok": False, "error": "channel_not_found"},
        ]
    )

    async def scenario():
        channels = Channels(net)
        c, status, extra = await channels.begin(
            "slack", {"token": "xoxb-fake", "channel_id": "C12345678"}
        )
        assert status == "CONNECTED" and extra["account"] == "deploy-bot"
        assert c["channel_id"] == "C12345678"
        assert (await channels.send("slack", c, "C12345678", "hello", "d-1"))["accepted"]
        with pytest.raises(PlatformRejected, match="channel_not_found"):
            await channels.send("slack", c, "C12345678", "hello", "d-2")

    asyncio.run(scenario())
    assert net.calls[0][0][:2] == ("POST", "https://slack.com/api/auth.test")
    assert net.calls[1][0][2]["channel"] == "C12345678"
    assert net.calls[1][1]["headers"] == {"Authorization": "Bearer xoxb-fake"}
    with pytest.raises(ValueError):
        Channels.draft_config("slack", {"token": "xoxb-fake", "channel_id": "../abc"})


def test_onebot_local_http_login_and_send(wb, server):
    c, w = wb
    b = post(
        c,
        "bots",
        {
            "name": "OneBot",
            "type": "onebot",
            "workspace_mode": "shared",
            "config": {
                "url": server,
                "message_type": "group",
                "target_id": "45678",
                "access_token": "secret",
            },
        },
    )
    assert b["status"] == "CONNECTED" and b["targets"][0]["id"] == "group:45678"
    assert "secret" not in json.dumps(b)
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    assert asyncio.run(w.deliver_one())
    receipt = Handler.receipts[-1]
    assert receipt == {
        "message_type": "group",
        "group_id": 45678,
        "message": Handler.receipts[-1]["message"],
        "auto_escape": True,
    }
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "ADAPTER_ACCEPTED"
    assert post(c, "bots/" + b["id"] + "/reconnect", {})["status"] == "CONNECTED"
    assert (
        post(
            c,
            "bots/" + b["id"] + "/reconnect",
            {"config": {"message_type": "private", "target_id": "13579"}},
        )["targets"][0]["id"]
        == "private:13579"
    )


def test_onebot_config_rejects_private_network_without_allowlist(server):
    from backend.app.channels import Channels

    c = Channels(Network())
    with pytest.raises(ValueError, match="HTTPS|Private network"):
        asyncio.run(c.begin("onebot", {"url": server, "message_type": "group", "target_id": "1"}))
    with pytest.raises(ValueError):
        Channels.draft_config(
            "onebot",
            {"url": "https://example.org/path?key=x", "message_type": "private", "target_id": "1"},
        )


def test_onebot_target_change_cancels_pending_delivery(wb, server):
    c, w = wb
    b = post(
        c,
        "bots",
        {
            "name": "OneBot",
            "type": "onebot",
            "workspace_mode": "shared",
            "config": {"url": server, "message_type": "group", "target_id": "45678"},
        },
    )
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "READY"
    post(
        c,
        "bots/" + b["id"] + "/reconnect",
        {"config": {"message_type": "private", "target_id": "13579"}},
    )
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"
    assert not asyncio.run(w.deliver_one())
    assert not Handler.receipts


def test_feishu_group_signed_text_and_business_receipt(monkeypatch):
    import base64
    import hashlib
    import hmac

    from backend.app.channels import Channels, PlatformRejected

    url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36
    net = ScriptedNetwork([{"code": 0, "msg": "success"}, {"StatusCode": 19024}])
    monkeypatch.setattr("backend.app.channels.time.time", lambda: 1_800_000_000)

    async def scenario():
        channels = Channels(net)
        config, status, _ = await channels.begin(
            "feishu_group", {"url": url, "sign_secret": "private-sign-key"}
        )
        assert status == "READY" and config["targets"][0]["id"] == "group"
        assert (
            await channels.send(
                "feishu_group", config, "group", 'update <at user_id="all">everyone</at>', "d-1"
            )
        )["accepted"]
        with pytest.raises(PlatformRejected):
            await channels.send("feishu_group", config, "group", "second", "d-2")

    asyncio.run(scenario())
    assert net.resolved == [url]
    method, destination, payload = net.calls[0][0][:3]
    assert method == "POST" and destination == url
    assert payload["content"]["text"].startswith("update &lt;at")
    expected = base64.b64encode(
        hmac.new(b"1800000000\nprivate-sign-key", digestmod=hashlib.sha256).digest()
    ).decode()
    assert payload["timestamp"] == "1800000000" and payload["sign"] == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36,
        "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/" + "a" * 36,
        "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36 + "?next=private",
        "https://127.0.0.1/open-apis/bot/v2/hook/" + "a" * 36,
    ],
)
def test_feishu_group_rejects_unofficial_or_private_webhook(url):
    from backend.app.channels import Channels

    with pytest.raises(ValueError):
        Channels.draft_config("feishu_group", {"url": url})


def test_feishu_signing_key_change_cancels_pending_delivery(wb, monkeypatch):
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36
    c, w = wb
    monkeypatch.setattr(w.net, "resolve", lambda value: (None, None))
    b = post(
        c,
        "bots",
        {
            "name": "Feishu group",
            "type": "feishu_group",
            "workspace_mode": "shared",
            "config": {"url": url, "sign_secret": "first-key"},
        },
    )
    assert b["status"] == "READY" and "first-key" not in json.dumps(b)
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "READY"
    post(c, "bots/" + b["id"] + "/reconnect", {"config": {"sign_secret": "second-key"}})
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"


def test_wecom_group_text_and_business_receipt():
    from backend.app.channels import Channels, PlatformRejected

    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36
    net = ScriptedNetwork([{"errcode": 0, "errmsg": "ok"}, {"errcode": 93000}])

    async def scenario():
        channels = Channels(net)
        config, status, _ = await channels.begin("wecom_group", {"url": url})
        assert status == "READY" and config["targets"][0]["selected"]
        assert (await channels.send("wecom_group", config, "group", "notice", "d-1"))["accepted"]
        with pytest.raises(PlatformRejected):
            await channels.send("wecom_group", config, "group", "notice", "d-2")

    asyncio.run(scenario())
    assert net.resolved == [url]
    assert net.calls[0][0][:3] == ("POST", url, {"msgtype": "text", "text": {"content": "notice"}})


def test_dingtalk_group_signature_keyword_and_rejection(monkeypatch):
    import base64
    import hashlib
    import hmac
    from urllib.parse import parse_qs, urlsplit

    from backend.app.channels import Channels, PlatformRejected

    url = "https://oapi.dingtalk.com/robot/send?access_token=" + "b" * 36
    net = ScriptedNetwork([{"errcode": 0, "errmsg": "ok"}, {"errcode": 310000}])
    monkeypatch.setattr("backend.app.channels.time.time", lambda: 1_800_000_000)

    async def scenario():
        channels = Channels(net)
        config, status, _ = await channels.begin(
            "dingtalk_group", {"url": url, "sign_secret": "SECabc123", "keyword": "业务更新"}
        )
        assert status == "READY"
        assert (await channels.send("dingtalk_group", config, "group", "notice", "d-1"))["accepted"]
        with pytest.raises(PlatformRejected):
            await channels.send("dingtalk_group", config, "group", "notice", "d-2")

    asyncio.run(scenario())
    assert net.resolved == [url]
    method, signed_url, payload = net.calls[0][0][:3]
    assert method == "POST"
    assert payload == {"msgtype": "text", "text": {"content": "业务更新\nnotice"}}
    query = parse_qs(urlsplit(signed_url).query)
    assert query["timestamp"] == ["1800000000000"]
    expected = base64.b64encode(
        hmac.new(b"SECabc123", b"1800000000000\nSECabc123", hashlib.sha256).digest()
    ).decode()
    assert query["sign"] == [expected] and query["access_token"] == ["b" * 36]


@pytest.mark.parametrize(
    "kind,url",
    [
        ("wecom_group", "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36),
        (
            "wecom_group",
            "https://qyapi.weixin.qq.com.evil.test/cgi-bin/webhook/send?key=" + "a" * 36,
        ),
        (
            "dingtalk_group",
            "https://oapi.dingtalk.com/robot/send?access_token=" + "b" * 36 + "&sign=fake",
        ),
        ("dingtalk_group", "https://127.0.0.1/robot/send?access_token=" + "b" * 36),
    ],
)
def test_group_webhooks_reject_wrong_destination(kind, url):
    from backend.app.channels import Channels

    with pytest.raises(ValueError):
        Channels.draft_config(kind, {"url": url})


def test_dingtalk_keyword_change_cancels_pending_delivery(wb, monkeypatch):
    url = "https://oapi.dingtalk.com/robot/send?access_token=" + "b" * 36
    c, w = wb
    monkeypatch.setattr(w.net, "resolve", lambda value: (None, None))
    b = post(
        c,
        "bots",
        {
            "name": "DingTalk group",
            "type": "dingtalk_group",
            "workspace_mode": "shared",
            "config": {"url": url, "keyword": "old"},
        },
    )
    assert b["status"] == "READY" and "access_token" not in json.dumps(b)
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "READY"
    post(c, "bots/" + b["id"] + "/reconnect", {"config": {"keyword": "new"}})
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"


def test_wecom_group_rejects_oversize_utf8_message():
    from backend.app.channels import Channels, PlatformRejected

    net = ScriptedNetwork([])
    config = {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36}
    with pytest.raises(PlatformRejected, match="字节预算"):
        asyncio.run(Channels(net).send("wecom_group", config, "group", "汉" * 700, "d-1"))
    assert not net.calls


def test_wecom_delivery_splits_long_chinese_by_utf8_bytes(wb, monkeypatch):
    c, w = wb
    monkeypatch.setattr(w.net, "resolve", lambda value: (None, None))
    b = post(
        c,
        "bots",
        {
            "name": "WeCom group",
            "type": "wecom_group",
            "workspace_mode": "shared",
            "config": {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36},
        },
    )
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    job = w.store.db.execute("SELECT snapshot,paths FROM jobs").fetchone()
    snapshot, paths = json.loads(job["snapshot"]), json.loads(job["paths"])
    report = w.workspaces.file(snapshot["workspace"]["path"], paths["report"])
    report.write_text("汉" * 4000, encoding="utf-8")
    sent = []

    async def capture(kind, config, target, text, delivery_id):
        sent.append(text)
        return {"accepted": True}

    monkeypatch.setattr(w.channels, "send", capture)
    assert asyncio.run(w.deliver_one())
    assert len(sent) > 1 and all(len(part.encode("utf-8")) <= 2000 for part in sent)
    assert "汉" * 4000 == "".join(part.split("\n", 1)[-1] for part in sent)
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "ADAPTER_ACCEPTED"


def test_oversize_report_fails_delivery_without_retry_or_send(wb, monkeypatch):
    c, w = wb
    monkeypatch.setattr(w.net, "resolve", lambda value: (None, None))
    b = post(
        c,
        "bots",
        {
            "name": "WeCom group",
            "type": "wecom_group",
            "workspace_mode": "shared",
            "config": {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36},
        },
    )
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    job = w.store.db.execute("SELECT snapshot,paths FROM jobs").fetchone()
    snapshot, paths = json.loads(job["snapshot"]), json.loads(job["paths"])
    w.workspaces.file(snapshot["workspace"]["path"], paths["report"]).write_text(
        "汉" * 9000, encoding="utf-8"
    )

    async def fail_if_called(*args):
        raise AssertionError("Oversize report must not be sent")

    monkeypatch.setattr(w.channels, "send", fail_if_called)
    assert asyncio.run(w.deliver_one())
    row = w.store.db.execute("SELECT state,error FROM deliveries").fetchone()
    assert row["state"] == "FAILED" and "part budget" in row["error"]
    assert not asyncio.run(w.deliver_one())


def test_single_worker_lock_prevents_double_scheduling(tmp_path):
    async def scenario():
        first = Workbench(tmp_path)
        second = Workbench(tmp_path)
        await first.start()
        try:
            with pytest.raises(RuntimeError):
                await second.start()
        finally:
            await first.stop()
            await second.stop()

    asyncio.run(scenario())


def test_normal_rss_does_not_replay_old_publications(wb, server):
    c, w = wb
    b = bot(c, server)
    s = w.store.put(
        "source",
        {
            "name": "RSS",
            "url": "https://example.com/feed",
            "type": "rss",
            "enabled": True,
            "poll_minutes": 15,
        },
    )
    sub(c, b, s)
    rows = [
        {
            "item_key": "old",
            "title": "Old publication",
            "url": "https://example.com/old",
            "published": "Mon, 01 Jan 2024 00:00:00 GMT",
            "published_timestamp": 1704067200,
            "summary": "Old source evidence",
        }
    ]
    w.ingest(s, rows)
    rows[0]["item_key"] = "another-old"
    w.ingest(s, rows)
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_endpoint_change_invalidates_pending_delivery(wb, server):
    c, w = wb
    b = bot(c, server)
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    asyncio.run(w.process_one())
    post(
        c,
        "bots/" + b["id"] + "/reconnect",
        {"config": {"url": server + "/new-endpoint"}},
    )
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"
    assert not asyncio.run(w.deliver_one())


def test_invalid_configuration_and_stale_revision_are_rejected(wb, server):
    c, w = wb
    b = bot(c, server)
    assert (
        c.post(
            "/api/bots/" + b["id"] + "/update",
            json={"revision": b["revision"], "enabled": "false"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/subscriptions",
            json={
                "name": "Bad",
                "workspace_id": b["workspace_id"],
                "source_id": "missing",
                "mode": "agent",
            },
        ).status_code
        == 400
    )
    s = post(
        c,
        "sources",
        {"name": "Source", "type": "api", "url": server + "/data", "poll_minutes": 5},
    )
    post(c, "sources", {**s, "name": "Updated"})
    assert c.post("/api/sources", json={**s, "name": "Stale"}).status_code == 409


def test_skipped_agent_input_keeps_reason_and_never_sends(wb, server):
    c, w = wb
    b = bot(c, server)
    s = source(c)
    m = post(c, "models", {"name": "Skip model", "model": "fixture", "base_url": server})
    sub(c, b, s, "agent", m["id"])
    hook(c, s)

    async def skip(*args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "SKIP: unrelated input"}}]}

    w.editor.completion = skip
    asyncio.run(w.process_one())
    row = w.store.db.execute("SELECT * FROM jobs").fetchone()
    assert row["state"] == "SKIPPED"
    paths = json.loads(row["paths"])
    assert "unrelated input" in Path(b["workspace_path"], paths["note"]).read_text(encoding="utf-8")
    assert not asyncio.run(w.deliver_one()) and not Handler.receipts


def manage(c, kind, action, rows, mode="apply"):
    return c.post(
        "/api/manage/" + mode,
        json={
            "kind": kind,
            "action": action,
            "items": [{"id": r["id"], "revision": r["revision"]} for r in rows],
        },
    )


def test_workspace_ai_settings_are_task_scoped_and_versioned(wb, server):
    c, w = wb
    a = bot(c, server, "independent")
    b = bot(c, server, "independent")
    src = source(c)
    for item, focus in ((a, "只关注能源政策"), (b, "只关注软件版本发布")):
        workspace = c.get("/api/workspaces/" + item["workspace_id"] + "/settings").json()
        profile = workspace["ai_profile"]
        profile.update(
            focus_prompt=focus,
            processing_prompt="保留证据，输出三条要点",
            output_template="标题：\n要点：\n来源：",
            excluded_topics="广告与未经证实的传闻",
        )
        result = post(
            c,
            "workspaces/" + workspace["id"] + "/settings",
            {"revision": workspace["revision"], "name": focus, "ai_profile": profile},
        )
        assert result["ai_profile"]["focus_prompt"] == focus
        assert (
            c.post(
                "/api/workspaces/" + workspace["id"] + "/settings",
                json={"revision": workspace["revision"], "ai_profile": profile},
            ).status_code
            == 409
        )
        sub(c, item, src)
    hook(c, src)
    jobs = w.store.db.execute("SELECT * FROM jobs ORDER BY created").fetchall()
    assert len(jobs) == 2
    snapshots = [json.loads(j["snapshot"]) for j in jobs]
    assert "只关注能源政策" in snapshots[0]["rules"]["docs/rules.md"]
    assert "只关注软件版本发布" in snapshots[1]["rules"]["docs/rules.md"]
    assert snapshots[0]["workspace_profile"] != snapshots[1]["workspace_profile"]
    workspace = c.get("/api/workspaces/" + a["workspace_id"] + "/settings").json()
    profile = workspace["ai_profile"]
    profile["focus_prompt"] = "后来修改的关注范围"
    post(
        c,
        "workspaces/" + workspace["id"] + "/settings",
        {"revision": workspace["revision"], "ai_profile": profile},
    )
    assert (
        "后来修改"
        not in json.loads(
            w.store.db.execute("SELECT snapshot FROM jobs WHERE id=?", (jobs[0]["id"],)).fetchone()[
                0
            ]
        )["rules"]["docs/rules.md"]
    )


def test_legacy_rules_are_migrated_without_overwriting_files(wb, server):
    c, w = wb
    b = bot(c, server, "independent")
    path = Path(b["workspace_path"], "docs/rules.md")
    path.write_text("原来的专属整理要求", encoding="utf-8")
    legacy = w.store.get("workspace", b["workspace_id"])
    legacy.pop("ai_profile", None)
    w.store.put("workspace", legacy)
    workspace = c.get("/api/workspaces/" + b["workspace_id"] + "/settings").json()
    assert workspace["ai_profile"]["processing_prompt"] == "原来的专属整理要求"
    profile = workspace["ai_profile"]
    profile["processing_prompt"] = "页面配置的新整理要求"
    post(
        c,
        "workspaces/" + workspace["id"] + "/settings",
        {"revision": workspace["revision"], "ai_profile": profile},
    )
    assert path.read_text(encoding="utf-8") == "原来的专属整理要求"
    assert (
        c.get("/api/workspaces/" + workspace["id"] + "/settings").json()["ai_profile"][
            "processing_prompt"
        ]
        == "页面配置的新整理要求"
    )
    assert (
        c.post(
            "/api/workspaces/" + workspace["id"] + "/files",
            json={"path": "docs/rules.md", "content": "legacy"},
        ).status_code
        == 409
    )


def test_bulk_bot_deletion_keeps_other_recipients_and_local_files(wb, server):
    c, w = wb
    a = bot(c, server)
    b = bot(c, server)
    src = source(c)
    sub(c, a, src)
    hook(c, src)
    impact = manage(c, "bot", "delete", [a], "preview").json()
    assert impact["pending_deliveries"] == 1
    assert manage(c, "bot", "delete", [a]).status_code == 200
    assert Path(a["workspace_path"], "AGENTS.md").is_file()
    asyncio.run(w.process_one())
    asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1
    states = {r["bot_id"]: r["state"] for r in w.store.db.execute("SELECT * FROM deliveries")}
    assert states[a["id"]] == "CANCELLED" and states[b["id"]] == "ADAPTER_ACCEPTED"


def test_deleting_one_shared_subscription_does_not_cancel_the_other(wb, server):
    c, w = wb
    b = bot(c, server)
    src = source(c)
    first = sub(c, b, src)
    second = sub(c, b, src)
    hook(c, src)
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 2
    assert manage(c, "subscription", "delete", [first]).status_code == 200
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"
    w.store.recover()  # recovery must not resurrect a removed subscription membership
    assert manage(c, "subscription", "delete", [second]).status_code == 200
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "CANCELLED"
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"


def test_source_deletion_cascades_bindings_but_preserves_history(wb, server):
    c, w = wb
    b = bot(c, server)
    src = source(c)
    sub(c, b, src)
    hook(c, src)
    current = w.store.get("source", src["id"])
    impact = manage(c, "source", "delete", [current], "preview").json()
    assert impact["related_subscriptions"] == 1
    assert manage(c, "source", "delete", [current]).status_code == 200
    assert not w.store.all("subscription")
    assert w.store.db.execute("SELECT COUNT(*) FROM inputs").fetchone()[0] == 1
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "CANCELLED"
    assert Path(b["workspace_path"]).exists()


def test_workspace_registration_deletion_requires_unbound_bots_and_keeps_files(wb, server):
    c, w = wb
    b = bot(c, server, "independent")
    workspace = w.store.get("workspace", b["workspace_id"])
    assert manage(c, "workspace", "delete", [workspace]).status_code == 409
    assert manage(c, "bot", "delete", [b]).status_code == 200
    assert manage(c, "workspace", "delete", [workspace]).status_code == 200
    assert Path(workspace["path"], "AGENTS.md").is_file()
    assert not w.store.all("workspace")


def test_model_deletion_rebinds_defaults_and_cancels_unstarted_calls(wb, server):
    c, w = wb
    b = bot(c, server)
    src = source(c)
    first = post(c, "models", {"name": "Primary", "model": "first", "base_url": server})
    second = post(c, "models", {"name": "Fallback", "model": "second", "base_url": server})
    binding = sub(c, b, src, "agent", first["id"])
    hook(c, src)
    assert manage(c, "model", "delete", [first]).status_code == 200
    assert w.store.setting("default_model") == second["id"]
    assert w.store.get("subscription", binding["id"])["model_id"] is None
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "CANCELLED"


def test_bulk_mutation_is_atomic_and_sending_guard_is_enforced(wb, server):
    c, w = wb
    a = bot(c, server)
    b = bot(c, server)
    fresh = post(c, "bots/" + b["id"] + "/update", {"revision": b["revision"], "name": "Renamed"})
    assert manage(c, "bot", "delete", [a, b]).status_code == 409
    assert len(w.store.all("bot")) == 2
    src = source(c)
    sub(c, a, src)
    hook(c, src)
    asyncio.run(w.process_one())
    w.store.db.execute("UPDATE deliveries SET state='SENDING' WHERE bot_id=?", (a["id"],))
    assert manage(c, "bot", "delete", [a]).status_code == 409
    assert w.store.get("bot", a["id"])["name"] == a["name"]
    assert manage(c, "bot", "disable", [a, fresh]).status_code == 200
    assert all(not bot["enabled"] for bot in w.store.all("bot"))


def test_removed_source_inflight_check_cannot_ingest_old_results(wb, server):
    c, w = wb
    src = post(
        c,
        "sources",
        {"name": "In-flight", "type": "api", "url": server + "/data", "poll_minutes": 5},
    )

    async def remove_during_check(source):
        assert (
            w.manage(
                {
                    "kind": "source",
                    "action": "delete",
                    "items": [{"id": source["id"], "revision": source["revision"]}],
                }
            )["count"]
            == 1
        )
        return [{"item_key": "late", "title": "Late result", "text": "Should not be ingested"}]

    w.sources.discover = remove_during_check
    assert asyncio.run(w.poll(src["id"]))["cancelled"]
    assert w.store.db.execute("SELECT COUNT(*) FROM inputs").fetchone()[0] == 0


def test_legacy_appended_agent_rules_and_custom_templates_are_not_discarded(wb, server):
    c, w = wb
    b = bot(c, server, "independent")
    workspace = w.store.get("workspace", b["workspace_id"])
    workspace.pop("ai_profile")
    w.store.put("workspace", workspace)
    root = Path(workspace["path"])
    original = (root / "AGENTS.md").read_text(encoding="utf-8")
    custom = "自定义关注范围：" + ("市场政策。" * 3500) + "保留到末尾的要求"
    (root / "AGENTS.md").write_text(original + "\n" + custom, encoding="utf-8")
    template = (root / "docs/template.md").read_text(
        encoding="utf-8"
    ) + "\n客户自定义的栏目不能丢失"
    (root / "docs/template.md").write_text(template, encoding="utf-8")
    migrated = c.get("/api/workspaces/" + workspace["id"] + "/settings").json()["ai_profile"]
    assert custom in migrated["additional_instructions"]
    assert migrated["output_template"] == template
    assert (
        c.post(
            "/api/workspaces/" + workspace["id"] + "/settings",
            json={"revision": workspace["revision"] + 1, "ai_profile": migrated},
        ).status_code
        == 200
    )


def test_shared_workspace_can_be_named_and_reused_without_implicit_rename(wb, server):
    c, w = wb
    first = post(
        c,
        "bots",
        {
            "name": "First shared bot",
            "type": "webhook",
            "config": {"url": server + "/receive"},
            "workspace_mode": "shared",
            "workspace_name": "  市场资讯  ",
        },
    )
    assert first["workspace_name"] == "市场资讯"
    assert Path(first["workspace_path"]).name == "shared"
    second = bot(c, server)
    assert (
        second["workspace_id"] == first["workspace_id"] and second["workspace_name"] == "市场资讯"
    )
    from backend.app.core import Conflict

    with pytest.raises(Conflict):
        w.workspaces.bind("other", "shared", workspace_name="不同名称")
    assert w.store.get("workspace", first["workspace_id"])["name"] == "市场资讯"


def test_rename_workspace_preserves_identity_rules_bindings_and_queued_history(wb, server):
    c, w = wb
    a = bot(c, server)
    b = bot(c, server)
    src = source(c)
    subscription = sub(c, a, src)
    hook(c, src)
    workspace = w.store.get("workspace", a["workspace_id"])
    snapshot = w.store.db.execute("SELECT snapshot FROM jobs").fetchone()[0]
    result = post(
        c,
        "workspaces/" + workspace["id"] + "/rename",
        {"revision": workspace["revision"], "name": "  外汇与大宗商品  "},
    )
    assert (
        result["name"] == "外汇与大宗商品"
        and result["id"] == workspace["id"]
        and result["path"] == workspace["path"]
    )
    assert result["ai_profile"] == workspace["ai_profile"]
    assert w.store.get("subscription", subscription["id"])["workspace_id"] == workspace["id"]
    assert w.public_bot(w.store.get("bot", a["id"]))["workspace_name"] == "外汇与大宗商品"
    assert w.public_bot(w.store.get("bot", b["id"]))["workspace_name"] == "外汇与大宗商品"
    assert w.store.db.execute("SELECT snapshot FROM jobs").fetchone()[0] == snapshot
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"
    assert Path(workspace["path"], "AGENTS.md").is_file()


def test_workspace_name_validation_and_rename_conflict(wb, server):
    c, w = wb
    b = bot(c, server)
    workspace = w.store.get("workspace", b["workspace_id"])
    for name in ("   ", "x" * 101):
        assert (
            c.post(
                "/api/workspaces/" + workspace["id"] + "/rename",
                json={"revision": workspace["revision"], "name": name},
            ).status_code
            == 400
        )
    post(
        c,
        "workspaces/" + workspace["id"] + "/rename",
        {"revision": workspace["revision"], "name": "新名称"},
    )
    assert (
        c.post(
            "/api/workspaces/" + workspace["id"] + "/rename",
            json={"revision": workspace["revision"], "name": "过期编辑"},
        ).status_code
        == 409
    )
    count = len(w.store.all("bot"))
    assert (
        c.post(
            "/api/bots",
            json={
                "name": "Invalid name",
                "type": "webhook",
                "workspace_name": "  ",
                "config": {"url": server + "/receive"},
            },
        ).status_code
        == 400
    )
    assert len(w.store.all("bot")) == count


def test_one_subscription_routes_multiple_sources_and_trims_only_removed_source(wb, server):
    c, w = wb
    b = bot(c, server)
    a = source(c)
    other = source(c)
    binding = post(
        c,
        "subscriptions",
        {
            "name": "Two sources",
            "workspace_id": b["workspace_id"],
            "source_ids": [a["id"], other["id"]],
            "mode": "forward",
        },
    )
    assert binding["source_ids"] == [a["id"], other["id"]]
    hook(c, a, "first-event")
    hook(c, other, "second-event")
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    first = w.store.get("source", a["id"])
    impact = manage(c, "source", "delete", [first], "preview").json()
    assert impact["updated_subscriptions"] == 1 and impact["related_subscriptions"] == 0
    assert manage(c, "source", "delete", [first]).status_code == 200
    saved = w.store.get("subscription", binding["id"])
    assert saved["source_ids"] == [other["id"]] and saved["source_id"] == other["id"]
    states = {
        r["source_id"]: r["state"]
        for r in w.store.db.execute(
            "SELECT inputs.source_id,jobs.state FROM jobs JOIN inputs ON inputs.id=jobs.event_id"
        )
    }
    assert states[a["id"]] == "CANCELLED" and states[other["id"]] == "QUEUED"
    asyncio.run(w.process_one())
    asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1


def test_editing_source_set_keeps_shared_pending_tasks_for_other_subscriptions(wb, server):
    c, w = wb
    b = bot(c, server)
    a = source(c)
    other = source(c)
    group = post(
        c,
        "subscriptions",
        {
            "name": "Group",
            "workspace_id": b["workspace_id"],
            "source_ids": [a["id"], other["id"]],
            "mode": "forward",
        },
    )
    independent = sub(c, b, a)
    hook(c, a)
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    changed = post(c, "subscriptions", {**group, "source_ids": [other["id"]]})
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 1
    w.store.recover()
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 1
    assert manage(c, "subscription", "delete", [independent]).status_code == 200
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "CANCELLED"
    assert changed["source_ids"] == [other["id"]]


def test_source_selection_validation_and_legacy_bindings(wb, server):
    c, w = wb
    b = bot(c, server)
    a = source(c)
    base = {"name": "Sources", "workspace_id": b["workspace_id"], "mode": "forward"}
    for invalid in ([], [a["id"], a["id"]], ["missing"], "not-an-array"):
        assert c.post("/api/subscriptions", json={**base, "source_ids": invalid}).status_code == 400
    legacy = w.store.put("subscription", {**base, "source_id": a["id"], "enabled": True})
    dto = next(x for x in c.get("/api/overview").json()["subscriptions"] if x["id"] == legacy["id"])
    assert dto["source_ids"] == [a["id"]]
    hook(c, a)
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_scheduler_checks_every_enabled_source_in_a_group(wb, server):
    c, w = wb
    b = bot(c, server)
    sources = [
        post(
            c,
            "sources",
            {
                "name": "Metric " + str(i),
                "type": "api",
                "url": server + "/data?i=" + str(i),
                "poll_minutes": 5,
            },
        )
        for i in range(2)
    ]
    post(
        c,
        "subscriptions",
        {
            "name": "Both",
            "workspace_id": b["workspace_id"],
            "source_ids": [s["id"] for s in sources],
            "mode": "forward",
        },
    )
    checked = []

    async def poll(key):
        checked.append(key)

    w.poll = poll
    asyncio.run(w.tick())
    assert set(checked) == {s["id"] for s in sources}


def test_telegram_connection_failure_keeps_encrypted_draft_and_can_retry(wb, server):
    c, w = wb
    captured = []
    legacy = w.public_bot(
        {
            "id": "bot-legacy",
            "type": "telegram",
            "status": "CONNECTION_FAILED",
            "workspace_id": None,
            "error": "ConnectionRefusedError",
        }
    )
    assert "重新填写" in legacy["error"] and not legacy["has_credentials"]

    async def failing(kind, config):
        raise ConnectionRefusedError()

    original = w.channels.begin
    w.channels.begin = failing
    token = "fixture-retained-private-token"
    b = post(
        c,
        "bots",
        {"name": "TG", "type": "telegram", "config": {"token": token}},
    )
    assert b["status"] == "CONNECTION_FAILED" and b["has_credentials"] and "直连失败" in b["error"]
    stored = w.store.get("bot", b["id"])
    assert w.store.open(stored["draft_secret"])["token"] == token
    assert (
        token not in dumps(stored)
        and "draft_secret" not in b
        and token not in c.get("/api/overview").text
    )

    async def connected(kind, config):
        captured.append(config)
        return (
            {"token": config["token"], "targets": []},
            "WAITING_TARGET",
            {"account": "fixture"},
        )

    w.channels.begin = connected
    result = post(c, "bots/" + b["id"] + "/reconnect", {})
    assert result["status"] == "WAITING_TARGET" and result["workspace_id"]
    assert captured[0]["token"] == token
    assert "draft_secret" not in w.store.get("bot", b["id"])
    w.channels.begin = original


def test_failed_reconfiguration_does_not_replace_verified_account_or_send_old_content(wb, server):
    c, w = wb
    b = bot(c, server)
    a = source(c)
    sub(c, b, a)
    hook(c, a)
    asyncio.run(w.process_one())
    original = w.channels.begin

    async def failing(kind, config):
        raise ConnectionRefusedError()

    w.channels.begin = failing
    assert (
        c.post(
            "/api/bots/" + b["id"] + "/reconnect",
            json={"config": {"url": server + "/another-account"}},
        ).status_code
        == 400
    )
    stored = w.store.get("bot", b["id"])
    assert w.store.open(stored["secret"])["url"] == server + "/receive"
    assert w.store.open(stored["draft_secret"])["url"] == server + "/another-account"
    w.channels.begin = original
    post(c, "bots/" + b["id"] + "/reconnect", {})
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"
    assert not asyncio.run(w.deliver_one()) and not Handler.receipts


def test_telegram_all_api_calls_follow_saved_global_proxy_or_direct_without_one():
    from backend.app.channels import Channels

    cfg = {"scheme": "http", "host": "127.0.0.1", "port": 7890, "remote_dns": True}
    responses = [
        {"ok": True, "result": {"username": "fixture"}},
        {"ok": True, "result": {"url": ""}},
        {"ok": True, "result": []},
        {"ok": True, "result": {"message_id": 10}},
    ]
    net = ScriptedNetwork(responses)
    channels = Channels(net, lambda c: cfg)

    async def scenario():
        c, state, _ = await channels.begin("telegram", {"token": "fixture-token"})
        c = await channels.receive("telegram", c)
        assert (await channels.send("telegram", c, "test-chat", "Test", "delivery-fixture"))[
            "accepted"
        ]

    asyncio.run(scenario())
    assert len(net.calls) == 4 and all(kwargs["proxy"] == cfg for args, kwargs in net.calls)
    direct = ScriptedNetwork(responses[:2])
    channels = Channels(direct, lambda c: None)
    asyncio.run(channels.begin("telegram", {"token": "fixture-token"}))
    assert all("proxy" not in kwargs for args, kwargs in direct.calls)


def test_telegram_uses_saved_proxy_even_with_global_collection_switch_off(wb):
    _, w = wb
    assert w.bot_proxy({"network_mode": "proxy"}) is None
    w.save_proxy_preferences(
        {
            "revision": 0,
            "enabled": False,
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 7890,
            "remote_dns": True,
        }
    )
    assert w.bot_proxy({"network_mode": "direct"})["port"] == 7890
    assert w.effective_proxy({"proxy_mode": "inherit"}) is None
    assert w.effective_proxy({"proxy_mode": "direct"}) is None


def package(*, source_type="rss", url="https://example.com/feed.xml"):
    return {
        "format": "subscription-workbench/package",
        "version": 1,
        "package": {"key": "test-pack", "name": "测试包", "description": "可移植配置"},
        "categories": [
            {
                "key": "research",
                "name": "研究",
                "tags": [
                    {"key": "alpha", "name": "Alpha"},
                    {"key": "beta", "name": "Beta"},
                ],
                "sources": [
                    {
                        "name": "Example",
                        "type": source_type,
                        "url": url,
                        "tags": ["alpha"],
                        "poll_minutes": 15,
                    }
                ],
            }
        ],
    }


def test_builtin_packages_validate_and_export_is_portable(wb):
    c, w = wb
    listed = c.get("/api/packages").json()["packages"]
    assert {p["manifest"]["package"]["key"] for p in listed} == {
        "finance",
        "technology",
        "github-hot",
    }
    manifest = package()
    assert post(c, "packages/preview", {"manifest": manifest})["sources_new"] == 1
    first = w.import_package(manifest)
    assert first["categories_new"] == 1 and first["tags_new"] == 2
    assert len(w.store.all("source")) == 1
    assert not w.store.all("subscription")
    assert w.import_package(manifest)["sources_existing"] == 1
    assert len(w.store.all("source")) == 1
    category = w.store.all("category")[0]
    result = post(
        c,
        "packages/export",
        {
            "key": "research-export",
            "name": "研究包",
            "description": "公开配置",
            "category_ids": [category["id"]],
        },
    )
    assert result["categories"][0]["sources"][0]["tags"] == ["alpha"]
    assert "hook_hash" not in json.dumps(result) and "secret" not in json.dumps(result)
    assert w.validate_package(result) == result


def test_package_api_import_is_additive_and_does_not_create_subscription(wb, monkeypatch):
    c, w = wb
    checked = []
    monkeypatch.setattr(w.net, "destination", lambda url, proxy=None: checked.append(url))
    manifest = package(source_type="api", url="https://example.com/data.json")
    manifest["categories"][0]["sources"][0].update(
        {
            "json_path": "data.items",
            "id_field": "id",
            "title_field": "headline",
        }
    )
    assert post(c, "packages/import", {"manifest": manifest})["sources_new"] == 1
    assert checked == ["https://example.com/data.json"]
    assert post(c, "packages/import", {"manifest": manifest})["sources_existing"] == 1
    assert checked == ["https://example.com/data.json"]
    assert len(w.store.all("source")) == 1 and not w.store.all("subscription")
    source = w.store.all("source")[0]
    assert source["type"] == "api" and source["title_field"] == "headline"


def test_package_import_checks_all_new_urls_before_mutating(wb, monkeypatch):
    c, w = wb
    manifest = package()
    manifest["categories"][0]["sources"].append(
        {
            "name": "Another",
            "type": "rss",
            "url": "https://blocked.example/feed",
            "tags": ["beta"],
        }
    )

    def deny(url, proxy=None):
        if "blocked" in url:
            raise ValueError("地址校验未通过")

    monkeypatch.setattr(w.net, "destination", deny)
    assert c.post("/api/packages/import", json={"manifest": manifest}).status_code == 400
    assert not w.store.all("source") and not w.store.all("category")


def test_taxonomy_rejects_unhashable_tags_as_validation_error(wb):
    c, _ = wb
    category = post(c, "categories", {"key": "research", "name": "研究"})
    result = c.post(
        "/api/sources",
        json={
            "name": "Webhook",
            "type": "webhook",
            "category_id": category["id"],
            "tag_ids": [["invalid"]],
        },
    )
    assert result.status_code == 400


def test_package_rejects_private_url_secret_field_and_conflicting_taxonomy(wb):
    c, w = wb
    data = package()
    data["categories"][0]["sources"][0]["url"] = "http://127.0.0.1/private"
    assert c.post("/api/packages/preview", json={"manifest": data}).status_code == 400
    data = package()
    data["categories"][0]["sources"][0]["token"] = "do-not-import"
    assert c.post("/api/packages/preview", json={"manifest": data}).status_code == 400
    data = package()
    w.import_package(data)
    other = package()
    other["categories"][0]["key"] = "different"
    assert c.post("/api/packages/preview", json={"manifest": other}).json()["conflicts"]
    assert c.post("/api/packages/import", json={"manifest": other}).status_code == 409
    assert len(w.store.all("source")) == 1


def test_taxonomy_subscription_expands_for_new_matching_source(wb, server):
    c, w = wb
    b = bot(c, server)
    category = post(c, "categories", {"key": "finance", "name": "金融"})
    tag = post(c, "tags", {"category_id": category["id"], "key": "gold", "name": "黄金"})
    a = post(
        c,
        "sources",
        {"name": "First", "type": "webhook", "category_id": category["id"], "tag_ids": [tag["id"]]},
    )
    subscription = post(
        c,
        "subscriptions",
        {
            "name": "Gold",
            "workspace_id": b["workspace_id"],
            "category_id": category["id"],
            "tag_ids": [tag["id"]],
            "source_ids": [],
            "mode": "forward",
            "enabled": True,
        },
    )
    assert subscription["resolved_source_ids"] == [a["id"]]
    second = post(
        c,
        "sources",
        {
            "name": "Second",
            "type": "webhook",
            "category_id": category["id"],
            "tag_ids": [tag["id"]],
        },
    )
    third = post(
        c,
        "sources",
        {"name": "No tag", "type": "webhook", "category_id": category["id"], "tag_ids": []},
    )
    assert set(c.get("/api/overview").json()["subscriptions"][0]["resolved_source_ids"]) == {
        a["id"],
        second["id"],
    }
    assert third["id"] not in resolved_source_ids(
        w.store.get("subscription", subscription["id"]), w.store.all("source")
    )
    hook(c, second, "tagged")
    hook(c, third, "untagged")
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert (
        c.post(
            "/api/taxonomy/tag/" + tag["id"] + "/delete", json={"revision": tag["revision"]}
        ).status_code
        == 409
    )
    assert (
        c.post(
            "/api/taxonomy/category/" + category["id"] + "/delete",
            json={"revision": category["revision"]},
        ).status_code
        == 409
    )


def test_reclassifying_source_cancels_unmatched_queued_delivery(wb, server):
    c, w = wb
    b = bot(c, server)
    category = post(c, "categories", {"key": "finance", "name": "金融"})
    other = post(c, "categories", {"key": "technology", "name": "科技"})
    tag = post(c, "tags", {"category_id": category["id"], "key": "gold", "name": "黄金"})
    src = post(
        c,
        "sources",
        {
            "name": "Gold",
            "type": "webhook",
            "category_id": category["id"],
            "tag_ids": [tag["id"]],
        },
    )
    post(
        c,
        "subscriptions",
        {
            "name": "Gold",
            "workspace_id": b["workspace_id"],
            "category_id": category["id"],
            "tag_ids": [tag["id"]],
            "mode": "forward",
        },
    )
    hook(c, src)
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"
    post(c, "sources", {**src, "category_id": other["id"], "tag_ids": []})
    w.store.recover()
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 0
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "CANCELLED"
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"
    assert not asyncio.run(w.process_one()) and not asyncio.run(w.deliver_one())
    assert not Handler.receipts


def test_retagging_source_keeps_other_shared_subscription(wb, server):
    c, w = wb
    b = bot(c, server)
    category = post(c, "categories", {"key": "finance", "name": "金融"})
    gold = post(c, "tags", {"category_id": category["id"], "key": "gold", "name": "黄金"})
    silver = post(c, "tags", {"category_id": category["id"], "key": "silver", "name": "白银"})
    src = post(
        c,
        "sources",
        {
            "name": "Metals",
            "type": "webhook",
            "category_id": category["id"],
            "tag_ids": [gold["id"], silver["id"]],
        },
    )
    subs = [
        post(
            c,
            "subscriptions",
            {
                "name": name,
                "workspace_id": b["workspace_id"],
                "category_id": category["id"],
                "tag_ids": [tag["id"]],
                "mode": "forward",
            },
        )
        for name, tag in (("Gold", gold), ("Silver", silver))
    ]
    hook(c, src)
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 2
    src = post(c, "sources", {**src, "tag_ids": [silver["id"]]})
    w.store.recover()
    assert [r[0] for r in w.store.db.execute("SELECT subscription_id FROM job_subscriptions")] == [
        subs[1]["id"]
    ]
    assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "QUEUED"
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "WAITING_CONTENT"
    assert asyncio.run(w.process_one()) and asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1


def test_reclassification_rejected_while_only_job_is_running(wb, server):
    c, w = wb
    b = bot(c, server)
    category = post(c, "categories", {"key": "finance", "name": "金融"})
    src = post(c, "sources", {"name": "Gold", "type": "webhook", "category_id": category["id"]})
    post(
        c,
        "subscriptions",
        {
            "name": "Gold",
            "workspace_id": b["workspace_id"],
            "category_id": category["id"],
            "mode": "forward",
        },
    )
    hook(c, src)
    w.store.db.execute("UPDATE jobs SET state='RUNNING'")
    response = c.post("/api/sources", json={**src, "category_id": None, "tag_ids": []})
    assert response.status_code == 409
    assert w.store.get("source", src["id"])["category_id"] == category["id"]
    assert w.store.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 1


def test_membership_table_migration_runs_once(tmp_path):
    legacy = Store(tmp_path)
    subscription = legacy.put("subscription", {"name": "legacy"})
    legacy.db.execute(
        "INSERT INTO jobs(id,snapshot,state) VALUES(?,?,?)",
        ("job-legacy", json.dumps({"subscription_id": subscription["id"]}), "QUEUED"),
    )
    legacy.db.execute("DROP TABLE job_subscriptions")
    legacy.db.close()
    migrated = Store(tmp_path)
    assert migrated.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 1
    migrated.db.execute("DELETE FROM job_subscriptions")
    migrated.db.close()
    restarted = Store(tmp_path)
    restarted.recover()
    assert restarted.db.execute("SELECT COUNT(*) FROM job_subscriptions").fetchone()[0] == 0
    restarted.db.close()


def test_api_field_mapping_and_stable_change_policy(wb, server):
    c, w = wb
    source = post(
        c,
        "sources",
        {
            "name": "Mapped API",
            "type": "api",
            "url": server + "/data",
            "json_path": "",
            "id_field": "id",
            "title_field": "title",
            "content_field": "title",
            "update_on_change": False,
        },
    )
    rows = asyncio.run(w.sources.discover(w.store.get("source", source["id"])))
    assert rows[0]["item_key"] == "metric" and rows[0]["text"] == "Service status"
    changed = {**rows[0], "text": "Different metadata"}
    assert w.sources.version(rows[0], source) == w.sources.version(changed, source)
    assert w.sources.version(rows[0]) != w.sources.version(changed)


def test_undated_rss_requires_explicit_opt_in_after_baseline(wb, server):
    c, w = wb
    b = bot(c, server)
    s = post(c, "sources", {"name": "Undated", "type": "rss", "url": server + "/feed"})
    sub(c, b, s)
    row = {
        "item_key": "old",
        "title": "Old",
        "url": "https://example.com/old",
        "summary": "Older entry",
        "content_type": "article",
    }
    assert w.ingest(w.store.get("source", s["id"]), [row])["baseline_created"]
    row["item_key"] = "new"
    assert w.ingest(w.store.get("source", s["id"]), [row])["new_events"] == 0
    source = w.store.get("source", s["id"])
    source = post(c, "sources", {**source, "allow_undated": True, "capture_mode": "summary"})
    row["item_key"] = "later"
    row["summary"] = "A sufficiently long feed summary for the opt-in test and no invented content."
    assert w.ingest(w.store.get("source", s["id"]), [row])["new_events"] == 1
    assert w.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    captured = asyncio.run(w.sources.capture(row, source))
    assert captured["content_level"] == "summary_only"
