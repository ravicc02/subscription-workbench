import asyncio
import base64
import json
import select
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.app.api import create_app
from backend.app.network import Network, RedirectResponse
from backend.app.proxy import ProxyError


def relay(left, right):
    try:
        while True:
            ready, _, _ = select.select([left, right], [], [], 5)
            if not ready:
                return
            for stream in ready:
                data = stream.recv(65536)
                if not data:
                    return
                (right if stream is left else left).sendall(data)
    finally:
        right.close()


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.received.append({"path": self.path, "headers": dict(self.headers)})
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/feed")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        if self.path == "/feed":
            self.wfile.write(
                f"<rss><channel><item><title>Proxy feed item</title><link>http://127.0.0.1:{self.server.server_port}/article</link></item></channel></rss>".encode()
            )
        else:
            self.wfile.write(
                b"<html><article><p>This is a real body fetched through the configured proxy transport.</p></article></html>"
            )


class HttpProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_CONNECT(self):
        self.server.connections.append(self.path)
        expected = self.server.auth
        if (
            expected
            and self.headers.get("Proxy-Authorization")
            != "Basic " + base64.b64encode(expected.encode()).decode()
        ):
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        host, port = self.path.rsplit(":", 1)
        remote = socket.create_connection((host.strip("[]"), int(port)), timeout=3)
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        relay(self.connection, remote)
        self.close_connection = True


class SocksProxy(socketserver.StreamRequestHandler):
    def handle(self):
        v, n = self.rfile.read(2)
        methods = self.rfile.read(n)
        method = 2 if self.server.auth else 0
        if v != 5 or method not in methods:
            return
        self.wfile.write(bytes([5, method]))
        self.wfile.flush()
        if method == 2:
            version, ulen = self.rfile.read(2)
            user = self.rfile.read(ulen)
            plen = self.rfile.read(1)[0]
            pwd = self.rfile.read(plen)
            ok = version == 1 and (user + b":" + pwd).decode() == self.server.auth
            self.wfile.write(bytes([1, 0 if ok else 1]))
            self.wfile.flush()
            if not ok:
                return
        version, cmd, _, kind = self.rfile.read(4)
        if version != 5 or cmd != 1:
            return
        if kind == 1:
            host = socket.inet_ntoa(self.rfile.read(4))
        elif kind == 4:
            host = socket.inet_ntop(socket.AF_INET6, self.rfile.read(16))
        else:
            host = self.rfile.read(self.rfile.read(1)[0]).decode()
        port = struct.unpack("!H", self.rfile.read(2))[0]
        self.server.connections.append((host, port))
        remote = socket.create_connection((host, port), timeout=3)
        self.wfile.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        self.wfile.flush()
        relay(self.connection, remote)


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def tunnels():
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin.received = []
    http = ThreadingHTTPServer(("127.0.0.1", 0), HttpProxy)
    http.connections = []
    http.auth = "proxy-user:proxy-password"
    socks = ThreadedServer(("127.0.0.1", 0), SocksProxy)
    socks.connections = []
    socks.auth = "proxy-user:proxy-password"
    for server in (origin, http, socks):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield origin, http, socks
    for server in (origin, http, socks):
        server.shutdown()
        server.server_close()


def config(server, scheme):
    return {
        "enabled": True,
        "scheme": scheme,
        "host": "127.0.0.1",
        "port": server.server_address[1],
        "username": "proxy-user",
        "password": "proxy-password",
        "remote_dns": False,
    }


@pytest.mark.parametrize("scheme", ["http", "socks5"])
def test_authenticated_proxy_carries_real_requests_without_credential_leak(tunnels, scheme):
    origin, http, socks = tunnels
    proxy = http if scheme == "http" else socks
    net = Network(["127.0.0.1"])
    url = f"http://127.0.0.1:{origin.server_port}/redirect"
    raw, final, _ = net._request(
        "GET", url, headers={"Authorization": "Origin-secret"}, proxy=config(proxy, scheme)
    )
    assert b"Proxy feed item" in raw and final.endswith("/feed")
    assert len(proxy.connections) == 2
    assert origin.received[0]["headers"]["Authorization"] == "Origin-secret"
    assert all("Proxy-Authorization" not in r["headers"] for r in origin.received)
    assert "Authorization" not in origin.received[1]["headers"]


def test_source_request_can_stop_before_following_redirect(tunnels):
    origin, _, _ = tunnels
    url = f"http://127.0.0.1:{origin.server_port}/redirect"
    response = Network(["127.0.0.1"])._request("GET", url, follow_redirects=False)
    assert response == RedirectResponse(f"http://127.0.0.1:{origin.server_port}/feed", 302)
    assert [request["path"] for request in origin.received] == ["/redirect"]


@pytest.mark.parametrize("scheme", ["http", "socks5"])
def test_wrong_proxy_credentials_do_not_fall_back_to_direct(tunnels, scheme):
    origin, http, socks = tunnels
    proxy = http if scheme == "http" else socks
    cfg = config(proxy, scheme)
    cfg["password"] = "incorrect-password"
    with pytest.raises(ProxyError):
        Network(["127.0.0.1"])._request(
            "GET", f"http://127.0.0.1:{origin.server_port}/feed", proxy=cfg
        )
    assert not origin.received


def test_proxy_settings_are_encrypted_revisioned_and_collection_only(tmp_path, tunnels):
    origin, http, _ = tunnels
    app = create_app(tmp_path, worker=False, network=Network(["127.0.0.1"]))
    with TestClient(app) as client:
        client.headers["X-Requested-With"] = "workbench"
        assert (
            client.post("/api/auth/setup", json={"password": "test-password-long"}).status_code
            == 200
        )
        prefs = client.get("/api/network").json()
        assert not prefs["enabled"] and not prefs["configured"]
        body = {**config(http, "http"), "revision": 0}
        response = client.post("/api/network", json=body)
        assert response.status_code == 200
        public = response.json()
        assert public["has_password"] and "password" not in public
        state = app.state.workbench.store.setting("collection_proxy")
        assert "proxy-password" not in json.dumps(state) and "proxy-user" not in json.dumps(state)
        assert client.post("/api/network", json=body).status_code == 409
        source = client.post(
            "/api/sources",
            json={
                "name": "Proxied RSS",
                "type": "rss",
                "url": f"http://127.0.0.1:{origin.server_port}/feed",
                "domains": ["127.0.0.1"],
                "poll_minutes": 5,
                "proxy_mode": "inherit",
                "selectors": ["article"],
                "min_body_characters": 1,
                "min_body_paragraphs": 1,
                "min_paragraph_characters": 1,
            },
        ).json()
        w = app.state.workbench
        w.policy.settings["minimum_request_gap_seconds"] = 0

        async def fetch_pair():
            rows = await w.sources.discover(source)
            return await w.sources.capture(rows[0], source)

        body = asyncio.run(fetch_pair())
        assert body["content_level"] == "full_text" and "real body" in body["text"]
        assert len(http.connections) == 2
        assert w.effective_proxy({**source, "proxy_mode": "direct"}) is None
        assert (
            w.net is w.channels.net and w.editor.net is w.net
        )  # only Sources passes an explicit proxy
        assert (
            client.post(
                "/api/network/test",
                json={
                    "source_id": source["id"],
                    "route": "proxy",
                    "config": {**config(http, "http"), "password": ""},
                },
            ).status_code
            == 200
        )
        assert not w.store.db.execute("SELECT 1 FROM jobs").fetchone()
        assert not w.store.db.execute("SELECT 1 FROM inputs").fetchone()
        assert "proxy-password" not in client.get("/api/overview").text


def test_proxy_remote_dns_is_pinned_cached_and_rejects_private_answers(monkeypatch):
    net = Network()
    calls = []

    def doh(method, url, **kwargs):
        calls.append((url, kwargs))
        return json.dumps({"Answer": [{"type": 1, "data": "8.8.8.8", "TTL": 60}]}).encode(), url, {}

    monkeypatch.setattr(net, "_request", doh)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Local destination DNS must not be used")
        ),
    )
    cfg = {"scheme": "http", "host": "127.0.0.1", "port": 7890, "remote_dns": True}
    assert net.destination("https://blocked.example/feed", cfg)[1] == "8.8.8.8"
    assert (
        net.destination("https://blocked.example/article", cfg)[1] == "8.8.8.8" and len(calls) == 1
    )
    assert calls[0][1]["_pinned_ip"] == "1.1.1.1" and not calls[0][1]["proxy"]["remote_dns"]

    def private(method, url, **kwargs):
        return b'{"Answer":[{"type":1,"data":"127.0.0.1"}]}', url, {}

    monkeypatch.setattr(net, "_request", private)
    with pytest.raises(ValueError):
        net.destination("https://another.example/feed", cfg)
    with pytest.raises(ValueError):
        net.destination("https://127.0.0.1/private", cfg)


def test_explicit_source_proxy_needs_configuration_and_secret_not_reused_across_hosts(
    tmp_path, tunnels
):
    _, http, _ = tunnels
    app = create_app(tmp_path, worker=False, network=Network(["127.0.0.1"]))
    w = app.state.workbench
    with pytest.raises(ValueError):
        w.effective_proxy({"proxy_mode": "proxy"})
    w.save_proxy_preferences({**config(http, "http"), "revision": 0})
    assert w.effective_proxy({"proxy_mode": "direct"}) is None
    current = w.proxy_preferences()
    with pytest.raises(ValueError):
        w.save_proxy_preferences({**current, "host": "new.example", "password": ""})
    w.store.db.close()
