"""Explicit HTTP CONNECT and SOCKS5 transports. Proxy credentials never go to the origin."""

import base64
import http.client
import ipaddress
import socket
import struct


class ProxyError(OSError):
    pass


def exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise ProxyError("代理在连接过程中关闭了通道")
        data += part
    return data


def connect(config, target_ip, target_port, timeout):
    host = config["host"]
    port = config["port"]
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        records.sort(key=lambda r: ipaddress.ip_address(r[4][0]).version)
        sock = socket.create_connection((records[0][4][0], port), timeout=timeout)
    except (OSError, IndexError) as exc:
        raise ProxyError("无法连接采集代理，请检查代理地址、端口及服务是否启动") from exc
    try:
        username = config.get("username", "")
        password = config.get("password", "")
        if config["scheme"] == "http":
            authority = (
                ("[" + target_ip + "]" if ":" in target_ip else target_ip) + ":" + str(target_port)
            )
            headers = [
                "CONNECT " + authority + " HTTP/1.1",
                "Host: " + authority,
                "Proxy-Connection: keep-alive",
            ]
            if username or password:
                headers.append(
                    "Proxy-Authorization: Basic "
                    + base64.b64encode((username + ":" + password).encode()).decode()
                )
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            status = response.status
            response.close()
            if status != 200:
                raise ProxyError(
                    "HTTP 代理隧道被拒绝（状态 "
                    + str(status)
                    + "）"
                    + ("，请检查认证信息" if status == 407 else "")
                )
        else:
            sock.sendall(b"\x05\x02\x00\x02" if username or password else b"\x05\x01\x00")
            version, method = exact(sock, 2)
            if version != 5:
                raise ProxyError("代理返回的 SOCKS 版本不正确")
            if method == 2:
                user = username.encode()
                pwd = password.encode()
                if not 1 <= len(user) <= 255 or not 1 <= len(pwd) <= 255:
                    raise ProxyError("SOCKS5 用户名和密码均需填写，且不超过 255 字节")
                sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
                if exact(sock, 2) != b"\x01\x00":
                    raise ProxyError("SOCKS5 认证失败，请检查用户名和密码")
            elif method != 0:
                raise ProxyError("SOCKS5 代理没有接受可用的认证方式")
            address = ipaddress.ip_address(target_ip)
            atyp = b"\x01" if address.version == 4 else b"\x04"
            sock.sendall(b"\x05\x01\x00" + atyp + address.packed + struct.pack("!H", target_port))
            version, result, _, kind = exact(sock, 4)
            if version != 5 or result != 0:
                raise ProxyError("SOCKS5 代理拒绝目标连接（代码 " + str(result) + "）")
            length = 4 if kind == 1 else 16 if kind == 4 else exact(sock, 1)[0] if kind == 3 else 0
            if not length:
                raise ProxyError("SOCKS5 返回地址格式无效")
            exact(sock, length + 2)
        return sock
    except BaseException:
        sock.close()
        raise
