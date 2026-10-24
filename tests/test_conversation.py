import asyncio
import json

from test_workbench import Handler, bot, hook, post, source, sub

from backend.app.channels import Channels

pytest_plugins = ["test_workbench"]


def model(c, url, name):
    return post(
        c,
        "models",
        {"name": name, "model": "test-model", "base_url": url, "api_key": "test-key"},
    )


def incoming(w, b, identity, text, target="endpoint", sender="user"):
    config = w.store.open(w.store.get("bot", b["id"])["secret"])
    return w.accept_inbound(
        w.store.get("bot", b["id"]),
        config,
        {"id": identity, "target": target, "sender": sender, "text": text},
    )


def test_telegram_normalizes_only_addressed_text_and_advances_cursor():
    class Network:
        async def json(self, *_args, **_kwargs):
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 7},
                            "text": "请解释刚才的消息",
                        },
                    },
                    {
                        "update_id": 11,
                        "message": {
                            "chat": {"id": -12, "type": "supergroup"},
                            "from": {"id": 8},
                            "text": "普通聊天",
                        },
                    },
                    {
                        "update_id": 12,
                        "message": {
                            "chat": {"id": -12, "type": "supergroup"},
                            "from": {"id": 9},
                            "text": "@my_bot 帮我看看",
                            "message_thread_id": 5,
                        },
                    },
                    {"update_id": 13, "my_chat_member": {"chat": {"id": -13}}},
                ],
            }

    config = {"token": "test", "offset": 10, "username": "my_bot", "account_id": "20"}
    updated, messages = asyncio.run(Channels(Network()).receive("telegram", config, True))
    assert updated["offset"] == 14
    assert [(m["id"], m["target"]) for m in messages] == [("10", "42"), ("12", "-12")]
    assert messages[1]["thread"] == "5" and messages[1]["group"]
    assert len(updated["targets"]) == 3


def test_weixin_only_enqueues_identified_user_text():
    class Network:
        async def json(self, *_args, **_kwargs):
            return {
                "ret": 0,
                "get_updates_buf": "cursor-next",
                "msgs": [
                    {
                        "message_id": "message-1",
                        "from_user_id": "user-1",
                        "context_token": "context-1",
                        "item_list": [None, {"type": 1, "text_item": {"text": "问题内容"}}],
                    },
                    {
                        "message_id": "message-2",
                        "from_user_id": "bot-account",
                        "context_token": "context-2",
                        "item_list": [{"type": 1, "text_item": {"text": "机器人发出的回复"}}],
                    },
                    {
                        "from_user_id": "user-1",
                        "context_token": "context-3",
                        "item_list": [{"type": 1, "text_item": {"text": "缺少稳定消息 ID"}}],
                    },
                ],
            }

    config = {
        "base_url": "https://ilinkai.weixin.qq.com",
        "token": "private",
        "account_id": "bot-account",
    }
    updated, messages = asyncio.run(Channels(Network()).receive("weixin", config, True))
    assert updated["sync_buf"] == "cursor-next"
    assert updated["contexts"]["user-1"] == "context-3"
    assert len(messages) == 1 and messages[0]["text"] == "问题内容"


def test_inbound_dedupe_chat_model_and_reply_priority(wb, server):
    c, w = wb
    b = bot(c, server)
    publishing = model(c, server, "publish")
    dialogue = model(c, server, "dialogue")
    assert (
        c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]}).status_code
        == 200
    )
    s = source(c)
    sub(c, b, s)
    hook(c, s)
    assert asyncio.run(w.process_one())
    assert incoming(w, b, "platform-1", "刚才推送了什么？")
    assert not incoming(w, b, "platform-1", "刚才推送了什么？")
    calls = []

    async def completion(config, messages, tools=None):
        calls.append((config["id"], messages, tools))
        return {"choices": [{"message": {"content": "这条推送概括了业务事件。"}}]}

    w.editor.completion = completion
    assert asyncio.run(w.chat_one())
    assert calls[0][0] == dialogue["id"]
    assert w.store.db.execute("SELECT state FROM inbound_messages").fetchone()[0] == "READY"
    assert asyncio.run(w.send_one())
    assert (
        w.store.db.execute("SELECT state FROM inbound_messages").fetchone()[0] == "ADAPTER_ACCEPTED"
    )
    assert w.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "READY"
    assert asyncio.run(w.send_one())
    assert len(Handler.receipts) == 2
    assert Handler.receipts[0]["text"] == "这条推送概括了业务事件。"
    assert incoming(w, b, "platform-2", "刚才推送的内容是什么？")
    assert asyncio.run(w.chat_one())
    prompt = calls[1][1][0]["content"]
    assert "业务通知" in prompt and "这条推送概括" in str(calls[1][1])
    assert c.get("/api/history?kind=inbound_messages").json()["total"] == 2
    assert publishing["id"] != dialogue["id"]


def test_conversation_scopes_bot_and_group_user(wb, server):
    c, w = wb
    a, b = bot(c, server), bot(c, server)
    assert incoming(w, a, "same-id", "私聊 A")
    assert incoming(w, b, "same-id", "私聊 B")
    row_a = w.store.db.execute(
        "SELECT * FROM inbound_messages WHERE bot_id=?", (a["id"],)
    ).fetchone()
    row_b = w.store.db.execute(
        "SELECT * FROM inbound_messages WHERE bot_id=?", (b["id"],)
    ).fetchone()
    assert row_a["conversation_id"] != row_b["conversation_id"]
    config = w.store.open(w.store.get("bot", a["id"])["secret"])
    for user in ("u1", "u2"):
        w.accept_inbound(
            w.store.get("bot", a["id"]),
            config,
            {"id": user, "target": "endpoint", "sender": user, "group": True, "text": "问题"},
        )
    ids = {
        r[0]
        for r in w.store.db.execute(
            "SELECT conversation_id FROM inbound_messages WHERE bot_id=?", (a["id"],)
        )
    }
    assert len(ids) == 3


def test_restart_does_not_resend_unknown_reply(wb, server):
    c, w = wb
    b = bot(c, server)
    incoming(w, b, "first", "问题")
    w.store.db.execute("UPDATE inbound_messages SET state='SENDING',response='未确认的回答'")
    w.store.recover()
    assert w.store.db.execute("SELECT state FROM inbound_messages").fetchone()[0] == "UNKNOWN"
    assert not asyncio.run(w.send_one())
    assert not Handler.receipts
    incoming(w, b, "second", "新问题")
    row = w.store.db.execute(
        "SELECT state FROM inbound_messages ORDER BY created DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "QUEUED"


def test_search_tools_only_open_current_result_and_respect_workspace_permission(wb, server):
    c, w = wb
    b = bot(c, server)
    dialogue = model(c, server, "dialogue")
    c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]})
    workspace = c.get("/api/workspaces/" + b["workspace_id"] + "/settings").json()
    post(
        c,
        "workspaces/" + workspace["id"] + "/settings",
        {
            "revision": workspace["revision"],
            "name": workspace["name"],
            "conversation_profile": {"prompt": "引用原文", "web_search_enabled": True},
        },
    )
    w.save_search_preferences(
        {
            "revision": 0,
            "provider": "searxng",
            "base_url": "https://search.example.com",
            "enabled": True,
        }
    )
    fetched = []

    async def request(method, url, **_kwargs):
        fetched.append(url)
        if "/search?" in url:
            return (
                json.dumps(
                    {
                        "results": [
                            {"title": "来源", "url": "https://example.com/a", "content": "线索"},
                            {"title": "内部", "url": "http://10.0.0.1/key", "content": "秘密"},
                        ]
                    }
                ).encode(),
                url,
                {},
            )
        return "<html><body><h1>公开内容</h1></body></html>".encode(), url, {}

    w.net.request = request
    calls = []

    def tool(name, args, ident):
        return {"id": ident, "function": {"name": name, "arguments": json.dumps(args)}}

    async def completion(_config, messages, tools=None):
        calls.append((messages, tools))
        if len(calls) == 1:
            return {
                "choices": [
                    {"message": {"tool_calls": [tool("search_web", {"query": "最新"}, "1")]}}
                ]
            }
        if len(calls) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                tool(
                                    "fetch_page", {"url": "https://other.example.com/secret"}, "2"
                                ),
                                tool("fetch_page", {"url": "https://example.com/a"}, "3"),
                            ]
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "公开内容，来源：https://example.com/a"}}]}

    w.editor.completion = completion
    incoming(w, b, "request", "现在有哪些更新？")
    assert asyncio.run(w.chat_one())
    assert c.get("/api/workspaces/" + workspace["id"] + "/settings").json()["conversation_profile"] == {
        "web_search_enabled": True
    }
    assert "引用原文" not in calls[0][0][0]["content"]
    trace = json.loads(w.store.db.execute("SELECT tool_trace FROM inbound_messages").fetchone()[0])
    assert [r["name"] for r in trace] == ["search_web", "fetch_page", "fetch_page"]
    assert trace[1]["error"] and trace[2]["host"] == "example.com"
    assert all("10.0.0.1" not in url and "other.example.com" not in url for url in fetched)
    assert calls[0][1] is not None
    assert not Handler.receipts


def test_missing_conversation_model_fails_closed(wb, server):
    c, w = wb
    b = bot(c, server)
    incoming(w, b, "first", "你好")
    assert asyncio.run(w.chat_one())
    row = w.store.db.execute("SELECT state,error FROM inbound_messages").fetchone()
    assert row["state"] == "FAILED" and "对话模型" in row["error"]
    assert not asyncio.run(w.send_one())
    assert not Handler.receipts


def test_receiver_commits_cursor_with_inbound_and_rolls_back_on_failure(wb, server, monkeypatch):
    c, w = wb
    b = bot(c, server)
    saved = w.store.get("bot", b["id"])
    saved["type"] = "telegram"
    w.store.put("bot", saved)
    original = w.store.open(saved["secret"])
    original["offset"] = 4
    saved["secret"] = w.store.seal(original)
    w.store.put("bot", saved)

    async def receive(_kind, config, with_messages=False):
        config["offset"] = 5
        w.running = False
        return config, [{"id": "4", "target": "endpoint", "sender": "u", "text": "问题"}]

    w.channels.receive = receive

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("backend.app.service.asyncio.sleep", no_delay)
    asyncio.run(w.receiver(b["id"]))
    assert w.store.open(w.store.get("bot", b["id"])["secret"])["offset"] == 5
    assert w.store.db.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 1

    current = w.store.get("bot", b["id"])
    config = w.store.open(current["secret"])
    config["offset"] = 4
    current["secret"] = w.store.seal(config)
    w.store.put("bot", current)
    w.running = True

    def fail(*_args):
        raise RuntimeError("write failed")

    w.accept_inbound = fail
    asyncio.run(w.receiver(b["id"]))
    assert w.store.open(w.store.get("bot", b["id"])["secret"])["offset"] == 4
    assert w.store.db.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 1


def test_binding_change_cancels_unsent_reply_and_preserves_history(wb, server):
    c, w = wb
    b = bot(c, server)
    incoming(w, b, "before-change", "问题")
    w.store.db.execute("UPDATE inbound_messages SET state='READY',response='不能发到新目标'")
    post(c, "bots/" + b["id"] + "/update", {"revision": b["revision"], "target_ids": []})
    row = w.store.db.execute("SELECT state,text FROM inbound_messages").fetchone()
    assert row["state"] == "CANCELLED" and row["text"] == "问题"
    assert not asyncio.run(w.reply_one())
    assert not Handler.receipts


def test_search_permission_is_per_workspace_and_api_is_authenticated(wb, server):
    c, w = wb
    b = bot(c, server)
    dialogue = model(c, server, "dialogue")
    c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]})
    w.save_search_preferences(
        {
            "revision": 0,
            "provider": "searxng",
            "base_url": "https://search.example.com",
            "enabled": True,
        }
    )
    calls = []

    async def completion(_config, _messages, tools=None):
        calls.append(tools)
        return {"choices": [{"message": {"content": "尚无法核实最新情况。"}}]}

    w.editor.completion = completion
    incoming(w, b, "no-search", "最新消息？")
    assert asyncio.run(w.chat_one())
    assert [[tool["function"]["name"] for tool in tools] for tools in calls] == [["search_pushes"]]
    assert c.get("/api/search").json()["has_key"] is False
    assert c.post("/api/search", json={"revision": 1}).status_code == 200


def test_group_threads_and_sender_are_isolated(wb, server):
    c, w = wb
    b = bot(c, server)
    config = w.store.open(w.store.get("bot", b["id"])["secret"])
    for sender, thread in (("alice", "1"), ("alice", "2"), ("bob", "1")):
        w.accept_inbound(
            w.store.get("bot", b["id"]),
            config,
            {
                "id": sender + thread,
                "target": "endpoint",
                "sender": sender,
                "thread": thread,
                "group": True,
                "text": "问题",
            },
        )
    assert (
        w.store.db.execute(
            "SELECT COUNT(DISTINCT conversation_id) FROM inbound_messages"
        ).fetchone()[0]
        == 3
    )


def test_publication_model_does_not_block_conversation_model(wb, server):
    c, w = wb
    b = bot(c, server)
    publishing = model(c, server, "publish")
    dialogue = model(c, server, "dialogue")
    c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]})
    s = source(c)
    sub(c, b, s, mode="agent", model=publishing["id"])
    hook(c, s)
    incoming(w, b, "while-publishing", "请回答")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_publish(_workspace, _snapshot, _paths):
            started.set()
            await release.wait()
            return "发布内容已整理。"

        async def reply(config, _messages, _tools):
            assert config["id"] == dialogue["id"]
            return {"choices": [{"message": {"content": "对话没有等待整理完成。"}}]}

        w.editor.run = slow_publish
        w.editor.completion = reply
        publication = asyncio.create_task(w.process_one())
        await started.wait()
        assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "RUNNING"
        await w.chat_one()
        assert w.store.db.execute("SELECT state FROM inbound_messages").fetchone()[0] == "READY"
        release.set()
        await publication
        assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "RENDERED"

    asyncio.run(run())


def test_different_conversations_run_together_with_bounded_model_slots(wb, server):
    c, w = wb
    a, b = bot(c, server), bot(c, server)
    dialogue = model(c, server, "dialogue")
    publishing = model(c, server, "publishing")
    c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]})
    s = source(c)
    sub(c, a, s, mode="agent", model=publishing["id"])
    hook(c, s)
    incoming(w, a, "question-a", "A 的问题")
    incoming(w, b, "question-b", "B 的问题")

    async def run():
        both_started = asyncio.Event()
        release = asyncio.Event()
        publication_started = asyncio.Event()
        active = 0
        peak = 0

        async def completion(_config, _messages, _tools):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await release.wait()
            active -= 1
            return {"choices": [{"message": {"content": "独立回答"}}]}

        async def publish(_workspace, _snapshot, _paths):
            publication_started.set()
            return "发布完成"

        w.editor.completion = completion
        w.editor.run = publish
        chats = [asyncio.create_task(w.chat_one()) for _ in range(2)]
        await asyncio.wait_for(both_started.wait(), 1)
        assert peak == 2
        assert [r[0] for r in w.store.db.execute("SELECT state FROM inbound_messages")] == [
            "RUNNING",
            "RUNNING",
        ]
        publication = asyncio.create_task(w.process_one())
        for _ in range(20):
            if w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert w.store.db.execute("SELECT state FROM jobs").fetchone()[0] == "RUNNING"
        assert not publication_started.is_set()
        release.set()
        assert await asyncio.gather(*chats) == [True, True]
        assert await publication
        assert publication_started.is_set()
        assert (
            w.store.db.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE state='READY'"
            ).fetchone()[0]
            == 2
        )

    asyncio.run(run())


def test_same_conversation_keeps_arrival_order_even_with_tied_timestamps(wb, server):
    c, w = wb
    b = bot(c, server)
    dialogue = model(c, server, "dialogue")
    c.post("/api/models/roles", json={"conversation_model_id": dialogue["id"]})
    incoming(w, b, "first-question", "第一问")
    incoming(w, b, "second-question", "第二问")
    w.store.db.execute("UPDATE inbound_messages SET created=123")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        prompts = []

        async def completion(_config, messages, _tools):
            prompts.append(messages)
            if len(prompts) == 1:
                started.set()
                await release.wait()
            return {"choices": [{"message": {"content": f"第{len(prompts)}答"}}]}

        w.editor.completion = completion
        first = asyncio.create_task(w.chat_one())
        await asyncio.wait_for(started.wait(), 1)
        assert not await w.chat_one()
        release.set()
        assert await first
        assert not await w.chat_one()
        assert await w.send_one()
        assert await w.chat_one()
        assert "第1答" in str(prompts[1])
        assert [
            r[0] for r in w.store.db.execute("SELECT state FROM inbound_messages ORDER BY rowid")
        ] == [
            "ADAPTER_ACCEPTED",
            "READY",
        ]

    asyncio.run(run())


def test_history_search_is_scoped_to_bot_target_and_workspace(wb, server):
    c, w = wb
    a, b = bot(c, server), bot(c, server)
    disabled = post(c, "bots/" + b["id"] + "/update", {"revision": b["revision"], "enabled": False})
    s = source(c)
    sub(c, a, s)
    hook(c, s)
    asyncio.run(w.process_one())
    asyncio.run(w.deliver_one())
    assert len(Handler.receipts) == 1
    post(c, "bots/" + b["id"] + "/update", {"revision": disabled["revision"], "enabled": True})
    incoming(w, a, "question-a", "业务通知")
    incoming(w, b, "question-b", "业务通知")
    rows = {
        row["bot_id"]: dict(row) for row in w.store.db.execute("SELECT * FROM inbound_messages")
    }
    workspace = w.store.get("workspace", a["workspace_id"])
    assert len(w.search_pushes(rows[a["id"]], workspace, "业务通知")) == 1
    assert w.search_pushes(rows[b["id"]], workspace, "业务通知") == []
