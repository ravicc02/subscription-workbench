"""Durable manual source checks using the ordinary poll and rate-limit path."""

import asyncio
import time

from .core import Conflict, uid


class SourceChecks:
    MAX_SOURCES = 500

    def create_source_check(self, body):
        scope = body.get("scope")
        if scope not in ("all", "selected"):
            raise ValueError("检查范围无效")
        sources = self.store.all("source")
        by_id = {source["id"]: source for source in sources}
        if scope == "all":
            if body.get("source_ids"):
                raise ValueError("检查全部时不需要指定来源")
            requested = sources
        else:
            ids = body.get("source_ids")
            if (
                not isinstance(ids, list)
                or not 1 <= len(ids) <= self.MAX_SOURCES
                or any(not isinstance(key, str) or key not in by_id for key in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("请选择不重复的已有来源")
            requested = [by_id[key] for key in ids]
        eligible = [
            source for source in requested if source.get("enabled") and source["type"] != "webhook"
        ]
        if not eligible or len(eligible) > self.MAX_SOURCES:
            raise ValueError("可检查的来源数量应为 1～500")
        now = time.time()
        key = uid("source-check")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            active = self.store.db.execute(
                "SELECT id FROM source_check_batches WHERE state IN ('QUEUED','RUNNING') LIMIT 1"
            ).fetchone()
            if active:
                raise Conflict("已有检查任务进行中，请查看任务进度")
            self.store.db.execute(
                "INSERT INTO source_check_batches(id,scope,state,total,skipped,created,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (key, scope, "QUEUED", len(eligible), len(requested) - len(eligible), now, now),
            )
            self.store.db.executemany(
                "INSERT INTO source_check_items(batch_id,ordinal,source_id,source_name,"
                "source_revision,state,updated) VALUES(?,?,?,?,?,'QUEUED',?)",
                [(key, i, s["id"], s["name"], s["revision"], now) for i, s in enumerate(eligible)],
            )
            self.store.audit("source_check", key, "QUEUED", {"total": len(eligible)})
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return self.source_check(key)

    def list_source_checks(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("最多读取 20 条检查记录")
        keys = [
            row[0]
            for row in self.store.db.execute(
                "SELECT id FROM source_check_batches ORDER BY created DESC LIMIT ?", (limit,)
            )
        ]
        return {"batches": [self.source_check(key, include_items=False) for key in keys]}

    def source_check(self, key, include_items=True):
        batch = self.store.db.execute(
            "SELECT * FROM source_check_batches WHERE id=?", (key,)
        ).fetchone()
        if not batch:
            raise ValueError("检查任务不存在")
        counts = {
            row["state"]: row["total"]
            for row in self.store.db.execute(
                "SELECT state,COUNT(*) total FROM source_check_items WHERE batch_id=? GROUP BY state",
                (key,),
            )
        }
        new_events = self.store.db.execute(
            "SELECT COALESCE(SUM(new_events),0) FROM source_check_items WHERE batch_id=?",
            (key,),
        ).fetchone()[0]
        done = batch["total"] - counts.get("QUEUED", 0) - counts.get("RUNNING", 0)
        warning = any(
            counts.get(state) for state in ("FAILED", "DEFERRED", "CANCELLED", "INTERRUPTED")
        )
        if batch["state"] in ("QUEUED", "RUNNING"):
            summary = f"已完成 {done}/{batch['total']} 个来源检查"
        else:
            summary = (
                f"已检查 {batch['total']} 个来源：完成 {counts.get('OK', 0) + counts.get('BASELINE', 0)}，"
                f"发现 {new_events} 个新事件；等待间隔或冷却 {counts.get('DEFERRED', 0)}，"
                f"失败 {counts.get('FAILED', 0)}，取消 {counts.get('CANCELLED', 0)}，"
                f"中断待核实 {counts.get('INTERRUPTED', 0)}。"
            )
        result = {
            **dict(batch),
            "stop_requested": bool(batch["stop_requested"]),
            "done": done,
            "counts": counts,
            "new_events": new_events,
            "warning": warning or batch["state"] == "CANCELLED",
            "summary": summary,
        }
        if include_items:
            result["rows"] = [
                dict(row)
                for row in self.store.db.execute(
                    "SELECT ordinal,source_id id,source_name name,state,items,new_events,"
                    "retry_at,detail,updated FROM source_check_items "
                    "WHERE batch_id=? ORDER BY ordinal",
                    (key,),
                )
            ]
        return result

    def stop_source_check(self, key):
        batch = self.source_check(key, include_items=False)
        if batch["state"] in ("QUEUED", "RUNNING"):
            now = time.time()
            self.store.db.execute(
                "UPDATE source_check_batches SET stop_requested=1,updated=? WHERE id=?",
                (now, key),
            )
            self.store.db.execute(
                "UPDATE source_check_items SET state='CANCELLED',detail='已停止后续检查',updated=? "
                "WHERE batch_id=? AND state='QUEUED'",
                (now, key),
            )
            running = self.store.db.execute(
                "SELECT 1 FROM source_check_items WHERE batch_id=? AND state='RUNNING' LIMIT 1",
                (key,),
            ).fetchone()
            if not running:
                self.store.db.execute(
                    "UPDATE source_check_batches SET state='CANCELLED',updated=? WHERE id=?",
                    (now, key),
                )
            self.store.audit("source_check", key, "STOP_REQUESTED")
        return self.source_check(key)

    async def source_check_one(self):
        async with self.source_check_lock:
            batch = self.store.db.execute(
                "SELECT id FROM source_check_batches WHERE state='QUEUED' ORDER BY created LIMIT 1"
            ).fetchone()
            if not batch:
                return False
            key = batch["id"]
            self.store.db.execute(
                "UPDATE source_check_batches SET state='RUNNING',updated=? WHERE id=?",
                (time.time(), key),
            )
            self.store.audit("source_check", key, "RUNNING")

            async def worker():
                while True:
                    if self.store.db.execute(
                        "SELECT stop_requested FROM source_check_batches WHERE id=?", (key,)
                    ).fetchone()[0]:
                        return
                    item = self.store.db.execute(
                        "SELECT * FROM source_check_items WHERE batch_id=? AND state='QUEUED' "
                        "ORDER BY ordinal LIMIT 1",
                        (key,),
                    ).fetchone()
                    if not item:
                        return
                    ordinal = item["ordinal"]
                    self.store.db.execute(
                        "UPDATE source_check_items SET state='RUNNING',updated=? "
                        "WHERE batch_id=? AND ordinal=?",
                        (time.time(), key, ordinal),
                    )
                    try:
                        source = self.store.get("source", item["source_id"])
                        if (
                            not source.get("enabled")
                            or source["type"] == "webhook"
                            or source["revision"] != item["source_revision"]
                        ):
                            result = ("CANCELLED", None, None, None, "来源已修改或暂停")
                        else:
                            value = await self.poll(source["id"])
                            if value.get("deferred"):
                                result = (
                                    "DEFERRED",
                                    None,
                                    None,
                                    value["retry_at"],
                                    "等待间隔或站点冷却",
                                )
                            elif value.get("cancelled"):
                                result = ("CANCELLED", None, None, None, "检查期间来源已变更")
                            else:
                                result = (
                                    "BASELINE" if value.get("baseline_created") else "OK",
                                    value["items"],
                                    value["new_events"],
                                    None,
                                    "首次建立基线，不补发历史"
                                    if value.get("baseline_created")
                                    else "检查完成",
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        result = ("FAILED", None, None, None, self.safe_error(exc))
                    self.store.db.execute(
                        "UPDATE source_check_items SET state=?,items=?,new_events=?,retry_at=?,"
                        "detail=?,updated=? WHERE batch_id=? AND ordinal=?",
                        (*result, time.time(), key, ordinal),
                    )
                    self.store.db.execute(
                        "UPDATE source_check_batches SET updated=? WHERE id=?", (time.time(), key)
                    )

            await asyncio.gather(*(worker() for _ in range(3)))
            stopped = self.store.db.execute(
                "SELECT stop_requested FROM source_check_batches WHERE id=?", (key,)
            ).fetchone()[0]
            self.store.db.execute(
                "UPDATE source_check_batches SET state=?,updated=? WHERE id=?",
                ("CANCELLED" if stopped else "COMPLETED", time.time(), key),
            )
            self.store.audit("source_check", key, "CANCELLED" if stopped else "COMPLETED")
            return True
