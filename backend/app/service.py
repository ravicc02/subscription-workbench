"""Independent lifecycle, scheduler, processing queue and real delivery workers."""

import asyncio
import json
import math
import random
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import Editor
from .catalog import Catalog
from .channels import CATALOG, Channels, ContextRequired, PlatformRejected
from .collection_settings import CollectionSettings
from .core import Conflict, Store, Workspaces, digest, dumps, uid
from .management import Management
from .network import Network, RemoteError
from .polling import DeferredRequest, RequestPolicy, retry_after
from .profiles import profile_for, rules_for
from .proxy import ProxyError
from .sources import Sources
from .subscriptions import public_subscription, resolved_source_ids


class Workbench(Catalog, Management, CollectionSettings):
    def __init__(self, root, network=None):
        self.store = Store(root)
        self.root = Path(root).resolve()
        self.workspaces = Workspaces(self.store)
        self.net = network or Network()
        self.channels = Channels(self.net, self.bot_proxy)
        self.policy = RequestPolicy(
            self.root / "state/http-state.json",
            {"minimum_request_gap_seconds": 10, "jitter_seconds": 15},
        )
        self.sources = Sources(self.net, self.policy, self.effective_proxy)
        self.editor = Editor(self.store, self.workspaces, self.net)
        self.tasks = []
        self.receivers = {}
        self.poll_locks = {}
        self.qr_locks = {}
        self.running = True
        self.import_sources()

    def import_sources(self):
        if self.store.setting("source_imported"):
            return
        p = self.root / "docs/imported-sources.json"
        if p.is_file():
            for f in json.loads(p.read_text(encoding="utf-8"))["feeds"]:
                if f.get("relay_hosts"):
                    continue
                f = {k: v for k, v in f.items() if k not in ("relay_hosts", "proxy", "token")}
                self.store.put(
                    "source",
                    {
                        **f,
                        "id": "source-" + f["id"],
                        "type": "rss",
                        "enabled": f.get("enabled", True),
                        "poll_minutes": f.get("poll_minutes", 15),
                    },
                )
        self.store.set_setting("source_imported", True)

    def public_bot(self, b):
        draft = self.store.open(b.get("draft_secret"))
        c = self.store.open(b.get("secret")) or draft
        fields = {k: v for k, v in b.items() if k not in ("secret", "draft_secret")}
        fields["targets"] = [
            {"id": t["id"], "name": t["name"], "selected": bool(t.get("selected"))}
            for t in c.get("targets", [])
        ]
        fields["qr_image"] = c.get("qr_image") if b.get("status") == "WAITING_QR" else None
        fields["workspace_path"] = (
            self.store.get("workspace", b["workspace_id"])["path"]
            if b.get("workspace_id")
            else None
        )
        fields["workspace_name"] = (
            self.store.get("workspace", b["workspace_id"])["name"]
            if b.get("workspace_id")
            else None
        )
        if b["type"] == "telegram":
            fields["network_mode"] = "proxy" if self.store.setting("collection_proxy") else "direct"
            if b.get("status") == "CONNECTION_FAILED" and not c.get("token"):
                fields["error"] = "此前接入失败时未保存 Bot Token，请在修改接入配置中重新填写后重试"
        fields["has_credentials"] = bool(c.get("token") or c.get("url"))
        return fields

    def public_model(self, m):
        return {
            **{k: v for k, v in m.items() if k != "secret"},
            "has_key": bool(self.store.open(m.get("secret")).get("api_key")),
        }

    async def create_bot(self, body):
        name = str(body.get("name", "")).strip()
        kind = body.get("type")
        self.channels.info(kind)
        if not 1 <= len(name) <= 80:
            raise ValueError("请输入机器人名称，最多 80 字")
        if body.get("config") is not None and not isinstance(body["config"], dict):
            raise ValueError("接入配置必须是对象")
        workspace_name = body.get("workspace_name")
        if workspace_name is not None and (
            not isinstance(workspace_name, str) or not 1 <= len(workspace_name.strip()) <= 100
        ):
            raise ValueError("工作区名称为 1～100 个字符")
        draft = self.channels.draft_config(kind, body.get("config") or {})
        key = uid("bot")
        b = self.store.put(
            "bot",
            {
                "id": key,
                "name": name,
                "type": kind,
                "enabled": True,
                "status": "CONNECTING",
                "draft_secret": self.store.seal(draft),
                "workspace_id": None,
                "binding_revision": 1,
                "workspace_request": {
                    k: body.get(k)
                    for k in ("workspace_mode", "workspace_id", "workspace_path", "workspace_name")
                },
                "created": time.time(),
            },
        )
        try:
            config, status, extra = await self.channels.begin(kind, draft)
            b.update(secret=self.store.seal(config), status=status, **extra)
            b.pop("draft_secret", None)
            if status != "WAITING_QR":
                b = self.bind_bot(b)
        except Exception as exc:
            b.update(status="CONNECTION_FAILED", error=self.bot_error(kind, draft, exc))
        b = self.store.put("bot", b)
        self.store.audit("bot", key, b["status"])
        return self.public_bot(b)

    @staticmethod
    def safe_error(exc):
        if isinstance(exc, (ValueError, RemoteError, DeferredRequest, ProxyError)):
            return str(exc)[:220]
        return type(exc).__name__ + "；请检查连接和服务日志"

    def bot_error(self, kind, config, exc):
        if kind == "telegram":
            if isinstance(exc, RemoteError) and exc.code in (401, 404):
                return "Telegram 拒绝了 Bot Token，请在修改接入配置中检查或重新填写"
            if isinstance(exc, (ConnectionError, TimeoutError, ProxyError)):
                if self.store.setting("collection_proxy"):
                    return "Telegram 经全局代理连接失败，请检查采集网络中的代理地址和服务日志后重试"
                return "Telegram 直连失败，请在采集网络中保存可用代理后重试"
        return self.safe_error(exc)

    def bind_bot(self, b, request=None):
        req = request or b.get("workspace_request") or {}
        w = self.workspaces.bind(
            b["id"],
            req.get("workspace_mode") or "independent",
            req.get("workspace_id"),
            req.get("workspace_path"),
            req.get("workspace_name"),
        )
        b["workspace_id"] = w["id"]
        b.pop("workspace_request", None)
        return b

    async def poll_qr(self, key):
        async with self.qr_locks.setdefault(key, asyncio.Lock()):
            b = self.store.get("bot", key)
            if b["type"] != "weixin" or b["status"] != "WAITING_QR":
                return self.public_bot(b)
            c, status, extra = await self.channels.qr_status(self.store.open(b["secret"]))
            b.update(secret=self.store.seal(c), status=status, **extra)
            if status == "WAITING_TARGET":
                b = self.bind_bot(b)
            b = self.store.put("bot", b)
            self.store.audit("bot", key, status)
            return self.public_bot(b)

    async def reconnect(self, key, config=None):
        b = self.store.get("bot", key)
        if self.store.db.execute(
            "SELECT 1 FROM deliveries WHERE bot_id=? AND state='SENDING'", (key,)
        ).fetchone():
            raise ValueError("发送进行中，完成后再修改接入")
        if b.get("status") == "WAITING_QR":
            return self.public_bot(b)
        old = self.store.open(b.get("secret"))
        merged = {**old, **self.store.open(b.get("draft_secret"))}
        if config is not None:
            if not isinstance(config, dict):
                raise ValueError("接入配置必须是对象")
            merged.update({k: v for k, v in config.items() if v not in (None, "")})
        draft = self.channels.draft_config(b["type"], merged)
        receiver = self.receivers.pop(key, None)
        if receiver:
            receiver.cancel()
        b.update(draft_secret=self.store.seal(draft), status="CONNECTING", error=None)
        b = self.store.put("bot", b)
        try:
            c, status, extra = await self.channels.begin(b["type"], {**merged, **draft})
        except Exception as exc:
            b.update(status="CONNECTION_FAILED", error=self.bot_error(b["type"], draft, exc))
            self.store.put("bot", b)
            raise ValueError(b["error"]) from exc
        if (
            b["type"] == "telegram"
            and merged.get("token") == old.get("token")
            and not merged.get("chat_id")
        ):
            c["targets"] = old.get("targets", [])
            c["offset"] = old.get("offset", 0)
            if any(t.get("selected") for t in c["targets"]):
                status = "CONNECTED"
        changed = (
            b["type"] == "weixin"
            or any(
                c.get(k) != old.get(k)
                for k in (
                    "token",
                    "url",
                    "bearer",
                    "access_token",
                    "message_type",
                    "channel_id",
                    "target_id",
                    "sign_secret",
                    "keyword",
                )
            )
            or c.get("targets") != old.get("targets")
        )
        if changed:
            b["binding_revision"] += 1
            self.store.db.execute(
                "UPDATE deliveries SET state='CANCELLED',error='Platform binding changed',updated=? WHERE bot_id=? AND state IN ('WAITING_CONTENT','READY','PAUSED','WAITING_CONTEXT')",
                (time.time(), key),
            )
        b.update(secret=self.store.seal(c), status=status, error=None, **extra)
        b.pop("draft_secret", None)
        if status != "WAITING_QR" and not b.get("workspace_id"):
            b = self.bind_bot(b)
        b = self.store.put("bot", b)
        return self.public_bot(b)

    def update_bot(self, key, body):
        b = self.store.get("bot", key)
        if "enabled" in body and type(body["enabled"]) is not bool:
            raise ValueError("启用状态必须是布尔值")
        if "name" in body and (
            not isinstance(body["name"], str) or not 1 <= len(body["name"].strip()) <= 80
        ):
            raise ValueError("机器人名称无效")
        if body.get("model_id"):
            self.store.get("model", body["model_id"])
        if "target_ids" in body and (
            not isinstance(body["target_ids"], list)
            or any(not isinstance(t, str) for t in body["target_ids"])
        ):
            raise ValueError("接收目标必须是列表")
        if body.get("revision") != b["revision"]:
            raise ValueError("机器人配置已更新，请刷新后重试")
        if any(
            k in body for k in ("workspace_id", "workspace_mode", "workspace_path", "target_ids")
        ):
            if self.store.db.execute(
                "SELECT 1 FROM deliveries WHERE bot_id=? AND state='SENDING'", (key,)
            ).fetchone():
                raise ValueError("发送进行中，完成后再修改绑定")
            if any(k in body for k in ("workspace_id", "workspace_mode", "workspace_path")):
                b = self.bind_bot(b, body)
            if "target_ids" in body:
                c = self.store.open(b["secret"])
                selected = set(body["target_ids"])
                known = {t["id"] for t in c.get("targets", [])}
                if not selected <= known:
                    raise ValueError("只能选择已发现的接收目标")
                for t in c["targets"]:
                    t["selected"] = t["id"] in selected
                b["secret"] = self.store.seal(c)
            b["binding_revision"] += 1
            self.store.db.execute(
                "UPDATE deliveries SET state='CANCELLED',error='Bot binding changed',updated=? WHERE bot_id=? AND state IN ('READY','PAUSED','WAITING_CONTEXT')",
                (time.time(), key),
            )
        for k in ("name", "enabled", "model_id"):
            if k in body:
                b[k] = body[k]
        b = self.store.put("bot", b)
        self.store.audit("bot", key, "UPDATED")
        return self.public_bot(b)

    def save_source(self, body, network_checked=False):
        kind = body.get("type", "rss")
        if kind not in ("rss", "webpage", "api", "webhook"):
            raise ValueError("Unknown source type")
        if not isinstance(body.get("name"), str) or not 1 <= len(body["name"]) <= 100:
            raise ValueError("Source name is required")
        if "enabled" in body and type(body["enabled"]) is not bool:
            raise ValueError("Enabled must be a boolean")
        if "domains" in body and (
            not isinstance(body["domains"], list)
            or len(body["domains"]) > 30
            or any(
                not isinstance(d, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", d)
                for d in body["domains"]
            )
        ):
            raise ValueError("Use a bounded list of exact domain names")
        for field in ("selectors", "exclude_url_patterns"):
            if field in body and (
                not isinstance(body[field], list)
                or len(body[field]) > 30
                or any(not isinstance(x, str) or len(x) > 300 for x in body[field])
            ):
                raise ValueError("Invalid source extraction configuration")
        previous = self.store.get("source", body["id"]) if body.get("id") else None
        data = {
            k: body[k]
            for k in (
                "id",
                "name",
                "url",
                "type",
                "enabled",
                "poll_minutes",
                "proxy_mode",
                "poll_offset",
                "poll_second",
                "selector",
                "selectors",
                "domains",
                "json_path",
                "default_categories",
                "min_body_characters",
                "min_body_paragraphs",
                "min_paragraph_characters",
                "paragraph_mode",
                "exclude_url_patterns",
                "include_tables",
                "category_id",
                "tag_ids",
                "allow_undated",
                "capture_mode",
                "update_on_change",
                "id_field",
                "title_field",
                "url_field",
                "content_field",
                "published_field",
            )
            if k in body
        }
        data["category_id"], data["tag_ids"] = self.validate_taxonomy(
            body.get("category_id", previous.get("category_id") if previous else None),
            body.get("tag_ids", previous.get("tag_ids", []) if previous else []),
        )
        data.update(type=kind, enabled=body.get("enabled", True))
        minutes = body.get("poll_minutes", 15)
        if type(minutes) is not int or minutes not in (5, 10, 15, 30, 60):
            raise ValueError("轮询间隔请选择 5、10、15、30 或 60 分钟")
        data["poll_minutes"] = minutes
        mode = body.get("proxy_mode", "inherit")
        if mode not in ("inherit", "direct", "proxy"):
            raise ValueError("订阅源网络方式无效")
        data["proxy_mode"] = mode
        for flag in ("allow_undated", "update_on_change"):
            if flag in data and type(data[flag]) is not bool:
                raise ValueError("来源布尔设置无效")
        if kind != "rss" and (data.get("allow_undated") or data.get("capture_mode") == "summary"):
            raise ValueError("无日期与摘要采集仅适用于 RSS")
        if data.get("capture_mode", "full") not in ("full", "summary"):
            raise ValueError("来源正文方式无效")
        for field in (
            "selector",
            "json_path",
            "id_field",
            "title_field",
            "url_field",
            "content_field",
            "published_field",
        ):
            if field in data and (not isinstance(data[field], str) or len(data[field]) > 300):
                raise ValueError("来源字段映射无效")
        if kind != "webhook":
            if not network_checked:
                self.net.destination(str(data.get("url", "")), self.effective_proxy(data))
        else:
            import secrets

            existing = self.store.get("source", data["id"]) if data.get("id") else None
            data["hook_hash"] = (
                existing["hook_hash"] if existing else digest(secrets.token_bytes(32))
            )
        edges, cancel = self.source_subscription_unlinks(previous, data) if previous else ([], [])
        nested = self.store.db.in_transaction
        self.store.db.execute("SAVEPOINT source_save" if nested else "BEGIN IMMEDIATE")
        try:
            data = self.store.put("source", data, body.get("revision"))
            if previous and previous.get("url") != data.get("url"):
                self.store.db.execute("DELETE FROM seen WHERE source_id=?", (data["id"],))
                self.store.db.execute("DELETE FROM feed_state WHERE source_id=?", (data["id"],))
            for job_id, sub_id in edges:
                self.store.db.execute(
                    "DELETE FROM job_subscriptions WHERE job_id=? AND subscription_id=?",
                    (job_id, sub_id),
                )
            for job_id in cancel:
                self.store.db.execute(
                    "UPDATE jobs SET state='CANCELLED',error='Source unassigned',updated=? "
                    "WHERE id=? AND state='QUEUED'",
                    (time.time(), job_id),
                )
                self.store.db.execute(
                    "UPDATE deliveries SET state='CANCELLED',error='Source unassigned',updated=? "
                    "WHERE job_id=? AND state IN ('WAITING_CONTENT','READY','WAITING_CONTEXT','PAUSED')",
                    (time.time(), job_id),
                )
            self.store.audit("source", data["id"], "SAVED")
            if nested:
                self.store.db.execute("RELEASE source_save")
            else:
                self.store.db.commit()
        except BaseException:
            if nested:
                self.store.db.execute("ROLLBACK TO source_save")
                self.store.db.execute("RELEASE source_save")
            else:
                self.store.db.rollback()
            raise
        return self.public_source(data)

    def public_source(self, s):
        health = self.store.db.execute(
            "SELECT * FROM feed_state WHERE source_id=?", (s["id"],)
        ).fetchone()
        site = self.policy.summary()["sites"].get(self.policy.site(s), {}) if s.get("url") else {}
        return {
            **{k: v for k, v in s.items() if k != "hook_hash"},
            "category_id": s.get("category_id"),
            "tag_ids": s.get("tag_ids") or [],
            "health": dict(health) if health else {},
            "cooldown_until": site.get("cooldown_until", 0),
        }

    def save_subscription(self, body):
        for k in ("name", "workspace_id"):
            if not body.get(k):
                raise ValueError("订阅名称和工作区为必填")
        self.store.get("workspace", body["workspace_id"])
        ids = (
            body.get("source_ids")
            if "source_ids" in body
            else ([body["source_id"]] if body.get("source_id") else [])
        )
        if (
            not isinstance(ids, list)
            or len(ids) > 100
            or any(not isinstance(i, str) for i in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("请选择 1～100 个不重复的订阅源")
        for key in ids:
            self.store.get("source", key)
        category_id, tag_ids = self.validate_taxonomy(
            body.get("category_id"), body.get("tag_ids", [])
        )
        if (category_id and ids) or (not category_id and not ids):
            raise ValueError("请按分类/标签或指定来源选择一种订阅范围")
        if body.get("mode") not in ("forward", "rules", "agent"):
            raise ValueError("请选择处理方式")
        if body.get("model_id"):
            self.store.get("model", body["model_id"])
        data = {
            k: body[k]
            for k in (
                "id",
                "name",
                "workspace_id",
                "source_id",
                "source_ids",
                "category_id",
                "tag_ids",
                "mode",
                "model_id",
                "instructions",
                "keywords",
                "enabled",
            )
            if k in body
        }
        if (
            not isinstance(data.get("keywords", []), list)
            or len(data.get("keywords", [])) > 100
            or any(
                not isinstance(k, str) or not 1 <= len(k) <= 100 for k in data.get("keywords", [])
            )
        ):
            raise ValueError("Keywords must be a bounded list of strings")
        if (
            not isinstance(data.get("instructions", ""), str)
            or len(data.get("instructions", "")) > 10000
        ):
            raise ValueError("Instructions exceed size limit")
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise ValueError("Enabled must be a boolean")
        data.setdefault("enabled", True)
        data.update(
            source_ids=ids,
            source_id=ids[0] if ids else None,
            category_id=category_id,
            tag_ids=tag_ids,
        )
        old = self.store.get("subscription", data["id"]) if data.get("id") else None
        if old and body.get("revision") != old["revision"]:
            raise Conflict("订阅已更新，请刷新后重试")
        edges, cancel = self.subscription_unlinks(old, data) if old else ([], [])
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.put("subscription", data, body.get("revision"))
            for job_id in edges:
                self.store.db.execute(
                    "DELETE FROM job_subscriptions WHERE job_id=? AND subscription_id=?",
                    (job_id, row["id"]),
                )
            for job_id in cancel:
                self.store.db.execute(
                    "UPDATE jobs SET state='CANCELLED',error='Source unassigned',updated=? WHERE id=? AND state='QUEUED'",
                    (time.time(), job_id),
                )
                self.store.db.execute(
                    "UPDATE deliveries SET state='CANCELLED',error='Source unassigned',updated=? WHERE job_id=? AND state IN ('WAITING_CONTENT','READY','WAITING_CONTEXT','PAUSED')",
                    (time.time(), job_id),
                )
            self.store.audit("subscription", row["id"], "SAVED")
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return public_subscription(row, self.store.all("source"))

    def effective_model(self, sub, bot, w):
        key = (
            sub.get("model_id")
            or bot.get("model_id")
            or w.get("model_id")
            or self.store.setting("default_model")
        )
        return self.store.get("model", key) if key else None

    def ingest(self, source, rows, external=False, allow_historical=False):
        now = time.time()
        feed = self.store.db.execute(
            "SELECT * FROM feed_state WHERE source_id=?", (source["id"],)
        ).fetchone()
        baseline = not external and (not feed or not feed["baseline"])
        count = 0
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                version = self.sources.version(row, source)
                prior = self.store.db.execute(
                    "SELECT version FROM seen WHERE source_id=? AND item_key=?",
                    (source["id"], row["item_key"]),
                ).fetchone()
                self.store.db.execute(
                    "INSERT INTO seen VALUES(?,?,?) ON CONFLICT(source_id,item_key) DO UPDATE SET version=excluded.version",
                    (source["id"], row["item_key"], version),
                )
                if baseline or (prior and prior[0] == version):
                    continue
                if source["type"] == "rss" and not allow_historical:
                    ts = row.get("published_timestamp")
                    if (not ts and not source.get("allow_undated")) or (
                        ts and (ts < now - 86400 or ts > now + 300)
                    ):
                        continue
                event_id = "event-" + digest([source["id"], row["item_key"], version])[:24]
                payload = {
                    **row,
                    "source": source["name"],
                    "observed_at": now,
                    "source_snapshot": source,
                }
                self.store.db.execute(
                    "INSERT OR IGNORE INTO inputs VALUES(?,?,?,?,?,?)",
                    (
                        event_id,
                        source["id"],
                        row["item_key"],
                        version,
                        dumps(payload),
                        now,
                    ),
                )
                for sub in self.store.all("subscription"):
                    if not sub.get("enabled") or source["id"] not in resolved_source_ids(
                        sub, [source]
                    ):
                        continue
                    text = " ".join(
                        str(row.get(k, "")) for k in ("title", "summary", "text")
                    ).lower()
                    if sub.get("keywords") and not any(
                        str(k).lower() in text for k in sub["keywords"]
                    ):
                        continue
                    w = self.store.get("workspace", sub["workspace_id"])
                    for bot in self.store.all("bot"):
                        if not bot.get("enabled") or bot.get("workspace_id") != w["id"]:
                            continue
                        config = self.store.open(bot.get("secret"))
                        targets = [t["id"] for t in config.get("targets", []) if t.get("selected")]
                        if not targets:
                            continue
                        rules = rules_for(w, self.workspaces)
                        snapshot = {
                            "mode": sub["mode"],
                            "instructions": sub.get("instructions", ""),
                            "model": self.effective_model(sub, bot, w),
                            "rules": rules,
                            "workspace_profile": profile_for(w, self.workspaces),
                            "subscription_id": sub["id"],
                            "workspace": w,
                        }
                        hash_value = digest(
                            {
                                k: v
                                for k, v in snapshot.items()
                                if k not in ("subscription_id", "workspace")
                            }
                        )
                        job_id = "job-" + digest([event_id, w["id"], hash_value])[:24]
                        # Identical processing settings across subscriptions and bots share the same task.
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO jobs(id,event_id,workspace_id,config_hash,snapshot,state,created,updated) VALUES(?,?,?,?,?,'QUEUED',?,?)",
                            (
                                job_id,
                                event_id,
                                w["id"],
                                hash_value,
                                dumps(snapshot),
                                now,
                                now,
                            ),
                        )
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO job_subscriptions VALUES(?,?)",
                            (job_id, sub["id"]),
                        )
                        for target in targets:
                            did = "delivery-" + digest([job_id, bot["id"], target])[:24]
                            self.store.db.execute(
                                "INSERT OR IGNORE INTO deliveries(id,job_id,bot_id,target,binding_revision,state,created,updated) VALUES(?,?,?,?,?,'WAITING_CONTENT',?,?)",
                                (
                                    did,
                                    job_id,
                                    bot["id"],
                                    target,
                                    bot["binding_revision"],
                                    now,
                                    now,
                                ),
                            )
                count += 1
            self.store.db.execute(
                "INSERT INTO feed_state(source_id,baseline,last_check) VALUES(?,1,?) ON CONFLICT(source_id) DO UPDATE SET baseline=1,last_check=excluded.last_check,error=NULL",
                (source["id"], now),
            )
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        self.store.audit(
            "source",
            source["id"],
            "BASELINE" if baseline else "POLLED",
            {
                "items": len(rows),
                "new_events": count,
                "validation_replay": allow_historical,
            },
        )
        return {"items": len(rows), "new_events": count, "baseline_created": baseline}

    async def poll(self, key):
        async with self.poll_locks.setdefault(key, asyncio.Lock()):
            source = self.store.get("source", key)
            if source["type"] == "webhook":
                raise ValueError("Webhook 来源通过事件接入，不轮询")
            try:
                await asyncio.to_thread(self.policy.begin_poll, source)
                rows = await self.sources.discover(source)
                live = next((s for s in self.store.all("source") if s["id"] == key), None)
                if (
                    live is None
                    or not live.get("enabled")
                    or live["revision"] != source["revision"]
                ):
                    self.store.audit(
                        "source",
                        key,
                        "CHECK_CANCELLED",
                        {"reason": "Source removed, paused or changed during request"},
                    )
                    return {"cancelled": True}
                result = self.ingest(source, rows)
                interval = source["poll_minutes"] * 60
                offset = int(source.get("poll_offset", 0)) * 60 + int(source.get("poll_second", 0))
                next_poll = (
                    (math.floor((time.time() - offset) / interval) + 1) * interval
                    + offset
                    + random.randint(0, 15)
                )
                self.store.db.execute(
                    "UPDATE feed_state SET next_poll=? WHERE source_id=?",
                    (next_poll, key),
                )
                return result
            except DeferredRequest as exc:
                self.store.db.execute(
                    "INSERT INTO feed_state(source_id,next_poll,error) VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET next_poll=excluded.next_poll,error=excluded.error",
                    (
                        key,
                        exc.retry_at,
                        None if str(exc).startswith("Source interval") else str(exc),
                    ),
                )
                self.store.audit("source", key, "DEFERRED", {"retry_at": exc.retry_at})
                return {"deferred": True, "retry_at": exc.retry_at}
            except Exception as exc:
                error = self.safe_error(exc)
                self.store.db.execute(
                    "INSERT INTO feed_state(source_id,next_poll,last_check,error) VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET next_poll=excluded.next_poll,last_check=excluded.last_check,error=excluded.error",
                    (key, time.time() + 300, time.time(), error),
                )
                self.store.audit("source", key, "FAILED", {"error": error})
                raise

    async def process_one(self):
        row = self.store.db.execute(
            "SELECT * FROM jobs WHERE state='QUEUED' ORDER BY created LIMIT 1"
        ).fetchone()
        if not row:
            return False
        job = dict(row)
        now = time.time()
        self.store.db.execute(
            "UPDATE jobs SET state='RUNNING',attempts=attempts+1,updated=? WHERE id=?",
            (now, job["id"]),
        )
        self.store.audit("job", job["id"], "RUNNING")
        snapshot = json.loads(job["snapshot"])
        workspace = snapshot["workspace"]
        root = Path(workspace["path"])
        date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        paths = {
            k: f"{folder}/{date}/{job['id']}{suffix}"
            for k, folder, suffix in [
                ("event", "runs", ".json"),
                ("raw", "data/raw", ".md"),
                ("processed", "data/processed", ".md"),
                ("report", "reports", ".md"),
                ("note", "runs", ".md"),
                ("trace", "runs", "-trace.json"),
            ]
        }
        try:
            self.store.db.execute("UPDATE jobs SET paths=? WHERE id=?", (dumps(paths), job["id"]))
            for name in paths.values():
                self.workspaces.file(root, name).parent.mkdir(parents=True, exist_ok=True)
            input_row = self.store.db.execute(
                "SELECT * FROM inputs WHERE id=?", (job["event_id"],)
            ).fetchone()
            payload = json.loads(input_row["payload"])
            captured = payload.get("capture")
            if captured is None:
                captured = await self.sources.capture(payload, payload["source_snapshot"])
                # Cache in the input record and on disk; only this worker downloads bodies.
                cache = self.root / "state/cache/public" / job["event_id"]
                cache.mkdir(parents=True, exist_ok=True)
                if captured.get("html"):
                    (cache / "capture.html").write_text(captured.pop("html"), encoding="utf-8")
                (cache / "content.json").write_text(dumps(captured), encoding="utf-8")
                payload["capture"] = captured
                self.store.db.execute(
                    "UPDATE inputs SET payload=? WHERE id=?",
                    (dumps(payload), job["event_id"]),
                )
                self.store.audit("input", job["event_id"], "CAPTURED")
            self.workspaces.file(root, paths["raw"]).write_text(captured["text"], encoding="utf-8")
            event = {
                **{k: v for k, v in captured.items() if k not in ("text", "source_snapshot")},
                "event_id": job["event_id"],
                "job_id": job["id"],
                "raw_path": paths["raw"],
                "input_sha256": digest(captured["text"].encode()),
                "outputs": {k: paths[k] for k in ("processed", "report", "note")},
            }
            self.workspaces.file(root, paths["event"]).write_text(dumps(event), encoding="utf-8")
            if snapshot["mode"] == "agent":
                text = await self.editor.run(workspace, snapshot, paths)
            else:
                content = captured["text"]
                title = captured.get("title", "订阅更新")
                source = captured.get("source", payload["source"])
                # Rules mode applies configured content limits and field layout without an LLM.
                budget = 5000 if snapshot["mode"] == "forward" else 1200
                text = (
                    f"# {title}\n\n"
                    + ("【来源摘要】\n" if captured.get("content_level") == "summary_only" else "")
                    + content[:budget]
                    + "\n\n来源："
                    + source
                    + ("\n" + captured["url"] if captured.get("url") else "")
                )
                self.workspaces.file(root, paths["processed"]).write_text(
                    "# 处理结果\n\n原始输入：" + paths["raw"] + "\n\n" + content,
                    encoding="utf-8",
                )
                self.workspaces.file(root, paths["report"]).write_text(text, encoding="utf-8")
                self.workspaces.file(root, paths["trace"]).write_text(
                    dumps(
                        {
                            "mode": snapshot["mode"],
                            "model": None,
                            "tools": [],
                            "rules": snapshot.get("rules", {}),
                            "instructions": snapshot.get("instructions", ""),
                            "input_sha256": event["input_sha256"],
                        }
                    ),
                    encoding="utf-8",
                )
                self.workspaces.file(root, paths["note"]).write_text(
                    "Mode: "
                    + snapshot["mode"]
                    + "\nInput SHA256: "
                    + event["input_sha256"]
                    + "\nNo model invoked.\n",
                    encoding="utf-8",
                )
            state = "RENDERED" if text else "SKIPPED"
            self.store.db.execute(
                "UPDATE jobs SET state=?,updated=? WHERE id=?",
                (state, time.time(), job["id"]),
            )
            self.store.db.execute(
                "UPDATE deliveries SET state=?,updated=? WHERE job_id=? AND state='WAITING_CONTENT'",
                ("READY" if text else "SKIPPED", time.time(), job["id"]),
            )
            self.store.audit("job", job["id"], state)
        except Exception as exc:
            error = self.safe_error(exc)
            self.store.db.execute(
                "UPDATE jobs SET state='FAILED',error=?,updated=? WHERE id=?",
                (error, time.time(), job["id"]),
            )
            self.store.db.execute(
                "UPDATE deliveries SET state='FAILED',error=?,updated=? WHERE job_id=? AND state='WAITING_CONTENT'",
                (error, time.time(), job["id"]),
            )
            self.store.audit("job", job["id"], "FAILED", {"error": error})
        return True

    @staticmethod
    def split_text(text, limit):
        parts = []
        current = ""
        units = 0
        for char in text:
            n = 2 if ord(char) > 0xFFFF else 1
            if units + n > limit:
                parts.append(current)
                current = ""
                units = 0
            current += char
            units += n
        if current:
            parts.append(current)
        if len(parts) > 12:
            raise ValueError("Report exceeds delivery part budget")
        return parts

    @staticmethod
    def split_text_bytes(text, limit):
        parts = []
        current = ""
        used = 0
        for char in text:
            size = len(char.encode("utf-8"))
            if used + size > limit:
                parts.append(current)
                current = ""
                used = 0
            current += char
            used += size
        if current:
            parts.append(current)
        if len(parts) > 12:
            raise ValueError("Report exceeds delivery part budget")
        return parts

    async def deliver_one(self):
        row = self.store.db.execute(
            "SELECT * FROM deliveries WHERE state IN ('READY','PAUSED','WAITING_CONTEXT') AND next_try<=? ORDER BY created LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not row:
            return False
        d = dict(row)
        b = self.store.get("bot", d["bot_id"])

        def state(value, error=None, next_try=0):
            self.store.db.execute(
                "UPDATE deliveries SET state=?,error=?,next_try=?,updated=? WHERE id=?",
                (value, error, next_try, time.time(), d["id"]),
            )
            self.store.audit("delivery", d["id"], value, {"error": error} if error else {})

        if b["binding_revision"] != d["binding_revision"]:
            state("CANCELLED", "Target or workspace binding changed")
            return True
        if not b["enabled"]:
            state("PAUSED", None, time.time() + 15)
            return True
        c = self.store.open(b["secret"])
        if b["status"] not in ("CONNECTED", "READY") or (
            b["type"] == "weixin" and not c.get("contexts", {}).get(d["target"])
        ):
            state("WAITING_CONTEXT", "等待账号连接或接收会话上下文", time.time() + 30)
            return True
        job = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (d["job_id"],)).fetchone()
        try:
            snapshot = json.loads(job["snapshot"])
            paths = json.loads(job["paths"])
            text = self.workspaces.file(snapshot["workspace"]["path"], paths["report"]).read_text(
                encoding="utf-8"
            )
            capability = self.channels.info(b["type"])
            parts = (
                self.split_text_bytes(text, capability["byte_limit"] - 128)
                if capability.get("byte_limit")
                else self.split_text(text, capability["limit"] - 64)
            )
            receipts = json.loads(d["receipts"])
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            state("FAILED", self.safe_error(exc))
            return True
        state("SENDING")
        self.store.db.execute("UPDATE deliveries SET attempts=attempts+1 WHERE id=?", (d["id"],))
        try:
            for index in range(d["part_index"], len(parts)):
                body = (
                    parts[index]
                    if len(parts) == 1
                    else f"[{d['job_id'][-6:]} {index + 1}/{len(parts)}]\n" + parts[index]
                )
                receipt = await self.channels.send(
                    b["type"], c, d["target"], body, d["id"] + "-" + str(index)
                )
                receipts.append({"part": index, **receipt, "at": time.time()})
                self.store.db.execute(
                    "UPDATE deliveries SET part_index=?,receipts=?,updated=? WHERE id=?",
                    (index + 1, dumps(receipts), time.time(), d["id"]),
                )
            state("ADAPTER_ACCEPTED")
        except ContextRequired as exc:
            state("WAITING_CONTEXT", str(exc), time.time() + 60)
        except RemoteError as exc:
            if exc.status == 429:
                state(
                    "READY",
                    "平台限流",
                    time.time() + max(60, retry_after(exc.retry_after, time.time())),
                )
            elif 400 <= exc.status < 500:
                state("FAILED", str(exc))
            else:
                state("UNKNOWN", "平台服务错误，发送结果无法确认")
        except PlatformRejected as exc:
            state("FAILED", str(exc))
        except (OSError, TimeoutError, asyncio.CancelledError):
            state("UNKNOWN", "发送中断或超时，不自动重发")
            if asyncio.current_task().cancelling():
                raise
        except Exception as exc:
            state("UNKNOWN", self.safe_error(exc))
        return True

    async def receiver(self, key):
        try:
            while self.running:
                b = self.store.get("bot", key)
                if (
                    not b.get("enabled")
                    or b["type"] not in ("weixin", "telegram")
                    or b["status"]
                    in ("WAITING_QR", "QR_EXPIRED", "CONNECTION_FAILED", "CONNECTING")
                ):
                    return
                try:
                    c = await self.channels.receive(b["type"], self.store.open(b["secret"]))
                    latest = self.store.get("bot", key)
                    # Don't overwrite a target selection or new login made while this request was in flight.
                    if latest["revision"] != b["revision"]:
                        await asyncio.sleep(1)
                        continue
                    selected = [t for t in c.get("targets", []) if t.get("selected")]
                    status = "CONNECTED" if selected else "WAITING_TARGET"
                    b.update(
                        secret=self.store.seal(c),
                        status=status,
                        error=None,
                        last_connected=time.time(),
                    )
                    self.store.put("bot", b)
                except Exception as exc:
                    b = self.store.get("bot", key)
                    b.update(status="RECONNECTING", error=self.safe_error(exc))
                    self.store.put("bot", b)
                    await asyncio.sleep(10)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def loop(self, operation, delay):
        while self.running:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.audit("system", "worker", "ERROR", {"type": type(exc).__name__})
            await asyncio.sleep(delay)

    async def tick(self):
        for b in self.store.all("bot"):
            if b.get("workspace_id") and b.get("enabled") and b["type"] in ("weixin", "telegram"):
                task = self.receivers.get(b["id"])
                if task is None or task.done():
                    self.receivers[b["id"]] = asyncio.create_task(self.receiver(b["id"]))
        sources = self.store.all("source")
        active = {
            source_id
            for s in self.store.all("subscription")
            if s.get("enabled")
            and any(
                b.get("enabled") and b.get("workspace_id") == s["workspace_id"]
                for b in self.store.all("bot")
            )
            for source_id in resolved_source_ids(s, sources)
        }
        tasks = []
        for source in sources:
            if (
                not source.get("enabled")
                or source["id"] not in active
                or source["type"] == "webhook"
            ):
                continue
            row = self.store.db.execute(
                "SELECT next_poll FROM feed_state WHERE source_id=?", (source["id"],)
            ).fetchone()
            if not row or row[0] <= time.time():
                tasks.append(source["id"])
        semaphore = asyncio.Semaphore(4)

        async def check(key):
            async with semaphore:
                try:
                    await self.poll(key)
                except Exception:
                    pass

        if tasks:
            await asyncio.gather(*(check(k) for k in tasks))

    async def start(self):
        import os

        self.lock_file = (self.root / "state/worker.lock").open("a+b")
        if self.lock_file.tell() == 0:
            self.lock_file.write(b"0")
            self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError("Another worker owns this state directory")
        self.store.recover()
        for workspace in self.store.all("workspace"):
            if "ai_profile" not in workspace:
                workspace["ai_profile"] = profile_for(workspace, self.workspaces)
                self.store.put("workspace", workspace, workspace["revision"])
                self.store.audit("workspace", workspace["id"], "PROFILE_MIGRATED")
        self.tasks = [
            asyncio.create_task(self.loop(self.tick, 3)),
            asyncio.create_task(self.loop(self.process_one, 1)),
            asyncio.create_task(self.loop(self.deliver_one, 0.5)),
        ]

    async def stop(self):
        self.running = False
        for t in self.tasks + list(self.receivers.values()):
            t.cancel()
        await asyncio.gather(*self.tasks, *self.receivers.values(), return_exceptions=True)
        self.store.db.close()
        if hasattr(self, "lock_file"):
            self.lock_file.close()

    def overview(self):
        def counts(table):
            return {
                r[0]: r[1]
                for r in self.store.db.execute(
                    "SELECT state,COUNT(*) FROM " + table + " GROUP BY state"
                )
            }

        return {
            "bots": [self.public_bot(b) for b in self.store.all("bot")],
            "workspaces": [
                {**row, "ai_profile": profile_for(row, self.workspaces)}
                for row in self.store.all("workspace")
            ],
            "sources": [self.public_source(s) for s in self.store.all("source")],
            "subscriptions": [
                public_subscription(s, self.store.all("source"))
                for s in self.store.all("subscription")
            ],
            "categories": self.store.all("category"),
            "tags": self.store.all("tag"),
            "models": [self.public_model(m) for m in self.store.all("model")],
            "platforms": CATALOG,
            "network": self.proxy_preferences(),
            "stats": {
                "inputs": self.store.db.execute("SELECT COUNT(*) FROM inputs").fetchone()[0],
                "jobs": counts("jobs"),
                "deliveries": counts("deliveries"),
            },
            "default_model": self.store.setting("default_model"),
        }
