"""Browser acceptance in an isolated state directory; no customer accounts."""

import json
import os
import secrets
import select as io_select
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
out = root / "runs/verification"
out.mkdir(parents=True, exist_ok=True)
instance = out / ("ui-instance-" + str(int(time.time())))
(instance / "web").mkdir(parents=True)
(instance / "docs").mkdir()
assets = Path(os.environ.get("WORKBENCH_BROWSER_DIST", root / "web/dist")).resolve()
shutil.copytree(assets, instance / "web/dist")
shutil.copyfile(root / "docs/imported-sources.json", instance / "docs/imported-sources.json")


class Sink(BaseHTTPRequestHandler):
    receipts = []
    checks = 0

    def do_GET(self):
        Sink.checks += 1
        time.sleep(2)
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(b"<rss><channel></channel></rss>")

    def log_message(self, *a):
        pass

    def do_POST(self):
        Sink.receipts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


class ProxySink(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = 0

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        host, port = self.path.rsplit(":", 1)
        remote = socket.create_connection((host, int(port)), timeout=5)
        ProxySink.connections += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                ready, _, _ = io_select.select([self.connection, remote], [], [], 5)
                if not ready:
                    return
                for stream in ready:
                    chunk = stream.recv(65536)
                    if not chunk:
                        return
                    (remote if stream is self.connection else self.connection).sendall(chunk)
        finally:
            remote.close()
            self.close_connection = True


proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), ProxySink)
threading.Thread(target=proxy_server.serve_forever, daemon=True).start()

server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
threading.Thread(target=server.serve_forever, daemon=True).start()
log = (out / "ui-server.log").open("w", encoding="utf-8")
env = {
    **os.environ,
    "WORKBENCH_ROOT": str(instance),
    "WORKBENCH_ALLOWED_HOSTS": "127.0.0.1",
}
proc = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "6191",
    ],
    cwd=root,
    env=env,
    stdout=log,
    stderr=log,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
results = {"instance": str(instance), "page_errors": []}
try:
    for _ in range(100):
        try:
            with urlopen("http://127.0.0.1:6191/api/health", timeout=1) as r:
                break
        except Exception:
            time.sleep(0.2)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1550, "height": 1050}, device_scale_factor=1)
        page.set_default_timeout(15000)
        page.on("pageerror", lambda e: results["page_errors"].append(str(e)))
        page.goto("http://127.0.0.1:6191", wait_until="networkidle")
        page.get_by_label("管理员密码").fill(secrets.token_urlsafe(24))
        page.get_by_role("button", name="创建并进入").click()
        page.get_by_role("heading", name="运行概览", exact=True).wait_for()
        results["setup_login"] = True
        page.screenshot(path=str(out / "overview.png"), full_page=True)

        def nav(name):
            page.locator("nav").get_by_role("button", name=name, exact=True).click()
            page.wait_for_timeout(200)
            if results["page_errors"]:
                raise RuntimeError("Page errors: " + str(results["page_errors"]))

        def select(label, value):
            control = page.get_by_role("dialog").get_by_label(label, exact=True)
            if control.get_attribute("readonly") is None:
                control.fill(value)
            else:
                control.press("ArrowDown")
            try:
                page.locator(".ant-select-dropdown:visible").get_by_text(value, exact=True).click(
                    timeout=6000
                )
            except Exception:
                page.screenshot(path=str(out / "select-failure.png"), full_page=True)
                print(
                    "Select failure:",
                    label,
                    control.get_attribute("aria-expanded"),
                    results["page_errors"],
                    flush=True,
                )
                raise

        def multiselect(label, value):
            control = page.get_by_role("dialog").get_by_label(label, exact=True)
            control.locator("xpath=ancestor::div[contains(@class,'ant-select-selector')]").click()
            control.fill(value)
            try:
                page.locator(".ant-select-dropdown:visible").get_by_text(value, exact=True).click(
                    timeout=6000
                )
            except Exception:
                page.screenshot(path=str(out / "multi-select-failure.png"), full_page=True)
                print(
                    "Multi select failure:",
                    label,
                    value,
                    page.locator(".ant-select-dropdown:visible").all_inner_texts(),
                    flush=True,
                )
                raise
            if control.get_attribute("aria-expanded") == "true":
                control.press("Escape")

        def addbot(name, shared):
            nav("机器人")
            page.get_by_role("button", name="新增机器人", exact=True).click()
            page.get_by_label("机器人名称", exact=True).fill(name)
            select("机器人类型", "HTTP Webhook")
            page.get_by_role("button", name="下一步", exact=True).click()
            page.get_by_label("接收地址", exact=True).fill(
                "http://127.0.0.1:" + str(server.server_port) + "/receive"
            )
            if shared:
                page.get_by_text("本地工作区设置", exact=True).click()
                select("使用方式", "共享目录 · 多机器人共用")
                if page.get_by_label("共享工作区名称", exact=True).count():
                    page.get_by_label("共享工作区名称", exact=True).fill("UI Shared Workspace")
            page.get_by_role("button", name="配置并接入", exact=True).click()
            page.get_by_role("heading", name="机器人已接入，工作区已绑定").wait_for()
            try:
                page.get_by_role("button", name="完成", exact=True).click()
            except Exception:
                page.screenshot(path=str(out / "onboarding-failure.png"), full_page=True)
                print("Onboarding page errors:", results["page_errors"], flush=True)
                print(
                    "Visible buttons:", page.get_by_role("button").all_text_contents(), flush=True
                )
                raise
            expect(page.get_by_role("dialog")).to_have_count(0)

        addbot("UI Shared A", True)
        addbot("UI Shared B", True)
        addbot("UI Independent C", False)
        results["bot_wizard"] = True
        overview = page.request.get("http://127.0.0.1:6191/api/overview").json()
        bots = overview["bots"]
        assert bots[0]["workspace_id"] == bots[1]["workspace_id"] != bots[2]["workspace_id"]
        assert all(Path(b["workspace_path"], "AGENTS.md").exists() for b in bots)
        results["automatic_shared_and_independent_directories"] = True
        assert (
            next(w for w in overview["workspaces"] if w["id"] == bots[0]["workspace_id"])["name"]
            == "UI Shared Workspace"
        )
        results["named_shared_workspace_creation"] = True
        page.screenshot(path=str(out / "bots.png"), full_page=True)
        nav("订阅广场")
        finance = page.locator(".plaza-item").filter(has_text="金融市场")
        expect(finance).to_have_count(1)
        expect(finance.locator(".plaza-metrics strong")).to_have_count(0)
        expect(finance.get_by_text("GoldSeek", exact=True)).not_to_be_visible()
        page.locator(".ant-notification-notice-close").evaluate_all(
            "(buttons) => buttons.forEach((button) => button.click())"
        )
        expect(page.locator(".ant-notification-notice")).to_have_count(0)
        page.screenshot(path=str(out / "plaza-compact.png"), full_page=True)
        finance.get_by_text("来源明细（6）", exact=True).click()
        expect(finance.get_by_text("GoldSeek", exact=True)).to_be_visible()
        page.wait_for_timeout(350)
        page.screenshot(path=str(out / "plaza-expanded.png"), full_page=True)
        finance.get_by_text("来源明细（6）", exact=True).click()
        expect(finance.get_by_text("GoldSeek", exact=True)).not_to_be_visible()
        results["plaza_compact_and_collapsible_sources"] = True
        action = finance.locator(".plaza-item-head button")
        expect(action).to_have_text("导入本地")
        action.click()
        page.locator(".ant-modal-confirm").get_by_role("button", name="同步到本地").click()
        page.get_by_role("dialog").get_by_text("订阅配置", exact=True).wait_for()
        expect(page.get_by_role("dialog").get_by_text("金融", exact=True)).to_be_visible()
        assert not page.request.get("http://127.0.0.1:6191/api/overview").json()["subscriptions"]
        page.get_by_role("dialog").get_by_role("button", name="取消", exact=True).click()
        nav("订阅广场")
        finance = page.locator(".plaza-item").filter(has_text="金融市场")
        action = finance.locator(".plaza-item-head button")
        expect(action).to_have_text("创建订阅")
        action.click()
        page.get_by_role("dialog").get_by_text("订阅配置", exact=True).wait_for()
        expect(page.get_by_role("dialog").get_by_text("金融", exact=True)).to_be_visible()
        multiselect("标签（多选）", "黄金 · 1 个来源")
        expect(page.get_by_text("匹配 1 个来源，其中 1 个已启用")).to_be_visible()
        page.get_by_role("dialog").get_by_role("button", name="取消", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        results["package_import_direct_subscription_and_ready_shortcut"] = True
        nav("订阅源")
        page.evaluate("window.scrollTo(0, 0)")
        page.get_by_role("button", name="新增订阅源", exact=True).click()
        page.get_by_label("订阅源名称", exact=True).fill("UI Release Events")
        select("订阅源类型", "Webhook 外部事件")
        page.get_by_role("button", name="保存", exact=True).click()
        page.locator(".ant-modal-confirm-title").filter(has_text="保存 Webhook 接入信息").wait_for()
        secrets_text = page.locator(".ant-modal-confirm-content").inner_text()
        token = secrets_text.split("Authorization: Bearer ")[1].split("\n")[0].strip()
        page.locator(".ant-modal-confirm-btns .ant-btn-primary").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        overview = page.request.get("http://127.0.0.1:6191/api/overview").json()
        src = next(s for s in overview["sources"] if s["name"] == "UI Release Events")
        results["source_create"] = True
        extra_response = page.request.post(
            "http://127.0.0.1:6191/api/sources",
            headers={"X-Requested-With": "workbench"},
            data={"name": "UI Extra Events", "type": "webhook", "poll_minutes": 15},
        )
        assert extra_response.ok
        extra = extra_response.json()
        category = page.request.post(
            "http://127.0.0.1:6191/api/categories",
            headers={"X-Requested-With": "workbench"},
            data={"key": "release-events", "name": "版本事件"},
        ).json()
        tags = []
        for key, name in (("release", "版本发布"), ("extra", "扩展事件")):
            response = page.request.post(
                "http://127.0.0.1:6191/api/tags",
                headers={"X-Requested-With": "workbench"},
                data={"category_id": category["id"], "key": key, "name": name},
            )
            assert response.ok, response.text()
            tags.append(response.json())
        for item, tag in zip((src, extra), tags):
            response = page.request.post(
                "http://127.0.0.1:6191/api/sources",
                headers={"X-Requested-With": "workbench"},
                data={**item, "category_id": category["id"], "tag_ids": [tag["id"]]},
            )
            assert response.ok, response.text()

        nav("订阅")
        page.get_by_role("button", name="刷新", exact=True).click()
        page.get_by_text("刷新工作台完成", exact=True).wait_for()
        page.get_by_role("button", name="新增订阅", exact=True).click()
        page.get_by_label("订阅名称", exact=True).fill("UI Delivery Flow")
        page.get_by_label("本地工作区", exact=True).press("ArrowDown")
        page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
            has_text="shared"
        ).first.click()
        expect(page.get_by_text("接收机器人：", exact=False)).to_be_visible()
        expect(page.get_by_role("dialog").get_by_label("订阅源", exact=True)).to_have_count(0)
        select("分类", "版本事件")
        multiselect("标签（多选）", "版本发布 · 1 个来源")
        multiselect("标签（多选）", "扩展事件 · 1 个来源")
        expect(page.get_by_text("匹配 2 个来源，其中 2 个已启用")).to_be_visible()
        page.screenshot(path=str(out / "subscription-scope.png"), full_page=True)
        results["category_and_multi_tag_selection"] = True
        expect(page.get_by_text("Agent 整理 · 调用模型", exact=True)).to_be_visible()
        results["processing_modes_explained"] = True
        select("处理方式", "直接转发 · 不调用模型")
        expect(page.get_by_text("直接转发 · 无模型调用", exact=True)).to_be_visible()
        expect(page.get_by_label("模型（可选）", exact=True)).to_have_count(0)
        expect(page.get_by_label("订阅追加要求（可选）", exact=True)).to_have_count(0)
        page.get_by_role("button", name="保存", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        results["subscription_create"] = True
        grouped = next(
            s
            for s in page.request.get("http://127.0.0.1:6191/api/overview").json()["subscriptions"]
            if s["name"] == "UI Delivery Flow"
        )
        assert grouped["source_ids"] == []
        assert grouped["category_id"] == category["id"]
        assert set(grouped["tag_ids"]) == {tag["id"] for tag in tags}
        assert len(grouped["resolved_source_ids"]) == 2
        results["multi_source_subscription"] = True
        r = page.request.post(
            "http://127.0.0.1:6191/api/hooks/" + src["id"],
            headers={"Authorization": "Bearer " + token},
            data={
                "id": "browser-release-1",
                "title": "版本发布验证",
                "text": "已发布版本 1.0。本条内容用于验证独立工作台从事件触发、处理到 HTTP 接收端的完整流程。",
            },
        )
        assert r.status == 200, r.text()
        for _ in range(80):
            if len(Sink.receipts) >= 2:
                break
            page.wait_for_timeout(250)
        assert len(Sink.receipts) == 2, len(Sink.receipts)
        assert Sink.receipts[0]["text"] == Sink.receipts[1]["text"]
        results["automatic_real_http_delivery"] = True
        nav("运行与回溯")
        page.get_by_text("通道已接受", exact=True).first.wait_for(timeout=15000)
        page.get_by_role("button", name="查看", exact=True).first.click()
        page.get_by_text("内容与运行证据", exact=True).wait_for()
        expect(page.locator(".artifact")).to_contain_text("版本发布验证")
        results["artifact_view"] = True
        page.screenshot(path=str(out / "artifact.png"), full_page=True)
        page.locator(".ant-drawer-open .ant-drawer-close").click()
        database = instance / "state/workbench.sqlite3"
        with sqlite3.connect(database) as conn:
            job_id, old_state, old_retry, old_error = conn.execute(
                "SELECT id,state,next_try,error FROM jobs ORDER BY created DESC LIMIT 1"
            ).fetchone()
            conn.execute(
                "UPDATE jobs SET state='QUEUED',next_try=?,error=? WHERE id=?",
                (time.time() + 180, "Site HTTP cooldown", job_id),
            )
        try:
            page.get_by_role("tab", name="处理任务").click()
            expect(page.get_by_role("tab", name="处理任务")).to_have_attribute(
                "aria-selected", "true"
            )
            expect(page.get_by_text("等待重试", exact=True)).to_be_visible()
            expect(page.get_by_role("columnheader", name="下次尝试")).to_be_visible()
            expect(page.locator(".ant-table-tbody")).to_contain_text("Site HTTP cooldown")
            page.wait_for_timeout(350)
            page.screenshot(path=str(out / "deferred-job.png"), full_page=True)
            results["deferred_job_history"] = True
        finally:
            with sqlite3.connect(database) as conn:
                conn.execute(
                    "UPDATE jobs SET state=?,next_try=?,error=? WHERE id=?",
                    (old_state, old_retry, old_error, job_id),
                )
        nav("工作区")
        page.screenshot(path=str(out / "workspaces.png"), full_page=True)
        original_path = bots[0]["workspace_path"]
        page.get_by_label("AI 关注范围", exact=True).fill("尚未保存的关注范围")
        page.get_by_role("button", name="重命名工作区", exact=True).click()
        page.get_by_role("dialog").get_by_label("工作区名称", exact=True).fill("行情共享工作区")
        page.get_by_role("dialog").get_by_role("button", name="保存名称", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.get_by_text(
            "工作区名称已更新，目录、规则和机器人绑定保持不变。", exact=True
        ).wait_for()
        renamed = page.request.get("http://127.0.0.1:6191/api/overview").json()
        current_workspace = next(
            w for w in renamed["workspaces"] if w["id"] == bots[0]["workspace_id"]
        )
        assert (
            current_workspace["name"] == "行情共享工作区"
            and current_workspace["path"] == original_path
        )
        assert all(
            b["workspace_id"] == bots[0]["workspace_id"]
            for b in renamed["bots"]
            if b["name"] in ("UI Shared A", "UI Shared B")
        )
        expect(page.get_by_label("AI 关注范围", exact=True)).to_have_value("尚未保存的关注范围")
        results["shared_workspace_quick_rename"] = True
        page.get_by_label("AI 关注范围", exact=True).fill("关注软件版本发布和兼容性变化")
        page.get_by_label("整理要求", exact=True).fill(
            "只保留已经核实的变更，分事实和后续关注两部分。"
        )
        page.locator(".workspace-content").get_by_label("工作区名称", exact=True).fill(
            "版本发布工作区"
        )
        page.get_by_role("button", name="保存工作区设置", exact=True).click()
        page.get_by_text(
            "工作区配置已保存，新入队任务将使用这套规则。", exact=True
        ).first.wait_for()
        current = page.request.get("http://127.0.0.1:6191/api/overview").json()
        shared = next(w for w in current["workspaces"] if w["id"] == bots[0]["workspace_id"])
        assert shared["ai_profile"]["focus_prompt"] == "关注软件版本发布和兼容性变化"
        assert shared["ai_profile"]["processing_prompt"].startswith("只保留")
        results["workspace_ai_settings"] = True
        page.screenshot(path=str(out / "workspace-settings.png"), full_page=True)
        independent = next(w for w in current["workspaces"] if w["id"] == bots[2]["workspace_id"])
        page.locator(".workspace-option button").filter(has_text=independent["name"]).click()
        page.get_by_label("AI 关注范围", exact=True).fill("仅关注能源政策与供需变化")
        page.get_by_role("button", name="保存工作区设置", exact=True).click()
        page.wait_for_timeout(300)
        current = page.request.get("http://127.0.0.1:6191/api/overview").json()
        assert len({w["ai_profile"]["focus_prompt"] for w in current["workspaces"]}) == 2
        results["independent_workspace_profiles"] = True
        headers = {"X-Requested-With": "workbench"}
        dialog_model = page.request.post(
            "http://127.0.0.1:6191/api/models",
            headers=headers,
            data={
                "name": "Conversation QA",
                "model": "qa-model",
                "base_url": "http://127.0.0.1:" + str(server.server_port),
            },
        ).json()
        nav("对话")
        page.get_by_role("combobox", name="默认对话模型").click()
        page.locator(".ant-select-dropdown:visible").get_by_text(
            "Conversation QA", exact=True
        ).click()
        page.get_by_text("默认对话模型已更新", exact=True).wait_for()
        page.get_by_role("combobox", name="默认对话模型").press("Escape")
        page.get_by_label("SearXNG 地址", exact=True).fill(
            "http://127.0.0.1:" + str(server.server_port)
        )
        page.get_by_role("switch", name="启用实时检索").click()
        expect(page.get_by_role("switch", name="启用实时检索")).to_be_checked()
        page.get_by_role("button", name="保存检索配置", exact=True).click()
        page.get_by_text("检索配置已保存", exact=True).wait_for()
        nav("工作区")
        page.locator(".workspace-option button").filter(has_text="版本发布工作区").click()
        page.get_by_label("对话风格与边界", exact=True).fill("只解释当前目标的推送")
        page.get_by_label("允许实时检索", exact=True).check()
        page.get_by_role("button", name="保存工作区设置", exact=True).click()
        page.get_by_text(
            "工作区配置已保存，新入队任务将使用这套规则。", exact=True
        ).first.wait_for()
        configured = page.request.get("http://127.0.0.1:6191/api/overview").json()
        workspace = next(w for w in configured["workspaces"] if w["id"] == bots[0]["workspace_id"])
        assert configured["conversation_model_id"] == dialog_model["id"]
        assert workspace["conversation_profile"] == {
            "prompt": "只解释当前目标的推送",
            "web_search_enabled": True,
        }
        nav("对话")
        expect(page.get_by_text("入站与回复", exact=True)).to_be_visible()
        page.screenshot(path=str(out / "conversations.png"), full_page=True)
        results["conversation_configuration"] = True
        slow = page.request.post(
            "http://127.0.0.1:6191/api/sources",
            headers=headers,
            data={
                "name": "Slow feedback feed",
                "type": "rss",
                "url": "http://127.0.0.1:" + str(server.server_port) + "/slow",
                "poll_minutes": 5,
            },
        ).json()
        nav("订阅源")
        page.get_by_role("button", name="刷新", exact=True).click()
        page.get_by_text("刷新工作台完成", exact=True).wait_for()
        results["manual_refresh_feedback"] = True
        page.get_by_placeholder("搜索来源").fill("Slow feedback feed")
        check_button = page.get_by_role("button", name="检查订阅源 Slow feedback feed", exact=True)
        check_button.click()
        expect(check_button).to_have_class(
            __import__("re").compile("ant-btn-loading"), timeout=1000
        )
        page.get_by_text("检查「Slow feedback feed」中", exact=True).wait_for(timeout=1000)
        check_button.evaluate("(button)=>button.click()")
        nav("模型")
        page.get_by_text("检查「Slow feedback feed」完成", exact=True).wait_for(timeout=15000)
        assert Sink.checks == 1
        results["immediate_check_feedback_and_duplicate_guard"] = True
        page.get_by_role("button", name="操作记录", exact=True).click()
        expect(page.locator(".operation-list")).to_contain_text("Slow feedback feed")
        page.get_by_role("button", name="操作记录", exact=True).click()
        results["operation_history_across_pages"] = True
        nav("订阅源")
        configured_sources = page.request.get("http://127.0.0.1:6191/api/overview").json()[
            "sources"
        ]
        external = [
            {"id": source["id"], "revision": source["revision"]}
            for source in configured_sources
            if source["enabled"] and source["type"] != "webhook" and source["id"] != slow["id"]
        ]
        if external:
            pause = page.request.post(
                "http://127.0.0.1:6191/api/manage/apply",
                headers=headers,
                data={"kind": "source", "action": "disable", "items": external},
            )
            assert pause.ok, pause.text()
        page.get_by_role("button", name="刷新", exact=True).click()
        expect(page.get_by_role("button", name="检查全部", exact=True)).to_be_enabled()
        page.get_by_role("button", name="检查全部", exact=True).click()
        confirmation = page.get_by_role("dialog", name="检查全部订阅源？")
        expect(confirmation).to_contain_text("可能触发整理和真实推送")
        expect(confirmation).to_contain_text("将检查 1 个")
        confirmation.get_by_role("button", name="开始检查").click()
        page.get_by_text("检查任务已创建，离开页面后仍会继续", exact=True).wait_for()
        nav("模型")
        page.reload(wait_until="networkidle")
        nav("订阅源")
        page.locator(".source-check-progress").get_by_text("已检查 1 个来源", exact=False).wait_for(
            timeout=30000
        )
        batches = page.request.get("http://127.0.0.1:6191/api/source-checks").json()["batches"]
        assert len(batches) == 1 and batches[0]["state"] == "COMPLETED"
        assert batches[0]["total"] == 1 and batches[0]["skipped"] == len(configured_sources) - 1
        assert batches[0]["counts"] == {"DEFERRED": 1}
        page.get_by_role("button", name="查看明细").click()
        details = page.get_by_role("dialog", name="订阅源检查结果")
        expect(details).to_contain_text("Slow feedback feed")
        page.screenshot(path=str(out / "sources-bulk.png"), full_page=True)
        expect(details).to_contain_text("间隔/冷却")
        details.locator(".ant-drawer-close").click()
        page.get_by_placeholder("搜索来源").fill("")
        page.get_by_role("combobox", name="来源状态").click()
        page.locator(".ant-select-dropdown:visible").get_by_text("Webhook 事件", exact=True).click()
        webhooks = [
            source
            for source in page.request.get("http://127.0.0.1:6191/api/overview").json()["sources"]
            if source["enabled"] and source["type"] == "webhook"
        ]
        assert webhooks
        expect(page.locator("main .panel .ant-table-tbody tr.ant-table-row")).to_have_count(
            min(len(webhooks), 12)
        )
        page.locator(".source-tools .ant-select").nth(2).locator(".ant-select-clear").click()
        results["check_all_persists_after_reload_and_skips_ineligible"] = True
        for name in ("Batch source A", "Batch source B"):
            response = page.request.post(
                "http://127.0.0.1:6191/api/sources",
                headers=headers,
                data={"name": name, "type": "webhook", "poll_minutes": 15},
            )
            assert response.ok
        nav("订阅源")
        page.get_by_role("button", name="刷新", exact=True).click()
        page.get_by_placeholder("搜索来源").fill("Batch source")
        for name in ("Batch source A", "Batch source B"):
            page.get_by_role("checkbox", name="选择 " + name, exact=True).check()
        expect(page.get_by_role("button", name="检查所选", exact=True)).to_be_disabled()
        results["webhook_batch_check_is_explicitly_unavailable"] = True
        page.get_by_role("button", name="批量暂停", exact=True).click()
        page.get_by_text("已暂停 2 个订阅源", exact=True).wait_for()
        current = page.request.get("http://127.0.0.1:6191/api/overview").json()
        batch = [x for x in current["sources"] if x["name"].startswith("Batch source")]
        assert len(batch) == 2 and all(not x["enabled"] for x in batch)
        for name in ("Batch source A", "Batch source B"):
            page.get_by_role("checkbox", name="选择 " + name, exact=True).check()
        page.get_by_role("button", name="批量删除订阅源", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="删除", exact=True).click()
        page.get_by_text("已删除 2 个订阅源", exact=True).wait_for()
        assert not any(
            x["name"].startswith("Batch source")
            for x in page.request.get("http://127.0.0.1:6191/api/overview").json()["sources"]
        )
        results["bulk_pause_and_delete"] = True
        nav("机器人")
        page.get_by_role("button", name="删除机器人 UI Independent C", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="删除", exact=True).click()
        page.get_by_text("已删除 1 个机器人", exact=True).wait_for()
        assert Path(bots[2]["workspace_path"], "AGENTS.md").exists()
        results["single_bot_delete_preserves_local_directory"] = True
        nav("采集网络")
        page.get_by_label("代理端口", exact=True).fill(str(proxy_server.server_port))
        page.get_by_role("button", name="保存采集网络设置", exact=True).click()
        page.get_by_text(
            "采集网络设置已保存，将用于后续订阅列表和正文请求。", exact=True
        ).wait_for()
        prefs = page.request.get("http://127.0.0.1:6191/api/network").json()
        assert (
            prefs["configured"]
            and not prefs["enabled"]
            and prefs["port"] == proxy_server.server_port
        )
        control = page.get_by_role("combobox", name="代理测试订阅源", exact=True)
        control.fill("Slow feedback feed")
        page.locator(".ant-select-dropdown:visible").get_by_text(
            "Slow feedback feed", exact=True
        ).click()
        page.get_by_role("button", name="测试此代理", exact=True).click()
        page.get_by_text("测试「Slow feedback feed」代理完成", exact=True).wait_for(timeout=20000)
        assert ProxySink.connections >= 1
        results["proxy_settings_and_real_tunnel_probe"] = True
        page.screenshot(path=str(out / "proxy-settings.png"), full_page=True)
        nav("订阅源")
        page.get_by_placeholder("搜索来源").fill("Slow feedback feed")
        page.get_by_role("row").filter(has_text="Slow feedback feed").get_by_role(
            "button", name="编辑", exact=True
        ).click()
        select("采集网络", "使用已配置代理")
        page.get_by_role("dialog").get_by_role("button", name="保存", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        current = page.request.get("http://127.0.0.1:6191/api/overview").json()
        assert next(s for s in current["sources"] if s["id"] == slow["id"])["proxy_mode"] == "proxy"
        results["per_source_proxy_override"] = True
        nav("机器人")
        page.get_by_role("button", name="新增机器人", exact=True).click()
        page.get_by_label("机器人名称", exact=True).fill("Telegram Network Check")
        select("机器人类型", "Telegram")
        page.get_by_role("button", name="下一步", exact=True).click()
        page.get_by_text("Telegram 自动使用采集网络中保存的全局代理地址").wait_for()
        assert page.get_by_label("机器人网络", exact=True).count() == 0
        page.get_by_role("dialog").locator(".ant-modal-close").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        results["telegram_inherits_global_proxy_without_bot_switch"] = True
        page.set_viewport_size({"width": 390, "height": 844})
        nav("订阅源")
        page.wait_for_timeout(250)
        page.locator(".ant-notification-notice-close").evaluate_all(
            "(buttons) => buttons.forEach((button) => button.click())"
        )
        page.screenshot(path=str(out / "sources-mobile.png"), full_page=True)
        assert page.locator("body").evaluate("(e)=>e.scrollWidth<=window.innerWidth")
        assert page.locator("main .ant-table-cell-fix-left").count() == 0
        results["source_mobile_unpinned"] = True
        page.set_viewport_size({"width": 500, "height": 900})
        nav("订阅广场")
        page.locator(".ant-notification-notice-close").evaluate_all(
            "(buttons) => buttons.forEach((button) => button.click())"
        )
        expect(page.locator(".ant-notification-notice")).to_have_count(0)
        page.screenshot(path=str(out / "plaza-mobile.png"), full_page=True)
        assert page.locator("body").evaluate("(e)=>e.scrollWidth<=window.innerWidth")
        page.get_by_text("来源明细（6）", exact=True).click()
        expect(page.get_by_text("GoldSeek", exact=True)).to_be_visible()
        assert page.locator("body").evaluate("(e)=>e.scrollWidth<=window.innerWidth")
        results["plaza_mobile_collapse"] = True
        nav("运行概览")
        page.screenshot(path=str(out / "mobile.png"), full_page=True)
        results["responsive_layout"] = page.locator("body").evaluate(
            "(e)=>e.scrollWidth<=window.innerWidth"
        )
        assert "沙盒" not in page.locator("body").inner_text()
        assert "客户工作区" not in page.locator("body").inner_text()
        page.context.clear_cookies()
        expect(page.get_by_role("heading", name="登录订阅推送工作台")).to_be_visible(timeout=10000)
        assert page.get_by_role("heading", name="运行概览", exact=True).count() == 0
        results["expired_session_clears_stale_data_and_returns_to_login"] = True
        browser.close()
    (out / "browser.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=True))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    server.shutdown()
    server.server_close()
    proxy_server.shutdown()
    proxy_server.server_close()
    log.close()
