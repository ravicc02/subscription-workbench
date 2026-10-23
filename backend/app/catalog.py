"""Local taxonomy and portable, additive subscription package exchange."""

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from .core import Conflict
from .sources import canonical

FORMAT = "subscription-workbench/package"
VERSION = 1
SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
POLL_MINUTES = (5, 10, 15, 30, 60)
FIELDS = (
    "name",
    "type",
    "url",
    "tags",
    "poll_minutes",
    "proxy_mode",
    "domains",
    "selector",
    "json_path",
    "id_field",
    "title_field",
    "url_field",
    "content_field",
    "published_field",
    "allow_undated",
    "capture_mode",
    "update_on_change",
)
API_FIELDS = ("id_field", "title_field", "url_field", "content_field", "published_field")


def label(value, maximum=100):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError("名称或描述长度无效")
    return value.strip()


def slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError("标识只能由小写字母、数字及连字符组成，且以字母开头")
    return value


def source_identity(source):
    return source["type"], canonical(source["url"])


class Catalog:
    def save_category(self, body):
        current = self.store.get("category", body["id"]) if body.get("id") else None
        name = label(body.get("name"), 80)
        key = current["key"] if current else slug(body.get("key"))
        if current and body.get("revision") != current["revision"]:
            raise Conflict("分类已变化，请刷新")
        if not current and any(c["key"] == key for c in self.store.all("category")):
            raise Conflict("分类标识已存在")
        saved = self.store.put(
            "category", {"id": current["id"] if current else None, "key": key, "name": name},
            body.get("revision"),
        )
        self.store.audit("category", saved["id"], "SAVED")
        return saved

    def save_tag(self, body):
        category = self.store.get("category", body.get("category_id"))
        current = self.store.get("tag", body["id"]) if body.get("id") else None
        name = label(body.get("name"), 80)
        key = current["key"] if current else slug(body.get("key"))
        if current and (body.get("revision") != current["revision"] or current["category_id"] != category["id"]):
            raise Conflict("标签已变化或所属分类不可修改")
        if not current and any(
            t["category_id"] == category["id"] and t["key"] == key
            for t in self.store.all("tag")
        ):
            raise Conflict("该分类下的标签标识已存在")
        saved = self.store.put(
            "tag", {"id": current["id"] if current else None, "key": key, "name": name,
                    "category_id": category["id"]}, body.get("revision"),
        )
        self.store.audit("tag", saved["id"], "SAVED")
        return saved

    def delete_taxonomy(self, kind, key, revision):
        if kind not in ("category", "tag"):
            raise ValueError("未知分类对象")
        row = self.store.get(kind, key)
        if revision != row["revision"]:
            raise Conflict("配置已变化，请刷新")
        if kind == "category":
            used = any(s.get("category_id") == key for s in self.store.all("source")) or any(
                s.get("category_id") == key for s in self.store.all("subscription")
            ) or any(t["category_id"] == key for t in self.store.all("tag"))
        else:
            used = any(key in (s.get("tag_ids") or []) for s in self.store.all("source")) or any(
                key in (s.get("tag_ids") or []) for s in self.store.all("subscription")
            )
        if used:
            raise Conflict("仍有来源、订阅或下级标签引用；请先重新归类后删除")
        self.store.db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, key))
        self.store.audit(kind, key, "DELETED")
        return {"ok": True}

    def validate_taxonomy(self, category_id, tag_ids):
        if tag_ids is None:
            tag_ids = []
        if (
            not isinstance(tag_ids, list)
            or len(tag_ids) > 30
            or any(not isinstance(t, str) for t in tag_ids)
            or len(set(tag_ids)) != len(tag_ids)
        ):
            raise ValueError("标签必须是不重复的列表，最多 30 个")
        if not category_id:
            if tag_ids:
                raise ValueError("请先选择分类")
            return None, []
        self.store.get("category", category_id)
        for key in tag_ids:
            tag = self.store.get("tag", key)
            if tag["category_id"] != category_id:
                raise ValueError("标签不属于所选分类")
        return category_id, tag_ids

    def validate_package(self, manifest):
        if not isinstance(manifest, dict) or set(manifest) != {
            "format", "version", "package", "categories"
        } or manifest["format"] != FORMAT or manifest["version"] != VERSION:
            raise ValueError("不支持的订阅包格式或版本")
        metadata = manifest["package"]
        if not isinstance(metadata, dict) or set(metadata) != {"key", "name", "description"}:
            raise ValueError("订阅包信息不完整")
        slug(metadata["key"])
        label(metadata["name"], 80)
        if not isinstance(metadata["description"], str) or len(metadata["description"]) > 500:
            raise ValueError("订阅包说明无效")
        categories = manifest["categories"]
        if not isinstance(categories, list) or not 1 <= len(categories) <= 20:
            raise ValueError("订阅包须包含 1～20 个分类")
        category_keys, identities, total = set(), set(), 0
        for category in categories:
            if not isinstance(category, dict) or set(category) != {"key", "name", "tags", "sources"}:
                raise ValueError("分类字段无效")
            key = slug(category["key"])
            if key in category_keys:
                raise ValueError("订阅包内分类重复")
            category_keys.add(key)
            label(category["name"], 80)
            tags = category["tags"]
            sources = category["sources"]
            if not isinstance(tags, list) or len(tags) > 30 or not isinstance(sources, list) or len(sources) > 100:
                raise ValueError("分类中的标签或来源过多")
            tag_keys = set()
            for tag in tags:
                if not isinstance(tag, dict) or set(tag) != {"key", "name"}:
                    raise ValueError("标签字段无效")
                tag_key = slug(tag["key"])
                if tag_key in tag_keys:
                    raise ValueError("分类内标签重复")
                tag_keys.add(tag_key)
                label(tag["name"], 80)
            for source in sources:
                if not isinstance(source, dict) or not {"name", "type", "url", "tags"} <= set(source) or set(source) - set(FIELDS):
                    raise ValueError("来源字段无效")
                label(source["name"], 100)
                if source["type"] not in ("rss", "webpage", "api"):
                    raise ValueError("共享包只能包含无需私人凭据的 RSS、网页或公开 API")
                if not isinstance(source["url"], str) or len(source["url"]) > 2048:
                    raise ValueError("来源地址无效")
                self.net.validate_url(source["url"])
                parsed = urlsplit(source["url"])
                host = parsed.hostname.lower().rstrip(".")
                if parsed.scheme != "https" or host in ("localhost", "127.0.0.1") or host.endswith((".local", ".internal", ".localhost")):
                    raise ValueError("可移植订阅包只接受公开 HTTPS 地址")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    raise ValueError("订阅包不能包含内网地址")
                identity = source_identity(source)
                if identity in identities:
                    raise ValueError("订阅包内来源重复")
                identities.add(identity)
                source_tags = source["tags"]
                if (
                    not isinstance(source_tags, list)
                    or any(not isinstance(t, str) for t in source_tags)
                    or len(source_tags) != len(set(source_tags))
                    or not set(source_tags) <= tag_keys
                ):
                    raise ValueError("来源引用了未知或重复标签")
                if source.get("poll_minutes", 15) not in POLL_MINUTES or type(source.get("poll_minutes", 15)) is not int:
                    raise ValueError("来源轮询间隔无效")
                if source.get("proxy_mode", "inherit") not in ("inherit", "direct", "proxy"):
                    raise ValueError("来源代理配置无效")
                for flag in ("allow_undated", "update_on_change"):
                    if flag in source and type(source[flag]) is not bool:
                        raise ValueError("来源布尔配置无效")
                if source.get("capture_mode", "full") not in ("full", "summary"):
                    raise ValueError("来源正文方式无效")
                if source["type"] != "rss" and (source.get("allow_undated") or source.get("capture_mode") == "summary"):
                    raise ValueError("无日期与摘要采集仅适用于 RSS")
                if "domains" in source and (
                    not isinstance(source["domains"], list) or len(source["domains"]) > 30
                    or any(not isinstance(d, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", d) for d in source["domains"])
                ):
                    raise ValueError("文章域名列表无效")
                for field in ("selector", "json_path", *API_FIELDS):
                    if field in source and (
                        not isinstance(source[field], str) or len(source[field]) > 300
                        or (field in API_FIELDS and source[field] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", source[field]))
                    ):
                        raise ValueError("来源字段映射无效")
                total += 1
        if total > 300:
            raise ValueError("订阅包来源总数过多")
        return manifest

    def preview_package(self, manifest):
        self.validate_package(manifest)
        existing_categories = {c["key"]: c for c in self.store.all("category")}
        existing_sources = {source_identity(s): s for s in self.store.all("source") if s.get("url")}
        existing_tags = {(t["category_id"], t["key"]): t["id"] for t in self.store.all("tag")}
        counts = {"categories_new": 0, "tags_new": 0, "sources_new": 0, "sources_updated": 0, "sources_existing": 0, "conflicts": []}
        for category in manifest["categories"]:
            local = existing_categories.get(category["key"])
            if not local:
                counts["categories_new"] += 1
            for tag in category["tags"]:
                if not local or (local["id"], tag["key"]) not in existing_tags:
                    counts["tags_new"] += 1
            for source in category["sources"]:
                found = existing_sources.get(source_identity(source))
                if not found:
                    counts["sources_new"] += 1
                elif found.get("category_id") and (not local or found["category_id"] != local["id"]):
                    counts["conflicts"].append(source["name"])
                elif not local or found.get("category_id") != local["id"] or any(
                    existing_tags.get((local["id"], key)) not in (found.get("tag_ids") or [])
                    for key in source["tags"]
                ):
                    counts["sources_updated"] += 1
                else:
                    counts["sources_existing"] += 1
        return counts

    def import_package(self, manifest):
        impact = self.preview_package(manifest)
        if impact["conflicts"]:
            raise Conflict("来源已归入其他分类，请先手动调整：" + "、".join(impact["conflicts"][:5]))
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            for category in manifest["categories"]:
                existing = next((c for c in self.store.all("category") if c["key"] == category["key"]), None)
                local = existing or self.save_category(category)
                tags = {}
                for tag in category["tags"]:
                    found = next((t for t in self.store.all("tag") if t["category_id"] == local["id"] and t["key"] == tag["key"]), None)
                    tags[tag["key"]] = found or self.save_tag({**tag, "category_id": local["id"]})
                for source in category["sources"]:
                    identity = source_identity(source)
                    found = next((s for s in self.store.all("source") if s.get("url") and source_identity(s) == identity), None)
                    tagged = [tags[key]["id"] for key in source["tags"]]
                    if found:
                        if found.get("category_id") not in (None, local["id"]):
                            raise Conflict("来源分类在导入时已变化，请重试")
                        combined = sorted(set(found.get("tag_ids") or []) | set(tagged))
                        if found.get("category_id") != local["id"] or combined != sorted(found.get("tag_ids") or []):
                            self.save_source({**found, "category_id": local["id"], "tag_ids": combined}, network_checked=True)
                    else:
                        self.save_source({
                            **{k: v for k, v in source.items() if k != "tags"},
                            "category_id": local["id"], "tag_ids": tagged,
                        }, network_checked=True)
            self.store.audit("package", manifest["package"]["key"], "IMPORTED", impact)
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return impact

    def export_package(self, body):
        keys = body.get("category_ids")
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 20
            or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys)):
            raise ValueError("请选择 1～20 个不重复的分类")
        metadata = {
            "key": slug(body.get("key")),
            "name": label(body.get("name"), 80),
            "description": body.get("description", ""),
        }
        result = {"format": FORMAT, "version": VERSION, "package": metadata, "categories": []}
        for key in keys:
            category = self.store.get("category", key)
            tags = [t for t in self.store.all("tag") if t["category_id"] == key]
            tag_keys = {t["id"]: t["key"] for t in tags}
            sources = []
            for source in self.store.all("source"):
                if source.get("category_id") != key or source["type"] == "webhook":
                    continue
                sources.append({
                    **{field: source[field] for field in FIELDS if field in source and field != "tags"},
                    "tags": [tag_keys[t] for t in source.get("tag_ids", []) if t in tag_keys],
                })
            result["categories"].append({
                "key": category["key"], "name": category["name"],
                "tags": [{"key": t["key"], "name": t["name"]} for t in tags],
                "sources": sources,
            })
        return self.validate_package(result)

    def builtin_packages(self):
        import json

        folder = Path(__file__).resolve().parents[2] / "packs"
        result = []
        for path in sorted(folder.glob("*.json")):
            manifest = self.validate_package(json.loads(path.read_text(encoding="utf-8")))
            result.append({
                "manifest": manifest,
                "preview": self.preview_package(manifest),
                "source_count": sum(len(c["sources"]) for c in manifest["categories"]),
            })
        return result
