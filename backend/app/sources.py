"""RSS/Atom, webpage changes, JSON APIs and authenticated event inputs."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .article import clean_body
from .core import digest


def canonical(url):
    u = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")
    ]
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urlencode(query), ""))


def stamp(value):
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .replace(tzinfo=timezone.utc)
            .timestamp()
            if not re.search(r"[+-]\d\d:\d\d$", value)
            else datetime.fromisoformat(value).timestamp()
        )
    except (ValueError, AttributeError):
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, AttributeError):
            return None


def feed_items(raw, base):
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        raise ValueError("XML entities are not supported")
    tree = ET.fromstring(raw)
    items = []

    def text(node, name):
        return next((c.text or "" for c in node if c.tag.rsplit("}", 1)[-1] == name), "").strip()

    for item in tree.iter():
        if item.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = text(item, "title")
        link = text(item, "link")
        if not link:
            link = next(
                (
                    c.get("href", "")
                    for c in item
                    if c.tag.rsplit("}", 1)[-1] == "link"
                    and c.get("rel", "alternate") == "alternate"
                ),
                "",
            )
        if not title or not link:
            continue
        url = urljoin(base, link)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        published = text(item, "pubDate") or text(item, "published") or text(item, "updated")
        summary = text(item, "description") or text(item, "summary") or text(item, "content")
        item_id = text(item, "guid") or text(item, "id") or canonical(url)
        items.append(
            {
                "item_key": item_id,
                "title": title,
                "url": canonical(url),
                "published": published,
                "published_timestamp": stamp(published),
                "summary": BeautifulSoup(summary, "lxml").get_text(" ", strip=True),
                "content_type": "article",
            }
        )
    return items


class Sources:
    def __init__(self, network, policy, proxy_config=None):
        self.net = network
        self.policy = policy
        self.proxy_config = proxy_config or (lambda source: None)

    async def fetch(self, url, source):
        # Blocking HTTP is isolated to threads; the reused same-site lock/cooldown applies to both RSS and bodies.
        proxy = self.proxy_config(source)

        def operation():
            return self.net._request("GET", url, limit=2_000_000, proxy=proxy)

        return await asyncio.to_thread(self.policy.fetch, url, source, operation)

    async def discover(self, source):
        if source["type"] == "webhook":
            return []
        raw, final, _ = await self.fetch(source["url"], source)
        if source["type"] == "rss":
            return feed_items(raw, final)
        if source["type"] == "webpage":
            soup = BeautifulSoup(raw, "lxml")
            for n in soup.select("script,style,nav,footer,header"):
                n.decompose()
            part = soup.select_one(source.get("selector") or "main") or soup.body or soup
            content = part.get_text("\n", strip=True)
            if not content:
                raise ValueError("Page selector produced no readable content")
            title = soup.title.get_text(strip=True) if soup.title else source["name"]
            return [
                {
                    "item_key": canonical(source["url"]),
                    "title": title,
                    "url": final,
                    "text": content[:80000],
                    "content_type": "page",
                    "published_timestamp": time.time(),
                }
            ]
        value = json.loads(raw)
        path = source.get("json_path", "")
        for key in filter(None, path.split(".")):
            if not isinstance(value, dict) or key not in value:
                raise ValueError("JSON path does not exist")
            value = value[key]
        values = value if isinstance(value, list) else [value]
        rows = []

        def field(item, key, fallback=""):
            if not key:
                return fallback
            current = item
            for part in key.split("."):
                if not isinstance(current, dict):
                    return fallback
                current = current.get(part)
            return fallback if current is None else current

        for i, v in enumerate(values[:500]):
            item_key = str(field(v, source.get("id_field", "id"), i))
            title = (
                str(field(v, source.get("title_field", "title")) or v.get("name") or source["name"])
                if isinstance(v, dict)
                else source["name"]
            )
            body = field(v, source.get("content_field", ""))
            content = str(body) if body else json.dumps(v, ensure_ascii=False, indent=2)
            link = field(v, source.get("url_field", ""))
            published = str(field(v, source.get("published_field", "")))
            rows.append(
                {
                    "item_key": item_key,
                    "title": title,
                    "text": content[:80000],
                    "content_type": "json",
                    "url": str(link) if isinstance(link, str) and link.startswith("https://") else source["url"],
                    "published": published,
                    "published_timestamp": stamp(published) if published else time.time(),
                }
            )
        return rows

    @staticmethod
    def version(row, source=None):
        # Observation time is not part of the change signature.
        fields = ("item_key", "title", "url", "summary", "text", "published")
        if source and source.get("update_on_change") is False:
            fields = ("item_key", "title", "url", "published")
        return digest({k: row.get(k) for k in fields})

    async def capture(self, row, source):
        if row.get("text"):
            return {
                **row,
                "content_level": "structured" if row["content_type"] == "json" else "full_text",
            }
        if source.get("capture_mode") == "summary":
            summary = row.get("summary", "").strip()
            if len(summary) < 50:
                raise ValueError("Feed summary is too short; no invented content")
            return {**row, "text": summary[:10000], "content_level": "summary_only"}
        host = urlsplit(row["url"]).hostname
        domains = source.get("domains") or [urlsplit(source["url"]).hostname]
        if host not in domains:
            raise ValueError("Article domain is outside source allowlist")
        raw, final, _ = await self.fetch(row["url"], source)
        if urlsplit(final).hostname not in domains:
            raise ValueError("Redirected article domain is outside source allowlist")
        paragraphs, _, level = clean_body(
            raw,
            {
                **source,
                "selectors": source.get("selectors") or ["article", "main"],
                "min_body_characters": source.get("min_body_characters", 150),
                "min_body_paragraphs": source.get("min_body_paragraphs", 1),
                "min_paragraph_characters": source.get("min_paragraph_characters", 15),
            },
        )
        if not level:
            text = row.get("summary", "").strip()
            if len(text) < 50:
                raise ValueError("No usable body or source summary; no invented content")
            level = "summary_only"
        else:
            text = "\n\n".join(f"[P{i:03}] {p}" for i, p in enumerate(paragraphs, 1))
        return {
            **row,
            "url": final,
            "text": text[:100000],
            "content_level": level,
            "html": raw.decode("utf-8", errors="replace"),
        }
