"""Read-only provider checks and unconfirmed WeChat QR registration; no recipient sends."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.agent import Editor
from backend.app.channels import Channels
from backend.app.core import Store, Workspaces
from backend.app.network import Network

root = Path(__file__).resolve().parents[1]


async def main():
    store = Store(root)
    net = Network()
    out = {}
    models = store.all("model")
    if models:
        editor = Editor(store, Workspaces(store), net)
        tool = {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "Verify tool calling",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
        }
        try:
            v = await editor.completion(
                models[0],
                [
                    {
                        "role": "user",
                        "content": "Call the probe tool once with ok=true. Do not explain.",
                    }
                ],
                [tool],
            )
            calls = v["choices"][0]["message"].get("tool_calls") or []
            out["live_model"] = {
                "model": models[0]["model"],
                "tool_calling": any(c.get("function", {}).get("name") == "probe" for c in calls),
                "usage": v.get("usage", {}),
            }
        except Exception as exc:
            out["live_model"] = {"error_type": type(exc).__name__}
    try:
        config, status, _ = await Channels(net).begin("weixin", {})
        out["live_weixin_qr"] = {
            "status": status,
            "valid_png": config["qr_image"].startswith("data:image/png;base64,"),
            "scan_completed": False,
            "real_wechat_send_performed": False,
        }
    except Exception as exc:
        out["live_weixin_qr"] = {
            "error_type": type(exc).__name__,
            "scan_completed": False,
            "real_wechat_send_performed": False,
        }
    folder = root / "runs/verification"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "live-services.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(out, ensure_ascii=True))
    store.db.close()


asyncio.run(main())
