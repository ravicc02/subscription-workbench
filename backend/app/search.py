"""Opt-in, bounded search tools for conversational answers."""

import json
import time
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from .core import Conflict, digest


class SearchTools:
    def search_preferences(self):
        saved = self.store.setting("search_config") or {}
        secret = self.store.open(saved.get("secret"))
        return {
            "revision": saved.get("revision", 0),
            "provider": saved.get("provider", "searxng"),
            "base_url": saved.get("base_url", ""),
            "enabled": saved.get("enabled", False),
            "has_key": bool(secret.get("api_key")),
        }

    def save_search_preferences(self, body):
        saved = self.store.setting("search_config") or {}
        if body.get("revision") != saved.get("revision", 0):
            raise Conflict("检索配置已更新，请刷新后重试")
        provider = body.get("provider", "searxng")
        enabled = body.get("enabled", False)
        base = str(body.get("base_url") or "").rstrip("/")
        if provider not in ("searxng", "brave") or type(enabled) is not bool:
            raise ValueError("检索服务配置无效")
        if provider == "searxng" and (base or enabled):
            parsed = self.net.validate_url(base)
            if parsed.query or parsed.fragment or len(base) > 500:
                raise ValueError("搜索服务地址不能包含参数或凭据")
        else:
            base = ""
        old_secret = self.store.open(saved.get("secret"))
        api_key = body.get("api_key")
        if api_key is None or api_key == "":
            api_key = old_secret.get("api_key", "")
        if not isinstance(api_key, str) or len(api_key) > 300 or any(c.isspace() for c in api_key):
            raise ValueError("检索服务密钥无效")
        if enabled and provider == "brave" and not api_key:
            raise ValueError("请填写 Brave Search API Key")
        self.store.set_setting(
            "search_config",
            {
                "revision": saved.get("revision", 0) + 1,
                "provider": provider,
                "base_url": base,
                "enabled": enabled,
                "secret": self.store.seal({"api_key": api_key}),
            },
        )
        self.store.audit(
            "search", "configuration", "SAVED", {"provider": provider, "enabled": enabled}
        )
        return self.search_preferences()

    def search_proxy(self):
        return self.bot_proxy({})

    async def search_web(self, query):
        setting = self.store.setting("search_config") or {}
        if not setting.get("enabled"):
            raise ValueError("实时检索未启用")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160:
            raise ValueError("搜索词长度应为 1～160 字")
        query = query.strip()
        proxy = self.search_proxy()
        if setting["provider"] == "brave":
            url = "https://api.search.brave.com/res/v1/web/search?" + urlencode(
                {"q": query, "count": 5}
            )
            secret = self.store.open(setting.get("secret"))
            headers = {"X-Subscription-Token": secret["api_key"], "Accept": "application/json"}
        else:
            url = setting["base_url"] + "/search?" + urlencode({"q": query, "format": "json"})
            headers = {"Accept": "application/json"}
        data, _, _ = await self.net.request(
            "GET", url, headers=headers, limit=250_000, timeout=12, proxy=proxy
        )
        parsed = json.loads(data)
        results = (
            parsed.get("web", {}).get("results", [])
            if setting["provider"] == "brave"
            else parsed.get("results", [])
        )
        if not isinstance(results, list):
            raise ValueError("搜索服务没有返回结果列表")
        items = []
        for result in results[:10]:
            link = result.get("url", "") if isinstance(result, dict) else ""
            try:
                self.net.validate_url(link)
            except (ValueError, TypeError):
                continue
            items.append(
                {
                    "title": str(result.get("title") or "")[:180],
                    "url": link,
                    "snippet": str(result.get("description") or result.get("content") or "")[:450],
                }
            )
            if len(items) == 5:
                break
        self.store.audit(
            "search",
            "conversation",
            "SEARCHED",
            {"query_sha256": digest(query.encode()), "results": len(items)},
        )
        return items

    async def fetch_page(self, url):
        self.net.validate_url(url)
        data, _, _ = await self.net.request(
            "GET", url, limit=350_000, timeout=12, proxy=self.search_proxy()
        )
        document = BeautifulSoup(data, "lxml")
        for node in document(["script", "style", "nav", "footer", "header"]):
            node.decompose()
        text = " ".join(document.stripped_strings)[:10000]
        if not text:
            raise ValueError("网页没有可读取的正文")
        result = {"url": url, "observed_at": time.time(), "text": text}
        self.store.audit("search", "conversation", "FETCHED", {"host": urlsplit(url).hostname})
        return result
