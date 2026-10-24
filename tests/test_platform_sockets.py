"""QQ and Feishu protocol contracts without touching external bot accounts."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from test_workbench import hook, post, source, sub

from backend.app.channels import Channels, ContextRequired, PlatformRejected
from backend.app.platform_sockets import FeishuSocket, QQSocket, feishu_event, qq_event

pytest_plugins = ["test_workbench"]


class PlatformNetwork:
    def __init__(self):
        self.calls = []
        self.fail_send = False

    async def json(self, method, url, payload=None, headers=None):
        self.calls.append((method, url, payload, headers))
        if url.endswith("/getAppAccessToken"):
            return {"access_token": "temporary-token", "expires_in": 7200}
        if url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "private-token", "expire": 7200}
        if url.endswith("/bot/v3/info"):
            return {"code": 0, "bot": {"open_id": "ou_bot", "app_name": "Feishu bot"}}
        if "im/v1/messages?" in url:
            if self.fail_send:
                return {"code": 230001, "msg": "forbidden"}
            return {"code": 0, "data": {"message_id": "om_sent"}}
        raise AssertionError(url)


def qq_message(scene="group", content="@机器人 你好"):
    return NS(
        id="qq-msg-1",
        content=content,
        group_openid="g1",
        channel_id="ch1",
        guild_id="guild1",
        group_name="测试群",
        mentions=[],
        author=NS(member_openid="m1", user_openid="u1", id="m1"),
    )


def lark_message(*, group=False, mentioned=False):
    mention = NS(key="@_user_1", id=NS(open_id="ou_bot"))
    return NS(
        event=NS(
            message=NS(
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group" if group else "p2p",
                message_type="text",
                content=json.dumps({"text": "@_user_1 你好" if mentioned else "你好"}),
                mentions=[mention] if mentioned else [],
            ),
            sender=NS(sender_type="user", sender_id=NS(open_id="ou_user")),
        )
    )


def test_socket_message_normalization_only_replies_to_addressed_groups():
    group = qq_event(qq_message(content='<qqbot-at-user id="bot" /> 你好'), "group")
    assert group["target"] == "group:g1" and group["text"] == "你好"
    assert qq_event(qq_message(), "group", addressed=False)["text"] == ""
    assert qq_event(qq_message(), "c2c")["target"] == "c2c:u1"
    assert qq_event(qq_message(), "channel")["target"] == "channel:ch1"
    assert qq_event(qq_message(), "dm")["target"] == "dm:guild1"
    assert feishu_event(lark_message(group=True), "ou_bot")["text"] == ""
    assert feishu_event(lark_message(group=True, mentioned=True), "ou_bot")["text"] == "你好"
    assert feishu_event(lark_message(), "ou_bot")["text"] == "你好"


def test_platform_preflight_feishu_ack_and_secret_validation():
    net = PlatformNetwork()
    channels = Channels(net)

    async def check():
        qq, qq_status, _ = await channels.begin(
            "qq", {"app_id": "123456789", "app_secret": "secret-123"}
        )
        assert qq_status == "WAITING_TARGET" and qq["targets"] == []
        feishu, status, _ = await channels.begin(
            "feishu", {"app_id": "cli_12345678", "app_secret": "secret-456"}
        )
        assert status == "WAITING_TARGET" and feishu["bot_open_id"] == "ou_bot"
        feishu["targets"] = [{"id": "oc_1", "selected": True}]
        receipt = await channels.send("feishu", feishu, "oc_1", "中文消息", "delivery-1")
        assert receipt == {"accepted": True, "message_id": "om_sent"}
        _, url, payload, headers = net.calls[-1]
        assert url.endswith("receive_id_type=chat_id")
        assert json.loads(payload["content"])["text"] == "中文消息"
        assert headers["Authorization"] == "Bearer private-token"
        net.fail_send = True
        with pytest.raises(PlatformRejected, match="飞书发送被拒绝"):
            await channels.send("feishu", feishu, "oc_1", "again", "delivery-2")
        with pytest.raises(PlatformRejected, match="目标已变更"):
            await channels.send("feishu", feishu, "oc_other", "again", "delivery-2")

    asyncio.run(check())
    assert sum("tenant_access_token" in item[1] for item in net.calls) == 1
    with pytest.raises(ValueError):
        Channels.draft_config("qq", {"app_id": "no", "app_secret": "secret-123"})


def test_qq_send_routes_replies_and_rejects_proactive_messages():
    sent = []

    class API:
        async def post_group_message(self, **kwargs):
            sent.append(("group", kwargs))
            return NS(id="group-sent")

        async def post_c2c_message(self, **kwargs):
            sent.append(("c2c", kwargs))
            return {"id": "c2c-sent"}

        async def post_message(self, **kwargs):
            sent.append(("channel", kwargs))
            return NS(id="channel-sent")

        async def post_dms(self, **kwargs):
            sent.append(("dm", kwargs))
            return NS(id="dm-sent")

    async def check():
        session = QQSocket({}, lambda _: None, lambda: None)
        with pytest.raises(ContextRequired):
            await session.send("group:g1", "hello", "delivery-0", "qq-original")
        session.client = NS(api=API())
        session.connected = True
        assert (await session.send("group:g1", "reply", "delivery-1", "qq-original"))["accepted"]
        await session.send("c2c:u1", "reply", "delivery-2", "qq-original-2")
        await session.send("channel:ch1", "reply", "delivery-3", "qq-original-3")
        await session.send("dm:guild1", "reply", "delivery-4", "qq-original-4")
        with pytest.raises(PlatformRejected, match="不支持主动推送"):
            await session.send("group:g1", "subscription", "delivery-5")

    asyncio.run(check())
    assert sent[0][1]["msg_id"] == "qq-original"
    assert sent[1][1]["msg_id"] == "qq-original-2"
    assert [row[0] for row in sent] == ["group", "c2c", "channel", "dm"]


def test_qq_conversation_target_does_not_receive_subscription_push(wb):
    client, workbench = wb
    workbench.channels.net = PlatformNetwork()
    qq = post(
        client,
        "bots",
        {
            "name": "QQ 仅被动回复",
            "type": "qq",
            "workspace_mode": "shared",
            "config": {"app_id": "123456789", "app_secret": "secret-123"},
        },
    )
    workbench.socket_message(
        qq["id"],
        qq["binding_revision"],
        {"id": "qq-inbound", "target": "c2c:user1", "sender": "user1", "text": "你好"},
    )
    feed = source(client)
    sub(client, qq, feed)
    assert hook(client, feed)["new_events"] == 1
    assert workbench.store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert client.post("/api/bots/" + qq["id"] + "/test", json={}).status_code == 400

    feishu = post(
        client,
        "bots",
        {
            "name": "飞书订阅推送",
            "type": "feishu",
            "workspace_mode": "shared",
            "config": {"app_id": "cli_12345678", "app_secret": "secret-456"},
        },
    )
    assert feishu["workspace_id"] == qq["workspace_id"]
    workbench.socket_message(
        feishu["id"],
        feishu["binding_revision"],
        {"id": "om_target", "target": "oc_1", "sender": "ou_user", "text": "你好"},
    )
    assert hook(client, feed, "event-2")["new_events"] == 1
    recipients = workbench.store.db.execute("SELECT bot_id FROM deliveries").fetchall()
    assert [row[0] for row in recipients] == [feishu["id"]]
    assert asyncio.run(workbench.process_one())
    workbench.store.db.execute(
        "UPDATE deliveries SET bot_id=?,target=?,binding_revision=?",
        (qq["id"], "c2c:user1", qq["binding_revision"]),
    )
    assert asyncio.run(workbench.deliver_one())
    assert workbench.store.db.execute("SELECT state FROM deliveries").fetchone()[0] == "CANCELLED"


def test_socket_discovery_persistence_dedupe_and_old_binding_rejection(wb):
    client, workbench = wb
    net = PlatformNetwork()
    workbench.channels.net = net
    bot = client.post(
        "/api/bots",
        json={
            "name": "飞书接入",
            "type": "feishu",
            "config": {"app_id": "cli_12345678", "app_secret": "secret-456"},
        },
    ).json()
    assert bot["status"] == "WAITING_TARGET" and bot["workspace_path"]
    assert bot["has_credentials"] and "secret-456" not in json.dumps(bot)
    workbench.socket_state(bot["id"], bot["binding_revision"])
    message = {"id": "om_1", "target": "oc_1", "sender": "ou_user", "text": "你好"}
    workbench.socket_message(bot["id"], bot["binding_revision"], message)
    workbench.socket_message(bot["id"], bot["binding_revision"], message)
    assert workbench.store.db.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 1
    updated = workbench.public_bot(workbench.store.get("bot", bot["id"]))
    assert updated["status"] == "CONNECTED" and updated["targets"][0]["selected"]
    client.post(
        "/api/bots/" + bot["id"] + "/update",
        json={"revision": updated["revision"], "target_ids": []},
    )
    workbench.socket_message(
        bot["id"],
        bot["binding_revision"],
        {**message, "id": "om_2", "text": "旧连接不应进入新配置"},
    )
    assert workbench.store.db.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 1


def test_feishu_socket_starts_receives_and_closes(monkeypatch):
    import lark_oapi as lark

    sockets = []
    seen = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.handler = kwargs["event_handler"]
            self._conn = None
            sockets.append(self)

        async def _connect(self):
            self._conn = object()

        async def _disconnect(self):
            self._conn = None

    monkeypatch.setattr(lark.ws, "Client", FakeClient)

    async def check():
        session = FeishuSocket(
            {"app_id": "cli_12345678", "app_secret": "private", "bot_open_id": "ou_bot"},
            seen.append,
            lambda: seen.append("READY"),
        )
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.02)
        assert seen == ["READY"] and sockets[0]._conn is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sockets[0]._conn is None

    asyncio.run(check())


def test_qq_inbound_agent_reply_uses_original_message_and_persists_receipt(wb, server):
    client, workbench = wb
    workbench.channels.net = PlatformNetwork()
    bot = post(
        client,
        "bots",
        {
            "name": "QQ 双向",
            "type": "qq",
            "config": {"app_id": "123456789", "app_secret": "secret-123"},
        },
    )
    model = post(
        client,
        "models",
        {
            "name": "对话",
            "model": "test-model",
            "base_url": server,
            "api_key": "test-key",
        },
    )
    client.post("/api/models/roles", json={"conversation_model_id": model["id"]})
    sent = []

    class API:
        async def post_c2c_message(self, **kwargs):
            sent.append(kwargs)
            return NS(id="qq-accepted")

    socket = QQSocket({}, lambda _: None, lambda: None)
    socket.client, socket.connected = NS(api=API()), True
    workbench.sockets.sessions[bot["id"]] = socket
    workbench.socket_message(
        bot["id"],
        bot["binding_revision"],
        {"id": "qq-msg-1", "target": "c2c:user1", "sender": "user1", "text": "刚才推送了什么？"},
    )

    async def completion(config, messages, tools=None):
        assert config["id"] == model["id"]
        return {"choices": [{"message": {"content": "这是之前的更新。"}}]}

    workbench.editor.completion = completion
    assert asyncio.run(workbench.chat_one())
    assert asyncio.run(workbench.reply_one())
    row = workbench.store.db.execute("SELECT state,receipt FROM inbound_messages").fetchone()
    assert row["state"] == "ADAPTER_ACCEPTED"
    assert json.loads(row["receipt"])["message_id"] == "qq-accepted"
    assert sent[0]["openid"] == "user1" and sent[0]["msg_id"] == "qq-msg-1"
