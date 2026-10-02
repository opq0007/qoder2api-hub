#!/usr/bin/env python3
"""账号级 IP 代理隧道（纯标准库，零依赖）。

把官方桌面/CLI 客户端同款的上游链路套进一个**按账号**的出站代理：
同一台宿主机上的不同账号可以走不同出口 IP，且配了代理的账号**绝不回退
直连**（fail-closed），从网关侧保证宿主机真实 IP 不出现在上游日志里。

支持的标准代理协议（鉴权走 URL userinfo `user:pass@host:port`，与
curl/环境变量习惯一致）：

    http://…     经 HTTP 代理，对目标用 CONNECT 建隧道
    https://…    同上，但与代理本身先 TLS（代理侧证书照常校验）
    socks5://    SOCKS5，域名**本地解析**后发 IP（标准 socks5 语义）
    socks5h://   SOCKS5，域名交给代理**远端解析**（防本机 DNS 泄漏）

设计要点：

- **fail-closed**：隧道建立（TCP/CONNECT/SOCKS5 握手/鉴权）一旦失败，抛
  `ProxyTunnelError`（OSError 子类，能穿过 urllib 的 URLError 包装被识别），
  调用方据此熔断/换号，**不存在"代理失败改直连"的路径**。
- **零直连出口**：`build_opener()` 手工组装 OpenerDirector，只挂代理隧道
  handler；并显式 `ProxyHandler({})` 屏蔽环境变量代理（HTTP_PROXY 等），
  防止它们意外接管造成泄漏。
- **DNS**：走代理时由调用方跳过本地 `getaddrinfo` 校验（域名由代理解析）；
  代理服务器本身允许是 127.0.0.1/内网（本地 Clash 等分端口场景）。
- 目标 TLS 不受影响：CONNECT/SOCKS5 隧道之上照常 `wrap_socket`，端到端
  证书校验与直连完全一致。
- 全部纯标准库实现（SOCKS5/CONNECT 握手手写），python:3.11-alpine 可直接跑。
"""
import base64
import functools
import http.client
import ipaddress
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
EXIT_IP_URL = "https://api.ipify.org/?format=json"


def proxy_connect_timeout():
    """隧道建立超时（秒）。业务读写超时由调用方的 req timeout 决定。"""
    try:
        value = float(os.environ.get("QD_PROXY_CONNECT_TIMEOUT", "10") or 10)
    except Exception:
        value = 10.0
    if value <= 0:
        value = 10.0
    return value


class ProxyConfig(object):
    """解析后的代理配置（与原始字符串一一对应）。"""

    __slots__ = ("scheme", "host", "port", "username", "password", "raw")

    def __init__(self, scheme, host, port, username=None, password="", raw=""):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.raw = raw

    @property
    def remote_dns(self):
        """域名是否交给代理远端解析（socks5h / CONNECT 天然远端解析）。"""
        return self.scheme in ("http", "https", "socks5h")

    def mask(self):
        """脱敏展示：socks5h://us***@1.2.3.4:1080（绝不包含完整凭据）。"""
        host = "[%s]" % self.host if ":" in self.host else self.host
        who = (self.username[:2] + "***") if self.username else "-"
        return "%s://%s@%s:%d" % (self.scheme, who, host, self.port)

    def __repr__(self):
        return "ProxyConfig(%s)" % self.mask()


class ProxyTunnelError(OSError):
    """代理隧道建立失败（连接/握手/鉴权/转发被拒）。

    继承 OSError 以便穿过 urllib 的 `URLError(reason=…)` 包装仍可被
    `is_proxy_error()` 识别；`stage` 标记失败环节便于诊断。
    """

    def __init__(self, message, stage="tunnel"):
        super().__init__(message)
        self.stage = stage


class ProxyPausedError(ProxyTunnelError):
    """账号已因代理连续失败被熔断暂停：请求未出网即快速失败。"""


def parse_proxy_url(value):
    """解析单行代理 URL。空值 -> None；非法 raise ValueError。

    支持 `scheme://[user:pass@]host:port`，userinfo 按 URL 百分号解码
    （与 requests/curl 对代理串的通行处理一致）。仅取 userinfo 里第一个
    `:` 分隔用户名与密码，密码本身可含 `:`。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        raise ValueError("代理缺少协议前缀（支持 %s），示例 "
                         "socks5h://user:pass@1.2.3.4:1080"
                         % ", ".join(PROXY_SCHEMES))
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise ValueError("代理 URL 非法: %s" % exc)
    scheme = (parts.scheme or "").strip().lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("不支持的代理协议 %r（支持 %s）"
                         % (scheme, ", ".join(PROXY_SCHEMES)))
    host = (parts.hostname or "").strip().strip("[]").lower()
    if not host:
        raise ValueError("代理缺少主机地址")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("代理端口非法: %s" % exc)
    if port is None:
        raise ValueError("代理缺少端口（示例 socks5h://user:pass@1.2.3.4:1080）")
    if not (1 <= port <= 65535):
        raise ValueError("代理端口超出范围 (1-65535): %d" % port)
    username = urllib.parse.unquote(parts.username) \
        if parts.username is not None else None
    password = urllib.parse.unquote(parts.password or "") \
        if parts.username is not None else ""
    return ProxyConfig(scheme, host, port, username, password, raw=text)


def mask_proxy(pcfg_or_url):
    """把 ProxyConfig/原始串脱敏成可安全写日志的形态；解析失败原样返回。"""
    if isinstance(pcfg_or_url, ProxyConfig):
        return pcfg_or_url.mask()
    try:
        cfg = parse_proxy_url(pcfg_or_url)
    except ValueError:
        return str(pcfg_or_url)[:80]
    return cfg.mask() if cfg is not None else ""


def error_text(exc):
    """剥掉 URLError 包装，取出可读的最内层错误文本。"""
    seen = 0
    while isinstance(exc, urllib.error.URLError) and seen < 5:
        exc = exc.reason
        seen += 1
    return str(exc)


def is_proxy_error(exc):
    """异常链上是否为本模块的代理隧道错误（含 URLError 包装形态）。"""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, (ProxyTunnelError, ProxyPausedError)):
            return True
        if isinstance(exc, urllib.error.URLError):
            exc = exc.reason
            seen += 1
            continue
        break
    return False


# ---------------------------------------------------------------------------
# 隧道握手（CONNECT / SOCKS5）
# ---------------------------------------------------------------------------
_SOCKS5_ERRORS = {
    1: "一般性失败 (general failure)",
    2: "规则不允许 (not allowed)",
    3: "网络不可达 (network unreachable)",
    4: "目标主机不可达 (host unreachable)",
    5: "连接被拒绝 (refused)",
    6: "TTL 过期",
    7: "命令不支持 (command not supported)",
    8: "目标地址类型不支持 (address type not supported)",
}


def _recv_exact(sock, size, pcfg, stage="handshake"):
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ProxyTunnelError(
                "代理 %s 连接中断（%s 未完成）" % (pcfg.mask(), stage), stage=stage)
        buf += chunk
    return buf


def _connect_target_bytes(host):
    """IP 字面量 -> (atyp, packed)；域名 -> None（交给调用方决定解析方式）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.version == 4:
        return b"\x01" + ip.packed
    return b"\x04" + ip.packed


def _http_connect(sock, pcfg, host, port, timeout):
    """HTTP(S) 代理：发 CONNECT 并校验 2xx，失败给出可读的中文原因。"""
    shown_host = "[%s]" % host if ":" in host else host
    lines = [
        "CONNECT %s:%d HTTP/1.1" % (shown_host, port),
        "Host: %s:%d" % (shown_host, port),
        "User-Agent: qoder-proxy/1.0",
    ]
    if pcfg.username is not None:
        token = base64.b64encode(
            ("%s:%s" % (pcfg.username, pcfg.password)).encode("utf-8")
        ).decode("ascii")
        lines.append("Proxy-Authorization: Basic " + token)
    sock.settimeout(timeout)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))
    # 逐字节读到头结束：保证不吞掉隧道上首包数据（虽然本场景客户端先说话）
    buf = b""
    while not buf.endswith(b"\r\n\r\n") and len(buf) < 16384:
        chunk = sock.recv(1)
        if not chunk:
            raise ProxyTunnelError(
                "代理 %s 连接中断（未返回 CONNECT 响应）" % pcfg.mask(),
                stage="handshake")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
    status = head.split("\r\n", 1)[0].strip()
    code = 0
    try:
        code = int(status.split(" ")[1])
    except Exception:
        pass
    if not code:
        raise ProxyTunnelError(
            "代理 %s 的 CONNECT 响应异常：%r" % (pcfg.mask(), status[:80]),
            stage="handshake")
    if code != 200:
        detail = {
            403: "被代理拒绝 (403 Forbidden)",
            405: "代理不允许 CONNECT (405)",
            407: "代理鉴权失败 (407，检查用户名/密码)",
        }.get(code, "HTTP %d" % code)
        raise ProxyTunnelError(
            "代理 %s 拒绝建立隧道：%s" % (pcfg.mask(), detail), stage="handshake")


def _socks5_connect(sock, pcfg, host, port, timeout):
    """SOCKS5：协商鉴权（0x00 无鉴权 / 0x02 用户名密码）+ CONNECT。"""
    sock.settimeout(timeout)
    methods = [0x00]
    if pcfg.username is not None:
        methods.append(0x02)
    sock.sendall(b"\x05" + bytes([len(methods)]) + bytes(methods))
    greeting = _recv_exact(sock, 2, pcfg)
    if greeting[0] != 0x05:
        raise ProxyTunnelError(
            "代理 %s 不是 SOCKS5 服务（响应版本 %d）" % (pcfg.mask(), greeting[0]),
            stage="handshake")
    if greeting[1] == 0x02:
        user = pcfg.username.encode("utf-8")
        pwd = pcfg.password.encode("utf-8")
        if len(user) > 255 or len(pwd) > 255:
            raise ProxyTunnelError(
                "代理 %s 鉴权信息过长（SOCKS5 限 255 字节）" % pcfg.mask(),
                stage="auth")
        sock.sendall(b"\x01" + bytes([len(user)]) + user
                     + bytes([len(pwd)]) + pwd)
        auth = _recv_exact(sock, 2, pcfg, stage="auth")
        if auth[1] != 0x00:
            raise ProxyTunnelError(
                "代理 %s 鉴权失败（SOCKS5 用户名或密码错误）" % pcfg.mask(),
                stage="auth")
    elif greeting[1] != 0x00:
        raise ProxyTunnelError(
            "代理 %s 不支持可用的鉴权方式（method=0x%02x）"
            % (pcfg.mask(), greeting[1]), stage="auth")

    # 目标地址编码：IP 字面量直接发；socks5 本地解析成 IP（标准语义）；
    # socks5h 交代理远端解析（防本机 DNS 泄漏）。
    target = _connect_target_bytes(host)
    if target is None and pcfg.scheme == "socks5":
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            raise ProxyTunnelError("无法解析目标主机 %r" % host)
        target = _connect_target_bytes(str(infos[0][4][0]))
    if target is not None:
        payload = b"\x05\x01\x00" + target
    else:
        hb = host.encode("utf-8")
        if len(hb) > 255:
            raise ProxyTunnelError("目标主机名过长: %r" % host[:60])
        payload = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
    payload += struct.pack(">H", port)
    sock.sendall(payload)
    reply = _recv_exact(sock, 4, pcfg)
    if reply[0] != 0x05:
        raise ProxyTunnelError(
            "代理 %s 的 SOCKS5 响应异常（版本 %d）" % (pcfg.mask(), reply[0]),
            stage="handshake")
    if reply[1] != 0x00:
        raise ProxyTunnelError(
            "代理 %s 拒绝转发到 %s:%d：%s"
            % (pcfg.mask(), host, port,
               _SOCKS5_ERRORS.get(reply[1], "rep=%d" % reply[1])),
            stage="connect")
    atyp = reply[3]
    if atyp == 0x01:
        _recv_exact(sock, 4 + 2, pcfg)
    elif atyp == 0x04:
        _recv_exact(sock, 16 + 2, pcfg)
    elif atyp == 0x03:
        n = _recv_exact(sock, 1, pcfg)[0]
        _recv_exact(sock, n + 2, pcfg)
    else:
        raise ProxyTunnelError(
            "代理 %s 返回未知地址类型 %d" % (pcfg.mask(), atyp),
            stage="handshake")


def tunnel_socket(pcfg, host, port, timeout=None):
    """在代理上为目标 host:port 建立一条 TCP 隧道，返回就绪的裸 socket。

    任何环节失败抛 ProxyTunnelError 并关闭半成品连接；调用方绝不回退直连。
    """
    t = proxy_connect_timeout() if timeout is None else timeout
    try:
        sock = socket.create_connection((pcfg.host, pcfg.port), timeout=t)
    except Exception as exc:
        raise ProxyTunnelError(
            "连接代理 %s 失败：%s" % (pcfg.mask(), exc), stage="connect")
    try:
        if pcfg.scheme == "https":
            try:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=pcfg.host)
            except Exception as exc:
                raise ProxyTunnelError(
                    "与代理 %s 的 TLS 握手失败：%s" % (pcfg.mask(), exc),
                    stage="proxy-tls")
        if pcfg.scheme in ("http", "https"):
            _http_connect(sock, pcfg, host, port, t)
        else:
            _socks5_connect(sock, pcfg, host, port, t)
        return sock
    except Exception as exc:
        try:
            sock.close()
        except Exception:
            pass
        if isinstance(exc, ProxyTunnelError):
            raise
        raise ProxyTunnelError(
            "代理 %s 隧道建立失败：%s" % (pcfg.mask(), exc), stage="handshake")


# ---------------------------------------------------------------------------
# urllib 接入：全隧道连接类 + 单出口 handler + opener 工厂
# ---------------------------------------------------------------------------
class ProxiedHTTPConnection(http.client.HTTPConnection):
    """普通 http 目标：TCP 层先钻代理隧道。"""

    def __init__(self, *args, pcfg=None, **kw):
        super().__init__(*args, **kw)
        self._pcfg = pcfg

    def connect(self):
        if self._pcfg is None:
            return super().connect()
        self.sock = tunnel_socket(self._pcfg, self.host, self.port)
        if isinstance(self.timeout, (int, float)) and self.timeout > 0:
            self.sock.settimeout(self.timeout)


class ProxiedHTTPSConnection(http.client.HTTPSConnection):
    """https 目标：先钻代理隧道，再在隧道上做端到端 TLS（证书照常校验）。"""

    def __init__(self, *args, pcfg=None, **kw):
        super().__init__(*args, **kw)
        self._pcfg = pcfg

    def connect(self):
        if self._pcfg is None:
            return super().connect()
        raw = tunnel_socket(self._pcfg, self.host, self.port)
        if isinstance(self.timeout, (int, float)) and self.timeout > 0:
            raw.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class ProxyTunnelHTTPHandler(urllib.request.HTTPHandler,
                             urllib.request.HTTPSHandler):
    """同一 handler 覆盖 http+https，确保 opener 内不存在直连出口。"""

    def __init__(self, pcfg, debuglevel=0):
        self._debuglevel = int(debuglevel or 0)
        self._pcfg = pcfg

    def _conn_cls(self, is_https):
        cls = ProxiedHTTPSConnection if is_https else ProxiedHTTPConnection
        return functools.partial(cls, pcfg=self._pcfg)

    def http_open(self, req):
        return self.do_open(self._conn_cls(False), req)

    def https_open(self, req):
        return self.do_open(self._conn_cls(True), req)


def build_opener(pcfg):
    """只经代理隧道的 opener（手工组装，零直连出口）。

    关键：完全不复用 urllib 的模块级 opener，也不挂任何从环境变量读取代理的
    ProxyHandler。`urllib.request.urlopen()` 默认会读 HTTP(S)_PROXY/PROXY 等
    环境变量并自动接管——那会把"账号代理"偷换成"宿主机环境代理"，破坏防泄漏
    语义。手工组装的 OpenerDirector 只认本模块的隧道 handler，环境变量无从生效。
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(ProxyTunnelHTTPHandler(pcfg))
    return opener


def open_url(opener, req, timeout=None):
    """统一出口：opener 为 None 保持原直连路径（urlopen），否则走代理隧道。"""
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def fetch_exit_ip(pcfg, timeout=15):
    """经代理访问 ipify 取出口 IP（第三方只看到代理 IP，不涉及宿主机）。

    返回 (exit_ip, latency_ms)；失败抛 ProxyTunnelError（含 HTTPError 传播）。
    """
    opener = build_opener(pcfg)
    t0 = time.monotonic()
    req = urllib.request.Request(
        EXIT_IP_URL, headers={"User-Agent": "qoder-proxy-check",
                              "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("ip") or ""), int((time.monotonic() - t0) * 1000)
