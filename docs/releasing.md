# 发布流程

本项目以 `main` 为集成分支；版本号使用 `X.Y.Z`，Git 标签使用 `vX.Y.Z`。发布物是带已构建管理页面的源码 ZIP，不包含运行实例、平台登录状态或密钥。发布工作流不会部署服务，也不会发送消息。

## 合并前

1. 评估数据结构与旧版本兼容性，确认工作区、队列和真实投递语义；危险迁移单独备份并在隔离数据上演练。
2. 同步 `backend/app/__init__.py` 与 `web/package.json` 的版本，并更新 `docs/status.md`、`CHANGELOG.md`。不要只修改界面显示版本。
3. 在本地运行以下检查；涉及浏览器交互时额外运行隔离浏览器验收。CI 在每次推送 `main` 和每个 PR 上执行检查、构建与隔离浏览器验收。

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build-release.py --check
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
.\.venv\Scripts\python.exe scripts\check-browser.py
```

浏览器检查只操作独立数据目录和本地 HTTP 接收端。真实微信/Telegram 等账号、模型计费及公网订阅源仍需要另行人工验收，CI 通过不等于生产投递已验证。

## 创建版本

合并且确认 `main` CI 通过后，从干净的 `main` 提交创建注释标签并推送。标签必须与源码版本一致。标签工作流会重新运行 CI，并确认标签提交已进入 `main`，再生成 ZIP、SHA-256 文件与 GitHub Release；任何验证失败不会创建发布条目。

```powershell
git status --short
git tag -a v0.5.0 -m "Release v0.5.0"
git push origin v0.5.0
```

标签示例仅用于 `0.5.0` 版本；后续先升版本，再按实际版本创建新标签。不要移动已发布的标签，也不要把 `state/`、`workspaces/`、`logs/`、`runs/`、`.env*` 或 `vault.key` 加入源码。

本地可预检发布包：

```powershell
.\.venv\Scripts\python.exe scripts\build-release.py --tag v0.5.0
```

## 部署与回滚

部署前备份并同时保留 `state/` 的数据库与密钥、`workspaces/` 和环境配置。停止旧进程后再用发布包更新程序文件，保留上述目录，安装运行依赖并启动；同一个状态目录不得由两个版本并行发送。用管理员页面检查版本、来源、工作区、队列和通道状态，再用真实目标做小范围投递验收。若发生问题，停止新版本，恢复上一个发布包；发生不兼容数据迁移时需配套恢复部署前的数据备份。UNKNOWN 投递不得盲目重发。
