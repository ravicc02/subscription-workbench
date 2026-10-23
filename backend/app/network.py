"""Bounded outbound HTTP. Resolve once and connect to the validated IP."""

import asyncio
import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit


class RemoteError(HTTPError):
    def __init__(self, status, retry_after=None):
        self.retry_after = retry_after
        super().__init__(
            "<redacted>",
            status,
            "Remote request failed",
            {"Retry-After": retry_after} if retry_after else {},
            None,
        )


class Network:
    def __init__(self, allowed_hosts=None):
        self.dns_cache = {}
        self.dns_lock = threading.RLock()
        self.allowed_hosts = set(
            allowed_hosts or filter(None, os.environ.get("WORKBENCH_ALLOWED_HOSTS", "").split(","))
        )

    def validate_url(self, url):
        u = urlsplit(url)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
            or u.fragment
        ):
            raise ValueError("Use a complete HTTP(S) URL without embedded credentials or fragments")
        local = u.hostname in self.allowed_hosts
        if not local:
            if u.scheme != "https":
                raise ValueError("Public endpoints must use HTTPS")
            name = u.hostname.lower().rstrip(".")
            if name == "localhost" or name.endswith((".localhost", ".local", ".internal")):
                raise ValueError("Private network address is not permitted")
            try:
                address = ipaddress.ip_address(name)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("Private network address is not permitted")
        return u

    def resolve(self, url):
        u = self.validate_url(url)
        addresses = socket.getaddrinfo(
            u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
        if not addresses:
            raise ValueError("Host did not resolve")
        if u.hostname not in self.allowed_hosts and any(
            not ipaddress.ip_address(a[4][0]).is_global for a in addresses
        ):
            raise ValueError("Private network address is not permitted")
        return u, addresses[0][4][0]

    def proxy_dns(self, hostname, proxy):
        from .proxy import ProxyError

        key = hashlib.sha256(json.dumps([hostname, proxy], sort_keys=True).encode()).hexdigest()
        with self.dns_lock:
            cached = self.dns_cache.get(key)
            if cached and cached[0] > time.time():
                return cached[1]
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if not literal.is_global:
                raise ValueError("Private network address is not permitted")
            return str(literal)
        answers = []
        for kind in ("A", "AAAA"):
            url = "https://cloudflare-dns.com/dns-query?" + urlencode(
                {"name": hostname.encode("idna").decode(), "type": kind}
            )
            try:
                raw, _, _ = self._request(
                    "GET",
                    url,
                    headers={"Accept": "application/dns-json"},
                    limit=64000,
                    timeout=12,
                    proxy={**proxy, "remote_dns": False},
                    _pinned_ip="1.1.1.1",
                )
                result = json.loads(raw)
            except (RemoteError, ValueError, json.JSONDecodeError) as exc:
                raise ProxyError(
                    "代理 DNS 查询失败，请检查代理是否支持访问 Cloudflare DoH"
                ) from exc
            answers = [r for r in result.get("Answer", []) if r.get("type") in (1, 28)]
            if answers:
                break
        if not answers:
            raise ProxyError("代理 DNS 未找到该订阅源的有效地址")
        addresses = [ipaddress.ip_address(r["data"]) for r in answers]
        if any(not address.is_global for address in addresses):
            raise ValueError("代理 DNS 返回了非公网地址，请检查订阅源")
        ip = str(addresses[0])
        ttl = min(300, max(30, min(int(r.get("TTL", 60)) for r in answers)))
        with self.dns_lock:
            if len(self.dns_cache) > 1000:
                self.dns_cache.clear()
            self.dns_cache[key] = (time.time() + ttl, ip)
        return ip

    def destination(self, url, proxy=None):
        u = self.validate_url(url)
        if proxy and proxy.get("remote_dns") and u.hostname not in self.allowed_hosts:
            return u, self.proxy_dns(u.hostname, proxy)
        return self.resolve(url)

    def _request(
        self,
        method,
        url,
        payload=None,
        headers=None,
        limit=2_000_000,
        timeout=30,
        redirects=0,
        proxy=None,
        _pinned_ip=None,
    ):
        if _pinned_ip is not None:
            u = self.validate_url(url)
            if (
                u.hostname != "cloudflare-dns.com"
                or u.path != "/dns-query"
                or _pinned_ip != "1.1.1.1"
            ):
                raise ValueError("Pinned DNS endpoint is invalid")
            ip = _pinned_ip
        else:
            u, ip = self.destination(url, proxy)
        port = u.port or (443 if u.scheme == "https" else 80)
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
        if proxy:
            from .proxy import connect

            conn.sock = connect(proxy, ip, port, timeout)
        else:
            conn.sock = socket.create_connection((ip, port), timeout=timeout)
        if u.scheme == "https":
            conn.sock = ssl.create_default_context().wrap_socket(
                conn.sock, server_hostname=u.hostname
            )
        h = {
            "User-Agent": "SubscriptionWorkbench/0.1",
            "Accept-Encoding": "gzip",
            **(headers or {}),
        }
        if any(
            not isinstance(k, str) or not isinstance(v, str) or "\r" in k + v or "\n" in k + v
            for k, v in h.items()
        ):
            raise ValueError("Invalid outbound header")
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        if body is not None:
            h["Content-Type"] = "application/json"
        try:
            conn.request(
                method,
                quote(
                    (u.path or "/") + ("?" + u.query if u.query else ""),
                    safe="/%?&=+;:@!$'()*,-._~",
                ),
                body,
                h,
            )
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                if method != "GET" or redirects >= 3 or _pinned_ip:
                    raise RemoteError(r.status)
                target = urljoin(url, r.getheader("Location", ""))
                clean = {
                    k: v
                    for k, v in h.items()
                    if k.lower() not in ("authorization", "cookie", "x-api-key")
                }
                return self._request(
                    "GET", target, None, clean, limit, timeout, redirects + 1, proxy=proxy
                )
            if r.status >= 400:
                raise RemoteError(r.status, r.getheader("Retry-After"))
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Remote response exceeds byte budget")
            if r.getheader("Content-Encoding", "").lower() == "gzip":
                data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Expanded response exceeds byte budget")
            return data, url, dict(r.getheaders())
        finally:
            conn.close()

    async def request(self, *args, **kwargs):
        return await asyncio.to_thread(self._request, *args, **kwargs)

    async def json(self, method, url, payload=None, headers=None, **kwargs):
        data, _, _ = await self.request(method, url, payload, headers, **kwargs)
        value = json.loads(data or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Remote endpoint did not return an object")
        return value
