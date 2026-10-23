"""One-time classification of the bundled public feeds, without replacing user edits."""

import json


def apply_default_taxonomy(workbench):
    store = workbench.store
    if store.setting("default_taxonomy_v1"):
        return
    taxonomy = workbench.root / "docs/default-taxonomy.json"
    bundled = workbench.root / "docs/imported-sources.json"
    if not taxonomy.is_file() or not bundled.is_file():
        return
    spec = json.loads(taxonomy.read_text(encoding="utf-8"))
    feeds = {feed["id"]: feed for feed in json.loads(bundled.read_text(encoding="utf-8"))["feeds"]}
    if set(spec["sources"]) != set(feeds):
        raise ValueError("Default source taxonomy does not match the bundled feeds")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        categories = {item["key"]: item for item in store.all("category")}
        tags = {(item["category_id"], item["key"]): item for item in store.all("tag")}
        for category in spec["categories"]:
            current = categories.get(category["key"])
            if not current:
                current = workbench.save_category(category)
                categories[category["key"]] = current
            for tag in category["tags"]:
                key = (current["id"], tag["key"])
                if key not in tags:
                    tags[key] = workbench.save_tag({**tag, "category_id": current["id"]})
        sources = {item["id"]: item for item in store.all("source")}
        for feed_id, binding in spec["sources"].items():
            source = sources.get("source-" + feed_id)
            if (
                not source
                or source.get("category_id")
                or source.get("url") != feeds[feed_id]["url"]
            ):
                continue
            category = categories[binding["category"]]
            ids = [tags[(category["id"], key)]["id"] for key in binding["tags"]]
            workbench.save_source(
                {**source, "category_id": category["id"], "tag_ids": ids},
                network_checked=True,
            )
        store.set_setting("default_taxonomy_v1", True)
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
