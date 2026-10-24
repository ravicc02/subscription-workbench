"""Subscription source sets with compatibility for existing single-source bindings."""


def source_ids(subscription):
    values = subscription.get("source_ids")
    if values is None:
        return [subscription["source_id"]] if subscription.get("source_id") else []
    return list(values)


def resolved_source_ids(subscription, sources):
    category = subscription.get("category_id")
    if not category:
        return source_ids(subscription)
    tags = set(subscription.get("tag_ids") or [])
    return [
        source["id"]
        for source in sources
        if source.get("category_id") == category
        and (not tags or tags.intersection(source.get("tag_ids") or []))
    ]


def public_subscription(subscription, sources=None):
    ids = source_ids(subscription)
    visible = {
        key: value
        for key, value in subscription.items()
        if key not in ("keywords", "instructions")
    }
    return {
        **visible,
        "source_ids": ids,
        "source_id": ids[0] if ids else None,
        "category_id": subscription.get("category_id"),
        "tag_ids": subscription.get("tag_ids") or [],
        "resolved_source_ids": resolved_source_ids(subscription, sources or [])
        if sources is not None
        else ids,
    }
