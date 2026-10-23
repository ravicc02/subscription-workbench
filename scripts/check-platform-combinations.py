"""Retained, real-platform routing acceptance against a running Workbench.

Creates visibly named test-only objects via HTTP, emits synthetic webhooks, and
checks persisted model traces and delivery receipts. Does not print credentials.
"""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.core import Store

BOT_IDS = (
    "bot-d94c833f5aee4789",
    "bot-69f6036462234fc4",
    "bot-fe9ebced7ebc4ca6",
)
CASES = (
    "shared_single_agent",
    "independent_same_agent",
    "independent_distinct_agent",
    "shared_multi_agent",
    "shared_overlapping_subs",
    "independent_mixed_modes",
    "shared_filter_and_duplicate",
    "shared_pause_resume",
)
MARKER = "【系统验收测试，非真实资讯】"
PROFILE = {
    "focus_prompt": "只处理明确标记的系统验收合成事件，不是市场新闻。",
    "excluded_topics": "不得补充实时行情、新闻、价格、投资建议或输入以外的事实。",
    "processing_prompt": "保留场景码、工作区标记和输入中的编号；仅归纳输入的路由验收事实，不跳过有效事件。",
    "output_template": MARKER + "\n场景码：\n工作区标记：\n测试事实：\n来源：",
    "language": "中文",
    "additional_instructions": "这只是路由、模型处理与投递的验收，绝不描述成真实资讯。",
}
EXPECTED = {
    "shared_single_agent": (1, 3, 1),
    "independent_same_agent": (3, 3, 3),
    "independent_distinct_agent": (3, 3, 3),
    "shared_multi_agent": (2, 6, 2),
    "shared_overlapping_subs": (1, 3, 1),
    "independent_mixed_modes": (3, 3, 1),
    "shared_filter_and_duplicate": (1, 3, 1),
    "shared_pause_resume": (1, 3, 1),
}


class Acceptance:
    def __init__(self, cases, url):
        self.cases = cases
        self.store = Store(ROOT)
        self.original = {key: self.store.get("bot", key)["workspace_id"] for key in BOT_IDS}
        self.output = ROOT / "runs/verification" / (
            "platform-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        self.output.mkdir(parents=True, exist_ok=False)
        self.client = httpx.Client(base_url=url, timeout=30, trust_env=False)
        # Local administrator session; all configuration still goes through the live API.
        self.client.cookies.set("wb_session", self.store.session())
        self.client.headers["X-Requested-With"] = "workbench"
        self.results = []
        self.current_sources = []
        self.current_subs = []
        self.active_case = None
        self.save()

    def api(self, method, path, body=None, hook_token=None):
        headers = {"Authorization": "Bearer " + hook_token} if hook_token else {}
        response = self.client.request(method, "/api/" + path, json=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code} {response.text[:240]}")
        return response.json()

    def overview(self):
        return self.api("GET", "overview")

    def save(self):
        (self.output / "summary.json").write_text(
            json.dumps(
                {
                    "running_case": self.active_case,
                    "original_bindings": self.original,
                    "cases": self.results,
                    "limits": [
                        "ADAPTER_ACCEPTED only confirms the platform accepted a send call; it does not prove human receipt/read.",
                        "Forward and rules modes intentionally do not call the model; a negative filter intentionally makes no job.",
                        "Shared workspace subscriptions target all enabled bots bound to it; per-bot different source sets require separate workspaces.",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def bot_update(self, key, **changes):
        for _ in range(15):
            current = next(b for b in self.overview()["bots"] if b["id"] == key)
            try:
                return self.api(
                    "POST", f"bots/{key}/update", {"revision": current["revision"], **changes}
                )
            except RuntimeError as exc:
                if "配置已更新" not in str(exc) and "refresh" not in str(exc):
                    raise
                time.sleep(0.25)
        raise RuntimeError("Bot kept changing during update")

    def configure(self, case):
        shared = case.startswith("shared_")
        source_count = 3 if case == "independent_distinct_agent" else 2
        sources = [
            self.api("POST", "sources", {
                "name": f"【组合验收】{case} 来源 {index + 1}",
                "type": "webhook",
            })
            for index in range(source_count)
        ]
        self.current_sources = sources
        workspaces = []
        for index, key in enumerate(BOT_IDS):
            suffix = "shared" if shared else f"bot-{index + 1}"
            # Physical directories stay under WORKBENCH_ROOT/workspaces, not runs.
            path = ROOT / "workspaces/validation" / self.output.name / case / suffix
            if shared and index:
                bot = self.bot_update(key, workspace_id=workspaces[0]["id"])
            else:
                bot = self.bot_update(
                    key,
                    workspace_mode="shared" if shared else "independent",
                    workspace_path=str(path),
                    workspace_name=f"【组合验收】{case} {suffix}",
                )
            wid = bot["workspace_id"]
            if wid not in [w["id"] for w in workspaces]:
                current = self.api("GET", f"workspaces/{wid}/settings")
                configured = self.api("POST", f"workspaces/{wid}/settings", {
                    "revision": current["revision"],
                    "name": current["name"],
                    "model_id": self.overview()["default_model"],
                    "ai_profile": {
                        **PROFILE,
                        "additional_instructions": PROFILE["additional_instructions"]
                        + f" 工作区标记：VALIDATION-SPACE-{index + 1}。",
                    },
                })
                workspaces.append(configured)
        modes = ("agent", "forward", "rules") if case == "independent_mixed_modes" else ("agent",) * 3
        subscriptions = []
        for index, workspace in enumerate(workspaces):
            ids = (
                [s["id"] for s in sources]
                if case == "shared_multi_agent"
                else [sources[index]["id"]]
                if case == "independent_distinct_agent"
                else [sources[0]["id"]]
            )
            mode = "agent" if shared else modes[index]
            subscriptions.append(self.api("POST", "subscriptions", {
                "name": f"【组合验收】{case} {index + 1}",
                "workspace_id": workspace["id"],
                "source_ids": ids,
                "mode": mode,
                "keywords": ["VALIDATION-MATCH"] if case == "shared_filter_and_duplicate" or mode == "rules" else [],
                "instructions": "仅处理系统验收测试，不是真实资讯。",
                "enabled": True,
            }))
        if case == "shared_overlapping_subs":
            subscriptions.append(self.api("POST", "subscriptions", {
                "name": f"【组合验收】{case} 重叠订阅",
                "workspace_id": workspaces[0]["id"],
                "source_ids": [sources[0]["id"]],
                "mode": "agent",
                "instructions": "仅处理系统验收测试，不是真实资讯。",
                "enabled": True,
            }))
        self.current_subs = subscriptions
        return sources, workspaces, subscriptions

    def emit(self, source, case, suffix, match=True):
        text = (
            f"{MARKER} 场景码 {case}；编号 {suffix}；"
            "这条合成事件只用于验证订阅、模型处理和路由，不含市场事实。"
            + (" VALIDATION-MATCH" if match else " VALIDATION-NOMATCH")
        )
        if case == "independent_mixed_modes":
            text += "。验收字段只含合成事实，不含市场数据" * 130
        return self.api("POST", "hooks/" + source["id"], {
            "id": self.output.name + "-" + case + "-" + suffix,
            "title": MARKER + " " + case,
            "text": text,
        }, source["hook_token"])

    def rows(self, case):
        prefix = self.output.name + "-" + case + "-"
        inputs = self.store.db.execute(
            "SELECT id FROM inputs WHERE item_key LIKE ?", (prefix + "%",)
        ).fetchall()
        ids = [r[0] for r in inputs]
        if not ids:
            return [], []
        placeholders = ",".join("?" for _ in ids)
        jobs = [dict(r) for r in self.store.db.execute(
            f"SELECT * FROM jobs WHERE event_id IN ({placeholders}) ORDER BY id", ids
        )]
        deliveries = [dict(r) for r in self.store.db.execute(
            f"SELECT * FROM deliveries WHERE job_id IN (SELECT id FROM jobs WHERE event_id IN ({placeholders})) ORDER BY id", ids
        )]
        return jobs, deliveries

    def wait(self, case, jobs_expected, deliveries_expected, pause=False, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs, deliveries = self.rows(case)
            if len(jobs) == jobs_expected and len(deliveries) == deliveries_expected:
                if all(j["state"] not in ("QUEUED", "RUNNING") for j in jobs) and all(
                    d["state"] not in ("WAITING_CONTENT", "READY", "SENDING", "WAITING_CONTEXT")
                    for d in deliveries
                ):
                    if not pause or Counter(d["state"] for d in deliveries)["PAUSED"] == 1:
                        return jobs, deliveries
            time.sleep(1)
        raise TimeoutError(f"{case}: jobs/deliveries not terminal; see platform history")

    def check(self, case, workspaces, subscriptions, started):
        jobs_expected, deliveries_expected, agents_expected = EXPECTED[case]
        jobs, deliveries = self.wait(case, jobs_expected, deliveries_expected, case == "shared_pause_resume")
        if case == "shared_pause_resume":
            assert Counter(d["state"] for d in deliveries) == {
                "ADAPTER_ACCEPTED": 2, "PAUSED": 1
            }, "Pause did not hold exactly one delivery"
            self.bot_update(BOT_IDS[-1], enabled=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                jobs, deliveries = self.rows(case)
                if all(d["state"] == "ADAPTER_ACCEPTED" for d in deliveries):
                    break
                time.sleep(1)
            else:
                raise TimeoutError("Paused delivery did not reach accepted after resume")
        reports = []
        agents = 0
        for job in jobs:
            snapshot = json.loads(job["snapshot"])
            paths = json.loads(job["paths"] or "{}")
            root = Path(snapshot["workspace"]["path"])
            report = (root / paths["report"]).read_text(encoding="utf-8") if paths.get("report") else ""
            trace = json.loads((root / paths["trace"]).read_text(encoding="utf-8")) if paths.get("trace") else {}
            model_calls = len(trace.get("usage", []))
            tools = len(trace.get("tools", []))
            if snapshot["mode"] == "agent":
                agents += 1
                assert model_calls > 0 and tools > 0, "Agent did not call model/tools"
                assert case in report and "测试" in report, "Report lost scenario/marker"
            else:
                assert not model_calls and not tools, "Deterministic mode called the model"
            reports.append({
                "job_id": job["id"], "workspace_id": job["workspace_id"],
                "mode": snapshot["mode"], "state": job["state"], "attempts": job["attempts"],
                "model_calls": model_calls, "tools": tools,
                "report_sha256": hashlib.sha256(report.encode()).hexdigest(),
                "report_path": str(root / paths["report"]) if paths.get("report") else None,
                "trace_path": str(root / paths["trace"]) if paths.get("trace") else None,
                "error": job["error"],
            })
        states = Counter(d["state"] for d in deliveries)
        assert len(jobs) == jobs_expected and agents == agents_expected
        assert len(deliveries) == deliveries_expected
        assert states == {"ADAPTER_ACCEPTED": deliveries_expected}, states
        assert all(d["attempts"] == 1 and json.loads(d["receipts"]) for d in deliveries)
        if case == "shared_single_agent":
            assert len({d["job_id"] for d in deliveries}) == 1
        if case == "independent_same_agent":
            assert len({d["job_id"] for d in deliveries}) == 3
        if case == "shared_overlapping_subs":
            assert len({d["job_id"] for d in deliveries}) == 1
            assert self.store.db.execute(
                "SELECT COUNT(*) FROM job_subscriptions WHERE job_id=?", (jobs[0]["id"],)
            ).fetchone()[0] == 2
        if case == "independent_mixed_modes":
            forward = next(r for r in reports if r["mode"] == "forward")
            rules = next(r for r in reports if r["mode"] == "rules")
            assert Path(forward["report_path"]).stat().st_size > Path(rules["report_path"]).stat().st_size
        result = {
            "case": case, "passed": True, "elapsed_seconds": round(time.time() - started, 1),
            "expected": {"jobs": jobs_expected, "deliveries": deliveries_expected, "agents": agents_expected},
            "actual": {"jobs": len(jobs), "deliveries": len(deliveries), "agents": agents},
            "states": dict(states), "workspaces": [w["id"] for w in workspaces],
            "subscriptions": [s["id"] for s in subscriptions],
            "sources": [s["id"] for s in self.current_sources],
            "reports": reports,
            "delivery_ids": [d["id"] for d in deliveries],
            "bot_types": [self.store.get("bot", key)["type"] for key in BOT_IDS],
        }
        return result

    def retire_case(self):
        for row in self.current_subs:
            current = next((s for s in self.overview()["subscriptions"] if s["id"] == row["id"]), None)
            if current and current["enabled"]:
                self.api("POST", "subscriptions", {**current, "enabled": False})
        for row in self.current_sources:
            current = next((s for s in self.overview()["sources"] if s["id"] == row["id"]), None)
            if current and current["enabled"]:
                self.api("POST", "sources", {**current, "enabled": False})
        self.current_subs = []
        self.current_sources = []

    def restore(self):
        for key, original in self.original.items():
            deadline = time.monotonic() + 60
            while self.store.db.execute(
                "SELECT 1 FROM deliveries WHERE bot_id=? AND state='SENDING'", (key,)
            ).fetchone():
                if time.monotonic() > deadline:
                    raise RuntimeError("Cannot restore binding while sending")
                time.sleep(1)
            bot = self.store.get("bot", key)
            updates = {}
            if bot["workspace_id"] != original:
                updates["workspace_id"] = original
            if not bot["enabled"]:
                updates["enabled"] = True
            if updates:
                self.bot_update(key, **updates)

    def run_case(self, case):
        self.active_case = case
        self.save()
        started = time.time()
        try:
            sources, workspaces, subscriptions = self.configure(case)
            if case == "shared_filter_and_duplicate":
                self.emit(sources[0], case, "filtered", match=False)
                assert not self.rows(case)[0], "Negative filter created a job"
                self.emit(sources[0], case, "accepted")
                self.emit(sources[0], case, "accepted")
            else:
                selected = (
                    sources[:2] if case == "shared_multi_agent" else
                    sources[:3] if case == "independent_distinct_agent" else sources[:1]
                )
                for index, source in enumerate(selected):
                    self.emit(source, case, str(index + 1))
            if case == "shared_pause_resume":
                self.bot_update(BOT_IDS[-1], enabled=False)
            result = self.check(case, workspaces, subscriptions, started)
            self.results.append(result)
            print(json.dumps({"case": case, "states": result["states"], "jobs": result["actual"]["jobs"], "agents": result["actual"]["agents"], "seconds": result["elapsed_seconds"]}, ensure_ascii=False), flush=True)
        except BaseException as exc:
            jobs, deliveries = self.rows(case)
            self.results.append({
                "case": case, "passed": False, "error": f"{type(exc).__name__}: {str(exc)[:250]}",
                "job_states": dict(Counter(j["state"] for j in jobs)),
                "delivery_states": dict(Counter(d["state"] for d in deliveries)),
                "jobs": [j["id"] for j in jobs], "deliveries": [d["id"] for d in deliveries],
                "source_ids": [s["id"] for s in self.current_sources],
                "subscription_ids": [s["id"] for s in self.current_subs],
            })
            print(f"FAILED {case}: {type(exc).__name__}: {str(exc)[:250]}", flush=True)
            raise
        finally:
            self.retire_case()
            self.active_case = None
            self.save()

    def run(self):
        before = self.overview()
        assert self.api("GET", "health")["ok"]
        bots = {b["id"]: b for b in before["bots"]}
        assert all(bots[key]["enabled"] and bots[key]["status"] == "CONNECTED" and bots[key]["targets"] for key in BOT_IDS)
        assert [bots[key]["type"] for key in BOT_IDS] == ["weixin", "weixin", "telegram"]
        assert not self.store.db.execute("SELECT 1 FROM jobs WHERE state IN ('RUNNING','QUEUED')").fetchone()
        assert not self.store.db.execute("SELECT 1 FROM deliveries WHERE state='SENDING'").fetchone()
        assert self.original == {key: bots[key]["workspace_id"] for key in BOT_IDS}
        try:
            for case in self.cases:
                self.run_case(case)
        finally:
            try:
                self.retire_case()
            finally:
                self.restore()
                self.save()
        assert all(self.store.get("bot", key)["workspace_id"] == original for key, original in self.original.items())
        print("RESTORED original bindings; retained disabled test configuration and artifacts", flush=True)
        print("REPORT " + str(self.output / "summary.json"), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", choices=CASES, default=["shared_single_agent"])
    parser.add_argument("--url", default="http://127.0.0.1:6190")
    args = parser.parse_args()
    Acceptance(args.cases, args.url).run()
