"""Real platform adapters. Credentials never enter public bot responses."""

import base64
import hashlib
import hmac
import io
import re
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

import qrcode

WEIXIN_BASE = "https://ilinkai.weixin.qq.com"
CATALOG = [
    {
        "id": "weixin",
        "name": "个人微信",
        "auth": "qr",
        "implemented": True,
        "formats": ["text"],
        "fields": [],
        "limit": 1600,
    },
    {
        "id": "telegram",
        "name": "Telegram",
        "auth": "token",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "token", "label": "Bot Token", "secret": True, "required": True},
            {"name": "chat_id", "label": "接收会话 ID（可在收到消息后选择）"},
        ],
        "limit": 3500,
    },
    {
        "id": "webhook",
        "name": "HTTP Webhook",
        "auth": "config",
        "implemented": True,
        "formats": ["json"],
        "fields": [
            {"name": "url", "label": "接收地址", "required": True},
            {"name": "bearer", "label": "Bearer Token（可选）", "secret": True},
        ],
        "limit": 6000,
    },
    {
        "id": "discord",
        "name": "Discord",
        "auth": "token",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "token", "label": "Bot Token", "secret": True, "required": True},
            {"name": "channel_id", "label": "频道 ID", "required": True},
        ],
        "limit": 1800,
    },
    {
        "id": "slack",
        "name": "Slack",
        "auth": "token",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "token", "label": "Bot User OAuth Token", "secret": True, "required": True},
            {"name": "channel_id", "label": "频道 / 会话 ID", "required": True},
        ],
        "limit": 3500,
    },
    {
        "id": "onebot",
        "name": "OneBot v11",
        "auth": "config",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {
                "name": "url",
                "label": "HTTP API 基础地址",
                "required": True,
                "help": "本地地址需由服务端 WORKBENCH_ALLOWED_HOSTS 显式允许",
            },
            {"name": "access_token", "label": "Access Token（可选）", "secret": True},
            {
                "name": "message_type",
                "label": "接收类型",
                "required": True,
                "options": [
                    {"label": "群聊", "value": "group"},
                    {"label": "私聊", "value": "private"},
                ],
            },
            {"name": "target_id", "label": "群号 / 用户 ID", "required": True},
        ],
        "limit": 3000,
    },
    {
        "id": "feishu_group",
        "name": "飞书群自定义机器人",
        "auth": "config",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "url", "label": "群机器人 Webhook 地址", "required": True, "secret": True},
            {
                "name": "sign_secret",
                "label": "签名校验密钥（可选）",
                "secret": True,
                "help": "机器人已启用签名校验时填写",
            },
        ],
        "limit": 3000,
    },
    {
        "id": "wecom_group",
        "name": "企业微信群机器人",
        "auth": "config",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "url", "label": "群机器人 Webhook 地址", "required": True, "secret": True},
        ],
        "limit": 3000,
        "byte_limit": 2000,
    },
    {
        "id": "dingtalk_group",
        "name": "钉钉自定义群机器人",
        "auth": "config",
        "implemented": True,
        "formats": ["text"],
        "fields": [
            {"name": "url", "label": "群机器人 Webhook 地址", "required": True, "secret": True},
            {"name": "sign_secret", "label": "加签密钥（可选）", "secret": True},
            {
                "name": "keyword",
                "label": "安全关键词（可选）",
                "help": "若机器人启用了自定义关键词，填入后会前置于每条消息",
            },
        ],
        "limit": 3000,
    },
    *[
        {"id": k, "name": v, "implemented": False, "auth": "planned"}
        for k, v in [
            ("qq", "QQ 官方机器人"),
            ("wecom", "企业微信"),
            ("wechat_official", "微信公众号"),
            ("feishu", "飞书"),
            ("dingtalk", "钉钉"),
            ("line", "LINE"),
            ("kook", "KOOK"),
            ("mattermost", "Mattermost"),
            ("misskey", "Misskey"),
            ("satori", "Satori"),
            ("matrix", "Matrix"),
            ("vocechat", "VoceChat"),
        ]
    ],
]


class ContextRequired(ValueError):
    pass


class PlatformRejected(ValueError):
    pass


class Channels:
    def __init__(self, network, proxy_config=None):
        self.net = network
        self.proxy_config = proxy_config

    @staticmethod
    def draft_config(kind, config):
        if not isinstance(config, dict):
            raise ValueError("接入配置必须是对象")
        allowed = {field["name"] for field in Channels.info(kind)["fields"]}
        result = {key: value for key, value in config.items() if key in allowed}
        if kind == "telegram":
            token = str(result.get("token") or "").strip()
            if not token or len(token) > 300 or any(c in token for c in "/?#\r\n "):
                raise ValueError("请在修改接入配置中填写有效的 Telegram Bot Token")
            result["token"] = token
        if kind in ("discord", "slack"):
            token = str(result.get("token") or "").strip()
            channel = str(result.get("channel_id") or "").strip()
            if not token or len(token) > 300 or any(c.isspace() for c in token):
                raise ValueError("请填写有效的 Bot Token")
            if kind == "discord" and not re.fullmatch(r"[0-9]{15,22}", channel):
                raise ValueError("Discord 频道 ID 应为数字")
            if kind == "slack" and not re.fullmatch(r"[CGD][A-Z0-9]{6,20}", channel):
                raise ValueError("Slack 频道 / 会话 ID 无效")
            result.update(token=token, channel_id=channel)
        if kind == "onebot":
            url = str(result.get("url") or "").strip().rstrip("/")
            target = str(result.get("target_id") or "").strip()
            if not url or urlsplit(url).query or urlsplit(url).fragment:
                raise ValueError("OneBot HTTP API 地址无效")
            if result.get("message_type") not in ("group", "private"):
                raise ValueError("请选择群聊或私聊")
            if not re.fullmatch(r"[0-9]{1,22}", target):
                raise ValueError("OneBot 接收目标 ID 应为数字")
            access_token = str(result.get("access_token") or "").strip()
            if len(access_token) > 300 or any(c.isspace() for c in access_token):
                raise ValueError("OneBot Access Token 无效")
            result.update(url=url, target_id=target, access_token=access_token)
        if kind == "feishu_group":
            url = str(result.get("url") or "").strip()
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in ("open.feishu.cn", "open.larksuite.com")
                or parsed.port not in (None, 443)
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]{16,100}", parsed.path)
            ):
                raise ValueError("请填写飞书官方群机器人 Webhook 地址")
            secret = str(result.get("sign_secret") or "").strip()
            if len(secret) > 256 or any(char in secret for char in "\r\n"):
                raise ValueError("飞书签名密钥无效")
            result.update(url=url, sign_secret=secret)
        if kind in ("wecom_group", "dingtalk_group"):
            url = str(result.get("url") or "").strip()
            parsed = urlsplit(url)
            host, path, parameter = (
                ("qyapi.weixin.qq.com", "/cgi-bin/webhook/send", "key")
                if kind == "wecom_group"
                else ("oapi.dingtalk.com", "/robot/send", "access_token")
            )
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                parsed.scheme != "https"
                or parsed.hostname != host
                or parsed.port not in (None, 443)
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.path != path
                or set(query) != {parameter}
                or len(query[parameter]) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", query[parameter][0])
            ):
                raise ValueError("请填写官方群机器人 Webhook 地址（不要包含临时签名参数）")
            result["url"] = url
            if kind == "dingtalk_group":
                secret = str(result.get("sign_secret") or "").strip()
                keyword = str(result.get("keyword") or "").strip()
                if len(secret) > 256 or any(c in secret for c in "\r\n"):
                    raise ValueError("钉钉加签密钥无效")
                if len(keyword) > 80 or any(c in keyword for c in "\r\n"):
                    raise ValueError("钉钉安全关键词无效")
                result.update(sign_secret=secret, keyword=keyword)
        return result

    async def telegram_request(self, config, method, endpoint, payload=None, **kwargs):
        proxy = self.proxy_config(config) if self.proxy_config else None
        if proxy is not None:
            kwargs["proxy"] = proxy
        return await self.net.json(
            method,
            "https://api.telegram.org/bot" + config["token"] + "/" + endpoint,
            payload,
            **kwargs,
        )

    @staticmethod
    def info(kind):
        row = next((c for c in CATALOG if c["id"] == kind and c["implemented"]), None)
        if not row:
            raise ValueError("This platform adapter is not implemented")
        return row

    @staticmethod
    def wx_headers(c):
        return {
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            **({"Authorization": "Bearer " + c["token"]} if c.get("token") else {}),
        }

    @staticmethod
    def wx_ok(v):
        if any(v.get(k) not in (None, 0, "0") for k in ("ret", "errcode")):
            raise PlatformRejected("微信接口返回失败；请检查登录状态或刷新会话上下文")
        return v

    async def begin(self, kind, config):
        self.info(kind)
        if kind == "weixin":
            data = await self.net.json(
                "GET",
                WEIXIN_BASE + "/ilink/bot/get_bot_qrcode?bot_type=3",
                headers=self.wx_headers({}),
            )
            code = data.get("qrcode")
            content = data.get("qrcode_img_content")
            if not isinstance(code, str) or not isinstance(content, str) or not code or not content:
                raise ValueError("微信接口未返回有效二维码")
            image = qrcode.make(content)
            b = io.BytesIO()
            image.save(b, format="PNG")
            return (
                {
                    "qrcode": code,
                    "qr_image": "data:image/png;base64," + base64.b64encode(b.getvalue()).decode(),
                    "qr_created": time.time(),
                    "base_url": WEIXIN_BASE,
                    "contexts": {},
                    "targets": [],
                },
                "WAITING_QR",
                {},
            )
        if kind == "telegram":
            token = str(config.get("token", "")).strip()
            if not token or len(token) > 300 or any(c in token for c in "/?#"):
                raise ValueError("请输入有效的 Telegram Bot Token")
            config = self.draft_config(kind, config)
            me = await self.telegram_request(config, "GET", "getMe")
            if not me.get("ok"):
                raise PlatformRejected("Telegram authentication rejected")
            hook = await self.telegram_request(config, "GET", "getWebhookInfo")
            if hook.get("result", {}).get("url"):
                raise ValueError("该 Bot 已配置 Webhook，请先处理旧接入以免冲突")
            c = {"token": token, "offset": 0, "targets": []}
            if config.get("chat_id"):
                chat = await self.telegram_request(
                    config, "GET", "getChat?" + urlencode({"chat_id": config["chat_id"]})
                )
                if not chat.get("ok"):
                    raise ValueError("Telegram 接收会话不可访问")
                c["targets"] = [
                    {
                        "id": str(chat["result"]["id"]),
                        "name": chat["result"].get("title")
                        or chat["result"].get("first_name")
                        or str(chat["result"]["id"]),
                        "selected": True,
                    }
                ]
            return (
                c,
                "CONNECTED" if c["targets"] else "WAITING_TARGET",
                {"account": me["result"].get("username", "Telegram")},
            )
        if kind == "discord":
            c = self.draft_config(kind, config)
            headers = {"Authorization": "Bot " + c["token"]}
            me = await self.net.json(
                "GET", "https://discord.com/api/v10/users/@me", headers=headers
            )
            if not me.get("bot") or not me.get("id"):
                raise PlatformRejected("Discord Bot 身份验证失败")
            channel = await self.net.json(
                "GET", "https://discord.com/api/v10/channels/" + c["channel_id"], headers=headers
            )
            if str(channel.get("id")) != c["channel_id"]:
                raise PlatformRejected("Discord 频道不可访问")
            return (
                {
                    "token": c["token"],
                    "channel_id": c["channel_id"],
                    "targets": [
                        {
                            "id": c["channel_id"],
                            "name": channel.get("name") or c["channel_id"],
                            "selected": True,
                        },
                    ],
                },
                "CONNECTED",
                {"account": me.get("username") or str(me["id"])},
            )
        if kind == "slack":
            c = self.draft_config(kind, config)
            me = await self.net.json(
                "POST",
                "https://slack.com/api/auth.test",
                headers={"Authorization": "Bearer " + c["token"]},
            )
            if not me.get("ok") or not me.get("bot_id"):
                raise PlatformRejected("Slack Bot Token 验证失败")
            return (
                {
                    "token": c["token"],
                    "channel_id": c["channel_id"],
                    "targets": [
                        {"id": c["channel_id"], "name": c["channel_id"], "selected": True},
                    ],
                },
                "CONNECTED",
                {"account": me.get("user") or me["bot_id"]},
            )
        if kind == "onebot":
            c = self.draft_config(kind, config)
            await __import__("asyncio").to_thread(self.net.resolve, c["url"])
            headers = {"Authorization": "Bearer " + c["access_token"]} if c["access_token"] else {}
            me = await self.net.json("GET", c["url"] + "/get_login_info", headers=headers)
            if (
                me.get("status") != "ok"
                or me.get("retcode") != 0
                or not me.get("data", {}).get("user_id")
            ):
                raise PlatformRejected("OneBot 服务未返回有效登录账号")
            target = c["message_type"] + ":" + c["target_id"]
            return (
                {
                    "url": c["url"],
                    "access_token": c["access_token"],
                    "message_type": c["message_type"],
                    "target_id": c["target_id"],
                    "targets": [
                        {"id": target, "name": target, "selected": True},
                    ],
                },
                "CONNECTED",
                {"account": str(me["data"]["user_id"])},
            )
        if kind == "feishu_group":
            c = self.draft_config(kind, config)
            await __import__("asyncio").to_thread(self.net.resolve, c["url"])
            return (
                {
                    "url": c["url"],
                    "sign_secret": c["sign_secret"],
                    "targets": [
                        {"id": "group", "name": "飞书群", "selected": True},
                    ],
                },
                "READY",
                {},
            )
        if kind in ("wecom_group", "dingtalk_group"):
            c = self.draft_config(kind, config)
            await __import__("asyncio").to_thread(self.net.resolve, c["url"])
            return (
                {**c, "targets": [{"id": "group", "name": "接入群", "selected": True}]},
                "READY",
                {},
            )
        url = str(config.get("url", "")).strip()
        await __import__("asyncio").to_thread(self.net.resolve, url)
        return (
            {
                "url": url,
                "bearer": str(config.get("bearer", "")),
                "targets": [{"id": "endpoint", "name": urlsplit(url).hostname, "selected": True}],
            },
            "READY",
            {},
        )

    async def qr_status(self, c):
        data = await self.net.json(
            "GET",
            c["base_url"] + "/ilink/bot/get_qrcode_status?" + urlencode({"qrcode": c["qrcode"]}),
            headers={**self.wx_headers(c), "iLink-App-ClientVersion": "1"},
            timeout=38,
        )
        status = data.get("status", "wait")
        if status == "confirmed":
            token = data.get("bot_token")
            base = data.get("baseurl") or WEIXIN_BASE
            host = urlsplit(base).hostname or ""
            if urlsplit(base).scheme != "https" or not (
                host == "weixin.qq.com" or host.endswith(".weixin.qq.com")
            ):
                raise ValueError("微信返回了非预期的认证服务地址")
            if not token:
                raise ValueError("微信登录未返回 Token")
            c.update(
                token=token,
                base_url=base.rstrip("/"),
                account_id=data.get("ilink_bot_id", ""),
                sync_buf="",
            )
            c.pop("qrcode", None)
            c.pop("qr_image", None)
            return c, "WAITING_TARGET", {"account": c["account_id"]}
        if status in ("expired", "cancel", "canceled", "denied"):
            return c, "QR_EXPIRED", {}
        return c, "WAITING_QR", {"qr_status": status}

    async def receive(self, kind, c):
        found = []
        if kind == "weixin":
            data = self.wx_ok(
                await self.net.json(
                    "POST",
                    c["base_url"] + "/ilink/bot/getupdates",
                    {
                        "get_updates_buf": c.get("sync_buf", ""),
                        "base_info": {"channel_version": "subscription-workbench/0.1"},
                    },
                    self.wx_headers(c),
                    timeout=40,
                )
            )
            c["sync_buf"] = data.get("get_updates_buf", c.get("sync_buf", ""))
            for msg in data.get("msgs", []):
                user = str(msg.get("from_user_id", ""))
                context = msg.get("context_token")
                if user and context:
                    c.setdefault("contexts", {})[user] = context
                    found.append({"id": user, "name": user})
        elif kind == "telegram":
            data = await self.telegram_request(
                c,
                "GET",
                "getUpdates?" + urlencode({"offset": c.get("offset", 0), "timeout": 20}),
                timeout=28,
            )
            if not data.get("ok"):
                raise PlatformRejected("Telegram update request rejected")
            for item in data.get("result", []):
                c["offset"] = max(c.get("offset", 0), item["update_id"] + 1)
                msg = (
                    item.get("message")
                    or item.get("channel_post")
                    or item.get("my_chat_member")
                    or {}
                )
                chat = msg.get("chat", {})
                if chat.get("id"):
                    found.append(
                        {
                            "id": str(chat["id"]),
                            "name": chat.get("title") or chat.get("first_name") or str(chat["id"]),
                        }
                    )
        targets = c.setdefault("targets", [])
        for t in found:
            if not any(x["id"] == t["id"] for x in targets):
                targets.append({**t, "selected": not targets})
        return c

    async def send(self, kind, c, target, text, delivery_id):
        if kind == "weixin":
            context = c.get("contexts", {}).get(target)
            if not context:
                raise ContextRequired("请向机器人发送一条消息，更新微信会话上下文")
            v = self.wx_ok(
                await self.net.json(
                    "POST",
                    c["base_url"] + "/ilink/bot/sendmessage",
                    {
                        "base_info": {"channel_version": "subscription-workbench/0.1"},
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": target,
                            "client_id": delivery_id,
                            "message_type": 2,
                            "message_state": 2,
                            "context_token": context,
                            "item_list": [{"type": 1, "text_item": {"text": text}}],
                        },
                    },
                    self.wx_headers(c),
                )
            )
            return {"accepted": True, "message_id": v.get("message_id")}
        if kind == "telegram":
            v = await self.telegram_request(
                c, "POST", "sendMessage", {"chat_id": target, "text": text}
            )
            if not v.get("ok"):
                raise PlatformRejected("Telegram send rejected")
            return {"accepted": True, "message_id": v["result"].get("message_id")}
        if kind == "discord":
            v = await self.net.json(
                "POST",
                "https://discord.com/api/v10/channels/" + target + "/messages",
                {
                    "content": text,
                    "nonce": hashlib.sha256(delivery_id.encode()).hexdigest()[:24],
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
                headers={"Authorization": "Bot " + c["token"]},
            )
            if not v.get("id"):
                raise PlatformRejected("Discord 未确认消息提交")
            return {"accepted": True, "message_id": str(v["id"])}
        if kind == "slack":
            v = await self.net.json(
                "POST",
                "https://slack.com/api/chat.postMessage",
                {
                    "channel": target,
                    "text": text,
                    "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, delivery_id)),
                },
                headers={"Authorization": "Bearer " + c["token"]},
            )
            if not v.get("ok") or not v.get("ts"):
                raise PlatformRejected("Slack 拒绝消息：" + str(v.get("error", "unknown"))[:80])
            return {"accepted": True, "message_id": v["ts"]}
        if kind == "onebot":
            if target not in {t["id"] for t in c["targets"]}:
                raise PlatformRejected("OneBot 接收目标已变更")
            message_type, target_id = target.split(":", 1)
            headers = {"Authorization": "Bearer " + c["access_token"]} if c["access_token"] else {}
            v = await self.net.json(
                "POST",
                c["url"] + "/send_msg",
                {
                    "message_type": message_type,
                    message_type + "_id": int(target_id),
                    "message": text,
                    "auto_escape": True,
                },
                headers=headers,
            )
            if v.get("status") != "ok" or v.get("retcode") != 0:
                raise PlatformRejected("OneBot 服务拒绝消息")
            return {"accepted": True, "message_id": v.get("data", {}).get("message_id")}
        if kind == "feishu_group":
            if target != "group":
                raise PlatformRejected("飞书群接收目标已变更")
            payload = {
                "msg_type": "text",
                "content": {"text": re.sub(r"<\s*at\b", "&lt;at", text, flags=re.IGNORECASE)},
            }
            if c.get("sign_secret"):
                stamp = str(int(time.time()))
                signature = hmac.new(
                    (stamp + "\n" + c["sign_secret"]).encode(), digestmod=hashlib.sha256
                ).digest()
                payload.update(timestamp=stamp, sign=base64.b64encode(signature).decode())
            v = await self.net.json("POST", c["url"], payload)
            if v.get("code", v.get("StatusCode")) != 0:
                raise PlatformRejected("飞书群机器人拒绝消息")
            return {"accepted": True}
        if kind in ("wecom_group", "dingtalk_group"):
            if target != "group":
                raise PlatformRejected("群机器人接收目标已变更")
            url = c["url"]
            content = text
            if kind == "dingtalk_group":
                if c.get("keyword"):
                    content = c["keyword"] + "\n" + content
                if c.get("sign_secret"):
                    stamp = str(int(time.time() * 1000))
                    signature = base64.b64encode(
                        hmac.new(
                            c["sign_secret"].encode(),
                            (stamp + "\n" + c["sign_secret"]).encode(),
                            hashlib.sha256,
                        ).digest()
                    ).decode()
                    url += "&" + urlencode({"timestamp": stamp, "sign": signature})
            if kind == "wecom_group" and len(content.encode("utf-8")) > 2000:
                raise PlatformRejected("企业微信群消息超出单条字节预算")
            v = await self.net.json("POST", url, {"msgtype": "text", "text": {"content": content}})
            if v.get("errcode") != 0:
                raise PlatformRejected("群机器人拒绝消息")
            return {"accepted": True}
        headers = {"Idempotency-Key": delivery_id}
        if c.get("bearer"):
            headers["Authorization"] = "Bearer " + c["bearer"]
        # A successful HTTP response acknowledges submission, not human receipt.
        await self.net.request("POST", c["url"], {"id": delivery_id, "text": text}, headers)
        return {"accepted": True}
