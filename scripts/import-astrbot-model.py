"""One-time local provider migration. Does not import platform login state or print keys."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.core import Store

p = argparse.ArgumentParser()
p.add_argument("config")
p.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
args = p.parse_args()
v = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
store = Store(args.root)
count = 0
for provider in v.get("provider", []):
    if not provider.get("enable"):
        continue
    src = next(
        (
            s
            for s in v.get("provider_sources", [])
            if s.get("id") == provider.get("provider_source_id") and s.get("enable")
        ),
        None,
    )
    if not src or src.get("type") != "openai_chat_completion":
        continue
    raw = src.get("key") or []
    key = next((x for x in raw if x), None) if isinstance(raw, list) else raw
    existing = next(
        (m for m in store.all("model") if m.get("imported_from") == provider["id"]),
        None,
    )
    model = {
        "name": provider["model"],
        "model": provider["model"],
        "base_url": src["api_base"].rstrip("/"),
        "secret": store.seal({"api_key": key}) if key else store.seal({}),
        "temperature": 0.2,
        "max_tokens": 8000,
        "timeout": min(180, max(30, int(src.get("timeout") or 90))),
        "imported_from": provider["id"],
    }
    if existing:
        model["id"] = existing["id"]
    saved = store.put("model", model)
    count += 1
    if not store.setting("default_model"):
        store.set_setting("default_model", saved["id"])
store.audit("migration", "models", "IMPORTED", {"count": count})
store.db.close()
print(
    "Enabled compatible models imported:",
    count,
    "; credentials encrypted; platform state untouched.",
)
