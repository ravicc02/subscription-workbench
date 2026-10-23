"""Standalone authenticated management API and production webhook ingestion."""

import asyncio
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .core import Conflict, digest
from .service import Workbench
from .sources import canonical


def create_app(root=None, worker=True, network=None):
    project = Path(
        root or os.environ.get("WORKBENCH_ROOT") or Path(__file__).resolve().parents[2]
    ).resolve()
    app = FastAPI(title="订阅推送工作台", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=os.environ.get("WORKBENCH_HOSTS", "127.0.0.1,localhost,testserver").split(
            ","
        ),
    )
    app.state.workbench = Workbench(project, network=network)
    w = app.state.workbench
    attempts = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        if worker:
            await w.start()
        yield
        await w.stop()

    app.router.lifespan_context = lifespan

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    @app.exception_handler(Exception)
    async def failed(_, exc):
        w.store.audit("system", "api", "ERROR", {"type": type(exc).__name__})
        return JSONResponse({"error": w.safe_error(exc)}, status_code=502)

    @app.middleware("http")
    async def gate(req: Request, call_next):
        if req.url.path.startswith("/api/"):
            if req.method not in ("GET", "HEAD"):
                origin = req.headers.get("origin")
                expected = req.headers.get("host", "")
                if origin and urlsplit(origin).netloc != expected:
                    return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
                try:
                    if int(req.headers.get("content-length", "0")) > 1_000_000:
                        return JSONResponse({"error": "请求过大"}, status_code=413)
                except ValueError:
                    return JSONResponse({"error": "Invalid content length"}, status_code=400)
                raw = await req.body()
                if len(raw) > 1_000_000:
                    return JSONResponse({"error": "请求过大"}, status_code=413)
            public = (
                req.url.path.startswith("/api/auth/")
                or req.url.path.startswith("/api/hooks/")
                or req.url.path == "/api/health"
            )
            if not public and not w.store.authenticated(req.cookies.get("wb_session")):
                return JSONResponse({"error": "请先登录"}, status_code=401)
            if (
                not public
                and req.method not in ("GET", "HEAD")
                and req.headers.get("x-requested-with") != "workbench"
            ):
                return JSONResponse({"error": "请求校验失败"}, status_code=403)
        response = await call_next(req)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = (
            "no-store" if req.url.path.startswith("/api/") else "no-cache"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    async def body(req):
        try:
            v = await req.json()
        except (ValueError, json.JSONDecodeError):
            raise ValueError("请求必须是 JSON 对象")
        if not isinstance(v, dict):
            raise ValueError("请求必须是 JSON 对象")
        return v

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "service": "subscription-workbench",
            "version": __version__,
            "astrbot_required": False,
        }

    @app.get("/api/auth/status")
    async def auth_status(req: Request):
        return {
            "setup_required": not w.store.admin_ready(),
            "authenticated": w.store.authenticated(req.cookies.get("wb_session")),
        }

    @app.post("/api/auth/{action}")
    async def authenticate(action: str, req: Request):
        if req.headers.get("x-requested-with") != "workbench":
            raise HTTPException(403, "Request header required")
        data = await body(req)
        if action == "logout":
            token = req.cookies.get("wb_session", "")
            w.store.db.execute("DELETE FROM sessions WHERE token=?", (digest(token.encode()),))
            r = JSONResponse({"ok": True})
            r.delete_cookie("wb_session")
            return r
        host = req.client.host if req.client else "local"
        q = attempts[host]
        while q and q[0] < time.time() - 300:
            q.popleft()
        if len(q) >= 10:
            raise HTTPException(429, "登录尝试过多，五分钟后重试")
        q.append(time.time())
        password = str(data.get("password", ""))
        if action == "setup":
            if w.store.admin_ready():
                raise HTTPException(409, "管理员已设置")
            if len(password) > 256:
                raise ValueError("密码过长")
            w.store.set_password(password)
        elif action != "login" or len(password) > 256 or not w.store.check_password(password):
            raise HTTPException(401, "密码不正确")
        q.clear()
        token = w.store.session()
        r = JSONResponse({"ok": True})
        r.set_cookie(
            "wb_session",
            token,
            httponly=True,
            samesite="strict",
            secure=req.url.scheme == "https",
            max_age=43200,
        )
        return r

    @app.get("/api/overview")
    async def overview():
        return w.overview()

    @app.post("/api/bots")
    async def create_bot(req: Request):
        return await w.create_bot(await body(req))

    @app.post("/api/bots/{key}/{action}")
    async def bot_action(key: str, action: str, req: Request):
        data = await body(req)
        if action == "qr":
            return await w.poll_qr(key)
        if action == "reconnect":
            return await w.reconnect(key, data.get("config"))
        if action == "update":
            return w.update_bot(key, data)
        if action == "test":
            bot = w.store.get("bot", key)
            if not bot.get("enabled") or bot["status"] not in ("CONNECTED", "READY"):
                raise ValueError("机器人尚未就绪或已暂停")
            config = w.store.open(bot["secret"])
            targets = [t["id"] for t in config.get("targets", []) if t.get("selected")]
            if not targets:
                raise ValueError("请先配置接收目标")
            text = str(data.get("text") or "订阅推送工作台连接测试：这是一条真实发送的消息。")[
                :1500
            ]
            receipts = []
            for target in targets:
                receipts.append(
                    await w.channels.send(
                        bot["type"],
                        config,
                        target,
                        text,
                        "test-" + secrets.token_hex(12),
                    )
                )
            w.store.audit("bot", key, "TEST_ACCEPTED", {"targets": len(receipts)})
            return {"accepted": len(receipts), "note": "通道已接受，不代表用户已读"}
        raise HTTPException(404)

    @app.post("/api/sources")
    async def source_save(req: Request):
        data = await body(req)
        if data.get("type", "rss") != "webhook":
            await asyncio.to_thread(
                w.net.destination, str(data.get("url", "")), w.effective_proxy(data)
            )
        saved = w.save_source(data, network_checked=True)
        if saved["type"] == "webhook" and not data.get("id"):
            token = secrets.token_urlsafe(32)
            row = w.store.get("source", saved["id"])
            row["hook_hash"] = digest(token.encode())
            saved = w.public_source(w.store.put("source", row))
            saved.update(hook_token=token, hook_path="/api/hooks/" + saved["id"])
        return saved

    @app.post("/api/sources/{key}/poll")
    async def source_poll(key: str):
        return await w.poll(key)

    @app.post("/api/subscriptions")
    async def subscription_save(req: Request):
        return w.save_subscription(await body(req))

    @app.post("/api/categories")
    async def category_save(req: Request):
        return w.save_category(await body(req))

    @app.post("/api/tags")
    async def tag_save(req: Request):
        return w.save_tag(await body(req))

    @app.post("/api/taxonomy/{kind}/{key}/delete")
    async def taxonomy_delete(kind: str, key: str, req: Request):
        return w.delete_taxonomy(kind, key, (await body(req)).get("revision"))

    @app.get("/api/packages")
    async def packages():
        return {"packages": w.builtin_packages()}

    @app.post("/api/packages/preview")
    async def package_preview(req: Request):
        return w.preview_package((await body(req)).get("manifest"))

    @app.post("/api/packages/import")
    async def package_import(req: Request):
        manifest = (await body(req)).get("manifest")
        impact = w.preview_package(manifest)
        if impact["conflicts"]:
            raise Conflict("来源分类冲突，请先调整后导入")
        existing = {
            (s["type"], canonical(s["url"]))
            for s in w.store.all("source") if s.get("url")
        }
        for category in manifest["categories"]:
            for source in category["sources"]:
                identity = (source["type"], canonical(source["url"]))
                if identity not in existing:
                    await asyncio.to_thread(w.net.destination, source["url"], w.effective_proxy(source))
        return w.import_package(manifest)

    @app.post("/api/packages/export")
    async def package_export(req: Request):
        return w.export_package(await body(req))

    @app.post("/api/models")
    async def model_save(req: Request):
        data = await body(req)
        if not data.get("name") or not data.get("model"):
            raise ValueError("模型名称和模型标识为必填")
        base = str(data.get("base_url", "")).rstrip("/")
        await asyncio.to_thread(w.net.resolve, base)
        key = data.get("id")
        old = w.store.get("model", key) if key else {}
        api_key = data.get("api_key")
        secret = (
            w.store.seal({"api_key": api_key})
            if api_key is not None
            else old.get("secret", w.store.seal({}))
        )
        model = {
            "name": str(data["name"])[:80],
            "base_url": base,
            "model": str(data["model"])[:150],
            "secret": secret,
            "temperature": float(data.get("temperature", 0.2)),
            "max_tokens": min(16000, max(100, int(data.get("max_tokens", 8000)))),
            "timeout": min(180, max(10, int(data.get("timeout", 90)))),
        }
        if data.get("thinking") in ("enabled", "disabled"):
            model["thinking"] = data["thinking"]
        elif "thinking" not in data and old.get("thinking"):
            model["thinking"] = old["thinking"]
        if key:
            model["id"] = key
        result = w.store.put("model", model, data.get("revision"))
        if data.get("default") or not w.store.setting("default_model"):
            w.store.set_setting("default_model", result["id"])
        w.store.audit("model", result["id"], "SAVED")
        return w.public_model(result)

    @app.post("/api/models/{key}/test")
    async def model_test(key: str):
        config = w.store.get("model", key)
        tool = {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "Check tool calling",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
        }
        r = await w.editor.completion(
            config,
            [
                {
                    "role": "user",
                    "content": "Call the probe tool with ok=true to verify tool calling.",
                }
            ],
            [tool],
        )
        calls = r["choices"][0]["message"].get("tool_calls", [])
        valid = any(
            c.get("function", {}).get("name") == "probe"
            and json.loads(c["function"]["arguments"]).get("ok") is True
            for c in calls
        )
        w.store.audit("model", key, "TOOLS_VERIFIED" if valid else "TOOLS_UNAVAILABLE")
        return {"tool_calling": valid, "usage": r.get("usage", {})}

    @app.get("/api/network")
    async def network_settings():
        return w.proxy_preferences()

    @app.post("/api/network")
    async def network_save(req: Request):
        return w.save_proxy_preferences(await body(req))

    @app.post("/api/network/test")
    async def network_test(req: Request):
        return await w.test_proxy(await body(req))

    @app.post("/api/manage/{mode}")
    async def manage(mode: str, req: Request):
        if mode not in ("preview", "apply"):
            raise HTTPException(404)
        return w.manage(await body(req), preview=mode == "preview")

    @app.post("/api/workspaces/{key}/rename")
    async def workspace_rename(key: str, req: Request):
        return w.rename_workspace(key, await body(req))

    @app.get("/api/workspaces/{key}/settings")
    async def workspace_settings(key: str):
        return w.workspace_settings(key)

    @app.post("/api/workspaces/{key}/settings")
    async def workspace_settings_save(key: str, req: Request):
        return w.save_workspace_settings(key, await body(req))

    @app.get("/api/workspaces/{key}/files")
    async def workspace_file(key: str, path: str = "docs/rules.md"):
        if path not in (
            "AGENTS.md",
            "docs/event-task.md",
            "docs/rules.md",
            "docs/template.md",
        ):
            raise ValueError("请选择规则或模板文件")
        wk = w.store.get("workspace", key)
        p = w.workspaces.file(wk["path"], path)
        return {
            "path": path,
            "content": p.read_text(encoding="utf-8"),
            "sha256": digest(p.read_bytes()),
        }

    @app.post("/api/workspaces/{key}/files")
    async def workspace_write(key: str, req: Request):
        w.store.get("workspace", key)
        raise Conflict("业务规则已迁入工作区设置，请在 AI 配置面板修改")

    @app.post("/api/workspaces/{key}/model")
    async def workspace_model(key: str, req: Request):
        data = await body(req)
        wk = w.store.get("workspace", key)
        if data.get("model_id"):
            w.store.get("model", data["model_id"])
        wk["model_id"] = data.get("model_id")
        w.store.put("workspace", wk, data.get("revision"))
        return {"ok": True}

    @app.post("/api/hooks/{key}")
    async def hook(key: str, req: Request):
        source = w.store.get("source", key)
        if source["type"] != "webhook" or not source.get("enabled"):
            raise HTTPException(404)
        supplied = req.headers.get("authorization", "").removeprefix("Bearer ")
        if not supplied or not hmac.compare_digest(digest(supplied.encode()), source["hook_hash"]):
            raise HTTPException(401, "Invalid webhook credential")
        data = await body(req)
        if (
            not isinstance(data.get("id"), str)
            or not 1 <= len(data["id"]) <= 200
            or not isinstance(data.get("text"), str)
            or not 1 <= len(data["text"]) <= 80000
        ):
            raise ValueError("Webhook requires bounded id and text strings")
        row = {
            "item_key": data["id"],
            "title": str(data.get("title") or source["name"])[:200],
            "text": data["text"],
            "content_type": "event",
        }
        return w.ingest(source, [row], external=True)

    @app.get("/api/history")
    async def history(
        kind: str = "deliveries",
        page: int = 1,
        state: str = "",
        bot_id: str = "",
        q: str = "",
    ):
        if kind not in ("deliveries", "jobs", "audit"):
            raise ValueError("Unknown history type")
        if not 1 <= page <= 100000:
            raise ValueError("Invalid page")
        clauses = []
        args = []
        if state:
            clauses.append("state=?")
            args.append(state)
        if bot_id and kind == "deliveries":
            clauses.append("bot_id=?")
            args.append(bot_id)
        if q:
            field = "object_id" if kind == "audit" else "id"
            clauses.append(field + " LIKE ?")
            args.append("%" + q[:100] + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = w.store.db.execute("SELECT COUNT(*) FROM " + kind + where, args).fetchone()[0]
        rows = [
            dict(r)
            for r in w.store.db.execute(
                "SELECT * FROM " + kind + where + " ORDER BY created DESC LIMIT 20 OFFSET ?",
                args + [(page - 1) * 20],
            )
        ]
        if kind == "jobs":
            for r in rows:
                r.pop("snapshot", None)
        return {"rows": rows, "total": total}

    @app.get("/api/artifacts/{job_id}")
    async def artifact(job_id: str, kind: str = "report"):
        if kind not in ("raw", "processed", "report", "note", "trace", "event"):
            raise ValueError("Unknown artifact type")
        job = w.store.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or not job["paths"]:
            raise HTTPException(404)
        snapshot = json.loads(job["snapshot"])
        paths = json.loads(job["paths"])
        p = w.workspaces.file(snapshot["workspace"]["path"], paths[kind])
        if not p.is_file():
            raise HTTPException(404)
        return {"path": paths[kind], "content": p.read_text(encoding="utf-8")[:500000]}

    dist = project / "web/dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(dist / "index.html")

    return app
