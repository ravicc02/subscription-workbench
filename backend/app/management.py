"""Revision-checked bulk configuration management; local files and history are retained."""

import json
import time

from .core import Conflict
from .profiles import (
    conversation_profile_for,
    profile_for,
    validate_conversation_profile,
    validate_profile,
)
from .subscriptions import resolved_source_ids, source_ids


class Management:
    def workspace_settings(self, key):
        w = self.store.get("workspace", key)
        return {
            **w,
            "ai_profile": profile_for(w, self.workspaces),
            "conversation_profile": conversation_profile_for(w),
        }

    def rename_workspace(self, key, body):
        workspace = self.store.get("workspace", key)
        if body.get("revision") != workspace["revision"]:
            raise Conflict("工作区已更新，请刷新后重试")
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("工作区名称为 1～100 个字符")
        previous = workspace["name"]
        workspace["name"] = name.strip()
        result = self.store.put("workspace", workspace, workspace["revision"])
        self.store.audit(
            "workspace", key, "RENAMED", {"previous": previous, "name": result["name"]}
        )
        return result

    def save_workspace_settings(self, key, body):
        w = self.store.get("workspace", key)
        if body.get("revision") != w["revision"]:
            raise Conflict("工作区设置已更新，请刷新后重试")
        name = body.get("name", w["name"])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("工作区名称为 1～100 个字符")
        model_id = body.get("model_id", w.get("model_id"))
        if model_id:
            self.store.get("model", model_id)
        conversation_model_id = body.get("conversation_model_id", w.get("conversation_model_id"))
        if conversation_model_id:
            self.store.get("model", conversation_model_id)
        w.update(
            name=name.strip(),
            model_id=model_id,
            conversation_model_id=conversation_model_id,
            conversation_profile=validate_conversation_profile(
                body.get("conversation_profile", conversation_profile_for(w))
            ),
            ai_profile=validate_profile(body.get("ai_profile", profile_for(w, self.workspaces))),
        )
        saved = self.store.put("workspace", w, w["revision"])
        self.store.audit("workspace", key, "AI_SETTINGS_SAVED", {"revision": saved["revision"]})
        return saved

    def subscription_unlinks(self, old, new):
        sources = self.store.all("source")
        new_sources = set(resolved_source_ids(new, sources)) if new.get("enabled") else set()
        removed = set(resolved_source_ids(old, sources)) - new_sources
        if old["workspace_id"] != new["workspace_id"]:
            removed = set(resolved_source_ids(old, sources))
        edges = []
        cancel = []
        if not removed:
            return edges, cancel
        for row in self.store.db.execute(
            "SELECT jobs.*,inputs.source_id AS origin_source FROM jobs JOIN job_subscriptions js ON js.job_id=jobs.id JOIN inputs ON inputs.id=jobs.event_id WHERE js.subscription_id=?",
            (old["id"],),
        ):
            job = dict(row)
            if job["origin_source"] not in removed:
                continue
            edges.append(job["id"])
            active = {s["id"] for s in self.store.all("subscription") if s.get("enabled")}
            others = any(
                member[0] in active
                for member in self.store.db.execute(
                    "SELECT subscription_id FROM job_subscriptions WHERE job_id=? AND subscription_id!=?",
                    (job["id"], old["id"]),
                )
            )
            if others:
                continue
            if (
                job["state"] == "RUNNING"
                or self.store.db.execute(
                    "SELECT 1 FROM deliveries WHERE job_id=? AND state='SENDING'", (job["id"],)
                ).fetchone()
            ):
                raise Conflict("被移除来源的内容正在处理或发送，请完成后再修改订阅")
            cancel.append(job["id"])
        return edges, cancel

    def source_subscription_unlinks(self, old, new):
        removed = {
            sub["id"]
            for sub in self.store.all("subscription")
            if old["id"] in resolved_source_ids(sub, [old])
            and (not new.get("enabled") or old["id"] not in resolved_source_ids(sub, [new]))
        }
        if not removed:
            return [], []
        edges, cancel = [], []
        for row in self.store.db.execute(
            "SELECT jobs.* FROM jobs JOIN inputs ON inputs.id=jobs.event_id "
            "WHERE inputs.source_id=?",
            (old["id"],),
        ):
            job = dict(row)
            members = {
                member[0]
                for member in self.store.db.execute(
                    "SELECT subscription_id FROM job_subscriptions WHERE job_id=?", (job["id"],)
                )
            }
            unlinked = members & removed
            if not unlinked:
                continue
            edges.extend((job["id"], sub_id) for sub_id in unlinked)
            if members - removed:
                continue
            if (
                job["state"] == "RUNNING"
                or self.store.db.execute(
                    "SELECT 1 FROM deliveries WHERE job_id=? AND state='SENDING'", (job["id"],)
                ).fetchone()
            ):
                raise Conflict("该来源的内容正在处理或发送，请完成后再修改分类标签")
            cancel.append(job["id"])
        return edges, cancel

    def cancel_pending_jobs(self, job_ids, reason):
        cancelled = 0
        for job_id in set(job_ids):
            self.store.db.execute(
                "UPDATE jobs SET state='CANCELLED',error=?,updated=? WHERE id=? AND state='QUEUED'",
                (reason, time.time(), job_id),
            )
            cancelled += self.store.db.execute(
                "UPDATE deliveries SET state='CANCELLED',error=?,updated=? "
                "WHERE job_id=? AND state IN ('WAITING_CONTENT','READY','WAITING_CONTEXT','PAUSED')",
                (reason, time.time(), job_id),
            ).rowcount
        return cancelled

    def manage(self, body, preview=False):
        kind = body.get("kind")
        action = body.get("action")
        items = body.get("items")
        if kind not in ("bot", "source", "subscription", "workspace", "model") or action not in (
            "delete",
            "enable",
            "disable",
        ):
            raise ValueError("批量操作类型无效")
        if action != "delete" and kind not in ("bot", "source", "subscription"):
            raise ValueError("该类型不支持启停操作")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("一次请选择 1～100 个项目")
        objects = []
        seen = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("项目列表格式无效")
            if item["id"] in seen:
                raise ValueError("项目重复")
            row = self.store.get(kind, item["id"])
            if item.get("revision") != row["revision"]:
                raise Conflict("所选配置已更新，请刷新后重试")
            seen.add(row["id"])
            objects.append(row)
        selected = {r["id"] for r in objects}
        subscriptions = self.store.all("subscription")
        bots = self.store.all("bot")
        deleting_subs = set(selected if kind == "subscription" and action == "delete" else [])
        trimmed_subs = []
        if action == "delete" and kind == "source":
            for sub in subscriptions:
                remaining = [key for key in source_ids(sub) if key not in selected]
                if len(remaining) == len(source_ids(sub)):
                    continue
                if not remaining:
                    deleting_subs.add(sub["id"])
                else:
                    trimmed_subs.append({**sub, "source_ids": remaining, "source_id": remaining[0]})
        elif action == "delete" and kind == "workspace":
            deleting_subs |= {sub["id"] for sub in subscriptions if sub["workspace_id"] in selected}
        if action == "delete" and kind == "workspace":
            bound = [b for b in bots if b.get("workspace_id") in selected]
            if bound:
                raise Conflict(
                    "工作区仍绑定机器人，请先改绑或删除机器人："
                    + "、".join(b["name"] for b in bound)[:180]
                )
        jobs = []
        if action == "delete":
            for row in self.store.db.execute(
                "SELECT jobs.*,inputs.source_id AS origin_source FROM jobs LEFT JOIN inputs ON inputs.id=jobs.event_id"
            ):
                j = dict(row)
                snapshot = json.loads(j["snapshot"])
                members = {
                    x[0]
                    for x in self.store.db.execute(
                        "SELECT subscription_id FROM job_subscriptions WHERE job_id=?", (j["id"],)
                    )
                }
                affected = (
                    (kind == "source" and j["origin_source"] in selected)
                    or (kind == "workspace" and j["workspace_id"] in selected)
                    or (kind == "model" and (snapshot.get("model") or {}).get("id") in selected)
                    or (bool(members & deleting_subs) and not (members - deleting_subs))
                )
                if affected:
                    jobs.append(j)
            running = [j for j in jobs if j["state"] == "RUNNING"]
            if running:
                raise Conflict("相关内容正在整理，请完成后再删除")
            if kind == "bot":
                sending = self.store.db.execute(
                    "SELECT bot_id FROM deliveries WHERE state='SENDING'"
                ).fetchall()
                blocked = (
                    any(r[0] in selected for r in sending)
                    or self.store.db.execute(
                        "SELECT 1 FROM inbound_messages WHERE bot_id IN ("
                        + ",".join("?" for _ in selected)
                        + ") AND state IN ('RUNNING','SENDING') LIMIT 1",
                        tuple(selected),
                    ).fetchone()
                )
            else:
                affected_ids = {j["id"] for j in jobs}
                blocked = any(
                    r[0] in affected_ids
                    for r in self.store.db.execute(
                        "SELECT job_id FROM deliveries WHERE state='SENDING'"
                    )
                )
            if blocked:
                raise Conflict("相关消息正在发送，请完成后再删除")
        job_ids = {j["id"] for j in jobs}
        pending = (
            [
                dict(d)
                for d in self.store.db.execute(
                    "SELECT id,job_id,bot_id FROM deliveries WHERE state IN ('WAITING_CONTENT','READY','PAUSED','WAITING_CONTEXT')"
                )
                if d["job_id"] in job_ids or (kind == "bot" and d["bot_id"] in selected)
            ]
            if action == "delete"
            else []
        )
        refs = []
        if action == "delete" and kind == "model":
            refs = [
                (k, o)
                for k in ("workspace", "bot", "subscription")
                for o in self.store.all(k)
                if o.get("model_id") in selected or o.get("conversation_model_id") in selected
            ]
            if self.store.db.execute(
                "SELECT 1 FROM inbound_messages WHERE state IN ('RUNNING','SENDING') "
                "AND model_id IN (" + ",".join("?" for _ in selected) + ") LIMIT 1",
                tuple(selected),
            ).fetchone():
                raise Conflict("对话正在处理或发送，请完成后再删除模型")
        impact = {
            "count": len(objects),
            "names": [o["name"] for o in objects],
            "related_subscriptions": len(
                deleting_subs - (selected if kind == "subscription" else set())
            ),
            "updated_subscriptions": len(trimmed_subs),
            "pending_deliveries": len(pending),
            "model_bindings": len(refs),
            "files_retained": True,
            "history_retained": True,
        }
        if preview:
            return impact
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            if action in ("enable", "disable"):
                for row in objects:
                    if (
                        action == "disable"
                        and row.get("enabled")
                        and kind in ("source", "subscription")
                    ):
                        if kind == "source":
                            edges, cancel = self.source_subscription_unlinks(
                                row, {**row, "enabled": False}
                            )
                            for job_id, sub_id in edges:
                                self.store.db.execute(
                                    "DELETE FROM job_subscriptions WHERE job_id=? AND subscription_id=?",
                                    (job_id, sub_id),
                                )
                        else:
                            edges, cancel = self.subscription_unlinks(
                                row, {**row, "enabled": False}
                            )
                            for job_id in edges:
                                self.store.db.execute(
                                    "DELETE FROM job_subscriptions WHERE job_id=? AND subscription_id=?",
                                    (job_id, row["id"]),
                                )
                        impact["pending_deliveries"] += self.cancel_pending_jobs(
                            cancel, "Subscription or source paused"
                        )
                    row["enabled"] = action == "enable"
                    self.store.put(kind, row, row["revision"])
            else:
                for sub in trimmed_subs:
                    self.store.put("subscription", sub, sub["revision"])
                if kind == "model":
                    remaining = [m for m in self.store.all("model") if m["id"] not in selected]
                    default = self.store.setting("default_model")
                    if default in selected:
                        default = remaining[0]["id"] if remaining else None
                        self.store.set_setting("default_model", default)
                    if self.store.setting("conversation_model_id") in selected:
                        self.store.set_setting("conversation_model_id", None)
                    for ref_kind, row in refs:
                        if row.get("model_id") in selected:
                            row["model_id"] = None
                        if row.get("conversation_model_id") in selected:
                            row["conversation_model_id"] = None
                        self.store.put(ref_kind, row, row["revision"])
                for j in jobs:
                    if j["state"] == "QUEUED":
                        self.store.db.execute(
                            "UPDATE jobs SET state='CANCELLED',error='Subscription configuration removed',updated=? WHERE id=?",
                            (time.time(), j["id"]),
                        )
                for d in pending:
                    self.store.db.execute(
                        "UPDATE deliveries SET state='CANCELLED',error='Configuration removed',updated=? WHERE id=?",
                        (time.time(), d["id"]),
                    )
                if kind == "bot":
                    self.store.db.execute(
                        "UPDATE inbound_messages SET state='CANCELLED',error='Bot removed',updated=? "
                        "WHERE bot_id IN (" + ",".join("?" for _ in selected) + ") "
                        "AND state IN ('QUEUED','READY','PAUSED','WAITING_CONTEXT')",
                        (time.time(), *selected),
                    )
                for key in deleting_subs:
                    self.store.db.execute(
                        "DELETE FROM job_subscriptions WHERE subscription_id=?", (key,)
                    )
                    self.store.db.execute(
                        "DELETE FROM objects WHERE kind='subscription' AND id=?", (key,)
                    )
                for row in objects:
                    self.store.db.execute(
                        "DELETE FROM objects WHERE kind=? AND id=?", (kind, row["id"])
                    )
            self.store.audit(kind, "batch", action.upper(), impact)
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        if kind == "bot" and action in ("delete", "disable"):
            for key in selected:
                task = self.receivers.pop(key, None)
                if task:
                    task.cancel()
        return impact
