"""Encrypted, revisioned collection-only proxy preferences."""

import asyncio
import re
import time

from .core import Conflict
from .polling import DeferredRequest

DEFAULT = {
    "enabled": False,
    "scheme": "http",
    "host": "127.0.0.1",
    "port": 7890,
    "username": "",
    "password": "",
    "remote_dns": True,
}


class CollectionSettings:
    def proxy_preferences(self):
        row = self.store.setting("collection_proxy")
        cfg = self.store.open(row["secret"]) if row else dict(DEFAULT)
        return {
            **{k: v for k, v in cfg.items() if k != "password"},
            "revision": row["revision"] if row else 0,
            "configured": bool(row),
            "has_password": bool(cfg.get("password")),
        }

    def proxy_config(self, body=None):
        stored = self.store.setting("collection_proxy")
        old = self.store.open(stored["secret"]) if stored else dict(DEFAULT)
        if body is None:
            return old
        if not isinstance(body, dict):
            raise ValueError("代理配置必须是对象")
        cfg = {**old, **{k: body[k] for k in DEFAULT if k in body and k != "password"}}
        for field in ("enabled", "remote_dns"):
            if type(cfg[field]) is not bool:
                raise ValueError("代理开关和 DNS 设置必须是布尔值")
        if cfg["scheme"] not in ("http", "socks5"):
            raise ValueError("支持 HTTP 和 SOCKS5 代理")
        if not isinstance(cfg["host"], str):
            raise ValueError("代理主机名格式无效")
        cfg["host"] = cfg["host"].strip().strip("[]")
        if (
            not cfg["host"]
            or len(cfg["host"]) > 253
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", cfg["host"])
        ):
            raise ValueError("代理主机仅填写主机名或 IP，不包含协议、路径或认证信息")
        if type(cfg["port"]) is not int or not 1 <= cfg["port"] <= 65535:
            raise ValueError("代理端口范围是 1～65535")
        if not isinstance(cfg["username"], str) or len(cfg["username"]) > 255:
            raise ValueError("代理用户名过长或格式无效")
        provided = body.get("password")
        if "clear_password" in body and type(body["clear_password"]) is not bool:
            raise ValueError("清除认证开关必须是布尔值")
        if body.get("clear_password"):
            cfg["password"] = ""
            cfg["username"] = ""
        elif provided not in (None, ""):
            if not isinstance(provided, str) or len(provided) > 1000:
                raise ValueError("代理密码过长或格式无效")
            cfg["password"] = provided
        elif old.get("password") and any(
            cfg[k] != old[k] for k in ("host", "port", "scheme", "username")
        ):
            raise ValueError("更换代理地址或用户名后，请重新填写密码或选择清除认证")
        if (
            cfg["scheme"] == "socks5"
            and (cfg["username"] or cfg["password"])
            and (
                not cfg["username"]
                or not cfg["password"]
                or len(cfg["username"].encode()) > 255
                or len(cfg["password"].encode()) > 255
            )
        ):
            raise ValueError("SOCKS5 认证需要填写用户名和密码，分别不超过 255 字节")
        return cfg

    def save_proxy_preferences(self, body):
        current = self.store.setting("collection_proxy")
        revision = current["revision"] if current else 0
        if body.get("revision") != revision:
            raise Conflict("采集网络设置已更新，请刷新后重试")
        cfg = self.proxy_config(body)
        self.store.set_setting(
            "collection_proxy", {"revision": revision + 1, "secret": self.store.seal(cfg)}
        )
        self.store.audit(
            "network",
            "collection",
            "PROXY_SAVED",
            {"enabled": cfg["enabled"], "scheme": cfg["scheme"], "remote_dns": cfg["remote_dns"]},
        )
        return self.proxy_preferences()

    def effective_proxy(self, source):
        mode = source.get("proxy_mode", "inherit")
        if mode not in ("inherit", "direct", "proxy"):
            raise ValueError("订阅源网络方式无效")
        if mode == "direct":
            return None
        stored = self.store.setting("collection_proxy")
        cfg = self.proxy_config()
        if mode == "inherit" and not cfg["enabled"]:
            return None
        if mode == "proxy" and not stored:
            raise ValueError("此订阅源指定使用代理，请先在采集网络中配置代理")
        return cfg

    def bot_proxy(self, _config):
        # The collection switch only controls sources; Telegram uses any saved proxy endpoint.
        return self.proxy_config() if self.store.setting("collection_proxy") else None

    async def test_proxy(self, body):
        if body.get("route", "proxy") not in ("proxy", "direct"):
            raise ValueError("测试网络方式无效")
        source = self.store.get("source", body.get("source_id"))
        if source["type"] == "webhook" or not source.get("url"):
            raise ValueError("请选择有 HTTP 地址的订阅源进行测试")
        config = (
            self.proxy_config(body.get("config") or {})
            if body.get("route", "proxy") == "proxy"
            else None
        )
        started = time.monotonic()

        def operation():
            return self.net._request(
                "GET", source["url"], limit=2_000_000, timeout=20, proxy=config
            )

        try:
            raw, final, _ = await asyncio.to_thread(
                self.policy.fetch, source["url"], source, operation
            )
        except DeferredRequest as exc:
            return {"deferred": True, "retry_at": exc.retry_at}
        detail = ""
        if source["type"] == "rss":
            from xml.etree import ElementTree as ET

            from .sources import feed_items

            try:
                items = feed_items(raw, final)
                if ET.fromstring(raw).tag.rsplit("}", 1)[-1] not in ("rss", "feed", "RDF"):
                    raise ValueError("not a feed")
            except (ValueError, ET.ParseError) as exc:
                raise ValueError("返回内容不是有效 RSS/Atom，可能是拦截页或错误页") from exc
            detail = f"有效订阅条目 {len(items)} 条，"
        elif source["type"] == "api":
            import json

            try:
                json.loads(raw)
            except ValueError as exc:
                raise ValueError("返回内容不是有效 JSON，可能是拦截页或错误页") from exc
            detail = "JSON 格式有效，"
        elapsed = round((time.monotonic() - started) * 1000)
        route = "代理" if config else "直连"
        return {
            "source_id": source["id"],
            "route": "proxy" if config else "direct",
            "bytes": len(raw),
            "elapsed_ms": elapsed,
            "summary": f"{route}访问「{source['name']}」成功：{detail}收到 {len(raw)} 字节，耗时 {elapsed} 毫秒。仅测试连接，不入队、不推送。",
        }
