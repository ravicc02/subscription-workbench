import asyncio
from urllib.error import HTTPError

import pytest

from backend.app.network import Network, RedirectResponse, RemoteError
from backend.app.polling import DeferredRequest, RequestPolicy
from backend.app.sources import Sources


def test_article_requests_share_target_site_cooldown_across_feeds(tmp_path):
    path = tmp_path / "http-state.json"
    policy = RequestPolicy(
        path,
        {"minimum_request_gap_seconds": 10, "jitter_seconds": 0},
        clock=lambda: 1000.0,
        sleep=lambda _: None,
        jitter=lambda *_: 0,
    )
    feed_a = {"url": "https://first.example/rss"}
    feed_b = {"url": "https://second.example/rss"}
    article_a = "https://articles.example/one"
    article_b = "https://articles.example/two"
    requests = []

    def rate_limited():
        requests.append("article")
        raise HTTPError(article_a, 429, "Rate limited", {"Retry-After": "60"}, None)

    with pytest.raises(DeferredRequest) as exc:
        policy.fetch(article_a, feed_a, rate_limited)
    assert exc.value.retry_at >= 1060
    assert policy.available_at(feed_a) == 0
    assert policy.available_at({"url": article_a}) >= 1060

    with pytest.raises(DeferredRequest):
        policy.fetch(article_b, feed_b, lambda: requests.append("other article"))
    assert requests == ["article"]
    assert policy.fetch(feed_b["url"], feed_b, lambda: "feed body") == "feed body"

    restored = RequestPolicy(path, {"minimum_request_gap_seconds": 10}, clock=lambda: 1000.0)
    with pytest.raises(DeferredRequest):
        restored.fetch(article_b, feed_b, lambda: requests.append("after restart"))
    assert requests == ["article"]


def test_article_requests_from_different_feeds_share_target_request_gap(tmp_path):
    now = [1000.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    policy = RequestPolicy(
        tmp_path / "http-state.json",
        {"minimum_request_gap_seconds": 10},
        clock=lambda: now[0],
        sleep=sleep,
    )
    first = {"url": "https://first.example/rss"}
    second = {"url": "https://second.example/rss"}
    article = "https://articles.example/story"

    assert policy.fetch(article, first, lambda: "one") == "one"
    assert policy.fetch(article, second, lambda: "two") == "two"
    assert waits == [10]


def test_redirect_target_429_cools_target_not_feed(tmp_path):
    feed = {"url": "https://feed.example/rss"}
    target = "https://articles.example/story"

    class RedirectingNetwork(Network):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _request(self, method, url, **kwargs):
            self.calls.append(url)
            if url == feed["url"]:
                return RedirectResponse(target, 302)
            raise RemoteError(429, "60")

    net = RedirectingNetwork()
    policy = RequestPolicy(
        tmp_path / "http-state.json",
        {"jitter_seconds": 0},
        clock=lambda: 1000.0,
        sleep=lambda _: None,
        jitter=lambda *_: 0,
    )
    sources = Sources(net, policy)
    with pytest.raises(DeferredRequest) as exc:
        asyncio.run(sources.fetch(feed["url"], feed))
    assert exc.value.status == 429
    assert net.calls == [feed["url"], target]
    assert policy.available_at(feed) == 0
    assert policy.summary()["sites"]["feed.example"]["status"] == 302
    assert policy.available_at({"url": target}) > 1000
    with pytest.raises(DeferredRequest):
        asyncio.run(sources.fetch(target, {"url": "https://other.example/rss"}))
    assert net.calls == [feed["url"], target]


def test_article_redirect_outside_allowlist_is_rejected_before_fetch(tmp_path):
    article = "https://articles.example/start"
    outside = "https://unexpected.example/story"

    class RedirectingNetwork(Network):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _request(self, method, url, **kwargs):
            self.calls.append(url)
            return RedirectResponse(outside, 302)

    net = RedirectingNetwork()
    sources = Sources(net, RequestPolicy(tmp_path / "http-state.json", {}))
    source = {"url": "https://feed.example/rss", "domains": ["articles.example"]}
    row = {"url": article, "content_type": "article", "summary": ""}
    with pytest.raises(ValueError, match="outside source allowlist"):
        asyncio.run(sources.capture(row, source))
    assert net.calls == [article]


def test_same_site_redirect_observes_gap_without_nested_lock(tmp_path):
    now = [1000.0]
    waits = []
    start = "https://feed.example/start"
    end = "https://feed.example/final"

    class RedirectingNetwork(Network):
        def _request(self, method, url, **kwargs):
            return RedirectResponse(end, 302) if url == start else (b"ok", url, {})

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    policy = RequestPolicy(
        tmp_path / "http-state.json",
        {"minimum_request_gap_seconds": 10},
        clock=lambda: now[0],
        sleep=sleep,
    )
    result = asyncio.run(Sources(RedirectingNetwork(), policy).fetch(start, {"url": start}))
    assert result == (b"ok", end, {})
    assert waits == [10]
