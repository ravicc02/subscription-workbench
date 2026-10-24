"""Official QQ and Feishu socket sessions; platform events stay outside Agent prompts."""

import asyncio
import hashlib
import json
import re

from .channels import ContextRequired, PlatformRejected


def qq_event(message, scene, *, addressed=True):
    """Normalize botpy's public message events without retaining SDK objects."""
    author = getattr(message, "author", None)
    if not author or getattr(author, "bot", False):
        return None
    if scene == "group":
        target_id = getattr(message, "group_openid", None)
        sender = getattr(author, "member_openid", None)
    elif scene == "c2c":
        target_id = getattr(author, "user_openid", None)
        sender = target_id
    elif scene == "channel":
        target_id = getattr(message, "channel_id", None)
        sender = getattr(author, "id", None)
    else:
        target_id = getattr(message, "guild_id", None)
        sender = getattr(author, "id", None)
    if not target_id or not sender:
        return None
    content = str(getattr(message, "content", "") or "")
    for mention in getattr(message, "mentions", None) or []:
        if getattr(mention, "is_you", False) and getattr(mention, "id", None):
            ident = re.escape(str(mention.id))
            content = re.sub(rf"<@!?{ident}>|<qqbot-at-user\s+id=\"{ident}\"\s*/>", "", content)
    if scene == "group" and addressed:
        content = re.sub(r'<qqbot-at-user\s+id="[^"]+"\s*/>', "", content, count=1)
    attachments = []
    if addressed:
        for attachment in getattr(message, "attachments", None) or []:
            mime = str(getattr(attachment, "content_type", "") or "").lower()
            media_kind = next((key for key in ("image", "video", "audio") if mime.startswith(key + "/")), None)
            url = getattr(attachment, "url", None)
            if media_kind and isinstance(url, str) and len(url) <= 2048:
                attachments.append({"type": "voice" if media_kind == "audio" else media_kind,
                                    "url": url})
    value = {
        "id": str(getattr(message, "id", "") or ""),
        "target": scene + ":" + str(target_id),
        "name": str(getattr(message, "group_name", "") or target_id),
        "sender": str(sender),
        "group": scene in ("group", "channel"),
        "text": content.strip() if addressed else "",
        "attachments": attachments[:3],
    }
    return value


def feishu_event(event, bot_open_id):
    payload = getattr(event, "event", None)
    message = getattr(payload, "message", None)
    sender = getattr(payload, "sender", None)
    if not message or not sender or getattr(sender, "sender_type", "user") != "user":
        return None
    target = str(getattr(message, "chat_id", "") or "")
    identity = str(getattr(message, "message_id", "") or "")
    user = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "")
    if not target or not identity or not user or user == bot_open_id:
        return None
    group = getattr(message, "chat_type", "") == "group"
    mentions = getattr(message, "mentions", None) or []
    addressed = not group or any(
        str(getattr(getattr(m, "id", None), "open_id", "")) == bot_open_id for m in mentions
    )
    text = ""
    attachments = []
    if addressed:
        try:
            content = json.loads(getattr(message, "content", "") or "{}")
            content = content if isinstance(content, dict) else {}
        except (TypeError, ValueError):
            content = {}
        if getattr(message, "message_type", "") == "text":
            text = str(content.get("text") or "")
        media_kind, key_name = {
            "image": ("image", "image_key"),
            "audio": ("voice", "file_key"),
            "media": ("video", "file_key"),
        }.get(getattr(message, "message_type", ""), (None, None))
        if media_kind and isinstance(content.get(key_name), str):
            attachments.append({"type": media_kind, "message_id": identity,
                                "file_key": content[key_name]})
        for mention in mentions:
            if str(getattr(getattr(mention, "id", None), "open_id", "")) == bot_open_id:
                key = getattr(mention, "key", "")
                if key:
                    text = text.replace(key, "")
    return {
        "id": identity,
        "target": target,
        "name": target,
        "sender": user,
        "group": group,
        "text": text.strip(),
        "attachments": attachments,
    }


class QQSocket:
    def __init__(self, config, on_message, on_ready):
        self.config = config
        self.on_message = on_message
        self.on_ready = on_ready
        self.client = None
        self.connected = False

    async def run(self):
        import botpy
        from botpy.connection import ConnectionState

        if not hasattr(ConnectionState, "parse_group_message_create"):

            def parse_group_message_create(state, payload):
                message = botpy.message.GroupMessage(
                    state.api, payload.get("id"), payload.get("d", {})
                )
                state._dispatch("group_message_create", message)

            ConnectionState.parse_group_message_create = parse_group_message_create

        owner = self

        class Client(botpy.Client):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.active_sockets = set()

            async def bot_connect(self, session):
                ws = botpy.gateway.BotWebSocket(session, self._connection)
                self.active_sockets.add(ws)
                try:
                    await ws.ws_connect()
                finally:
                    self.active_sockets.discard(ws)

            async def on_ready(self):
                owner.connected = True
                owner.on_ready()

            async def on_group_at_message_create(self, message):
                owner.on_message(qq_event(message, "group"))

            async def on_group_message_create(self, message):
                owner.on_message(qq_event(message, "group", addressed=False))

            async def on_c2c_message_create(self, message):
                owner.on_message(qq_event(message, "c2c"))

            async def on_at_message_create(self, message):
                owner.on_message(qq_event(message, "channel"))

            async def on_direct_message_create(self, message):
                owner.on_message(qq_event(message, "dm"))

        self.client = Client(
            intents=botpy.Intents(
                public_messages=True, public_guild_messages=True, direct_message=True
            ),
            bot_log=False,
            timeout=20,
        )
        try:
            await self.client.start(appid=self.config["app_id"], secret=self.config["app_secret"])
            raise ConnectionError("QQ WebSocket stopped")
        finally:
            self.connected = False
            for ws in list(self.client.active_sockets):
                ws._can_reconnect = False
                if getattr(ws, "_conn", None) and not ws._conn.closed:
                    await ws._conn.close()
            await self.client.close()

    async def send(self, target, text, delivery_id, reply_to=None):
        if not reply_to:
            raise PlatformRejected("QQ 官方接口不支持主动推送，仅可回复有效的入站消息")
        if not self.connected or not self.client or not self.client.api:
            raise ContextRequired("QQ 长连接尚未就绪")
        scene, _, recipient = target.partition(":")
        if not recipient or scene not in ("group", "c2c", "channel", "dm"):
            raise PlatformRejected("QQ 接收目标类型无效")
        msg_id = reply_to or None
        sequence = 1 + int(hashlib.sha256(delivery_id.encode()).hexdigest()[:6], 16) % 10000
        api = self.client.api
        try:
            if scene == "group":
                result = await api.post_group_message(
                    group_openid=recipient, content=text, msg_id=msg_id, msg_seq=sequence
                )
            elif scene == "c2c":
                result = await api.post_c2c_message(
                    openid=recipient, content=text, msg_id=msg_id, msg_seq=sequence
                )
            elif scene == "channel":
                result = await api.post_message(channel_id=recipient, content=text, msg_id=msg_id)
            else:
                result = await api.post_dms(guild_id=recipient, content=text, msg_id=msg_id)
        except Exception as exc:
            import botpy.errors as errors

            rejected = (
                errors.AuthenticationFailedError,
                errors.ForbiddenError,
                errors.MethodNotAllowedError,
                errors.NotFoundError,
                errors.SequenceNumberError,
            )
            if isinstance(exc, rejected):
                raise PlatformRejected(
                    "QQ 平台拒绝了消息提交，请检查权限、主动推送额度与目标"
                ) from exc
            raise
        message_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        if not message_id:
            raise RuntimeError("QQ 发送结果没有可确认的消息 ID")
        return {"accepted": True, "message_id": str(message_id)}


class FeishuSocket:
    def __init__(self, config, on_message, on_ready):
        self.config = config
        self.on_message = on_message
        self.on_ready = on_ready
        self.client = None

    async def run(self):
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(
                lambda event: self.on_message(feishu_event(event, self.config["bot_open_id"]))
            )
            .build()
        )
        self.client = lark.ws.Client(
            app_id=self.config["app_id"],
            app_secret=self.config["app_secret"],
            event_handler=handler,
            auto_reconnect=False,
            log_level=lark.LogLevel.ERROR,
        )
        try:
            await self.client._connect()
            if self.client._conn is None:
                raise ConnectionError("飞书长连接未建立")
            self.on_ready()
            while self.client._conn is not None:
                await asyncio.sleep(1)
            raise ConnectionError("飞书长连接已断开")
        finally:
            await self.client._disconnect()


class PlatformSockets:
    def __init__(self):
        self.sessions = {}

    async def run(self, bot_id, kind, config, on_message, on_ready):
        session = (QQSocket if kind == "qq" else FeishuSocket)(config, on_message, on_ready)
        self.sessions[bot_id] = session
        try:
            await session.run()
        finally:
            if self.sessions.get(bot_id) is session:
                self.sessions.pop(bot_id, None)

    async def send_qq(self, bot_id, target, text, delivery_id, reply_to=None):
        session = self.sessions.get(bot_id)
        if not isinstance(session, QQSocket):
            raise ContextRequired("QQ 长连接尚未建立")
        return await session.send(target, text, delivery_id, reply_to)
