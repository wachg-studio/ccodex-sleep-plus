#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ccodex-sleep-plus — Codex turn-state 本地网关（社区思路的独立增强实现）

机制与参考：github.com/gylive/ccodex-sleep-state（GPL-3.0）。本实现为独立重写：
- 零依赖单文件 Python（标准库 only），代码可审计
- 从 ~/.codex/auth.json 自举采集，无需先在 Codex 发消息
- 采集失败时默认 passthrough 兜底，不拦截正式请求
- 注入请求的响应 shape 异常只记 strike，不销毁已计费回复
- state 落盘，重启不丢；401 令牌刷新后自动解除封锁
- 自动发现本机代理出口（HTTP CONNECT / SOCKS5），多出口轮换
- 内置本地状态面板（仅监听 127.0.0.1）

免责：turn-state 规则是社区经验，不是官方指标，不保证效果；探测消耗真实额度。
"""
import argparse
import base64
import collections
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

# noconsole 打包（pythonw / PyInstaller --noconsole）下 stdout/stderr 为 None，
# 裸 print 会抛异常；统一重定向到 devnull，输出走 log()（同时落文件）。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf8")

try:
    from compression import zstd as _zstd
except ImportError:  # Python < 3.14
    _zstd = None

try:
    import modeltrace as _mt  # ModelTrace 归因（纯标准库移植，data/gpt_bank.json）
except Exception:
    _mt = None

APP = "ccodex-sleep-plus"
PROVIDER_ID = "sleep-plus"
STATE_HEADER = "X-Codex-Turn-State"
SUPPORTED_MODELS = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra")
DEFAULT_MODEL = "gpt-6-astra"

TTL_SECONDS = 3600          # state 有效期（社区经验值）
REFRESH_AHEAD = 1200        # 剩余低于该秒数时后台补采
COOLDOWN_SECONDS = 180      # 两轮探测的最小间隔
PROBE_TIMEOUT = 20          # 单次探测超时
MAX_PROBES_PER_ROUND = 4
MAX_BODY_BYTES = 64 << 20
UPSTREAM_HOST = "chatgpt.com"
UPSTREAM_BASE = "/backend-api/codex"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 17841

MANAGED_MARK = "# --- managed by ccodex-sleep-plus ---"

# 各账号规则接纳的密文块数集合。10/12 是社区经验旧形状（292/332 字符）；
# 33 块（780 字符）是 2026-09-22 官方新模型上线后的新形状
# （参见 gylive/ccodex-sleep-state issue #13 的实测反馈）。
PLAN_SHAPES = {"personal": {10, 33}, "team": {12, 33}}


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def config_path() -> Path:
    return codex_home() / "config.toml"


def auth_path() -> Path:
    return codex_home() / "auth.json"


# ---------------------------------------------------------------- 日志（脱敏） --
_LOG = collections.deque(maxlen=400)
_LOG_LOCK = threading.Lock()


def _write_log_file(line: str):
    try:
        d = data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"app-{time.strftime('%Y%m%d')}.log", "a", encoding="utf8") as f:
            f.write(line + "\n")
        old = time.time() - 7 * 86400
        for p in d.glob("app-*.log"):
            try:
                if p.stat().st_mtime < old:
                    p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def log(event: str, level: str = "info", **fields):
    safe = {}
    for k, v in fields.items():
        if k.lower() in ("authorization", "token", "value", "state", "access_token"):
            v = "<redacted>"
        safe[k] = v
    line = f"{time.strftime('%H:%M:%S')} [{level:5s}] {event} " + " ".join(
        f"{k}={v}" for k, v in safe.items() if v not in ("", None))
    with _LOG_LOCK:
        _LOG.append(line)
    _write_log_file(line)
    try:  # pythonw 后台运行时 stdout 为 None，print 会抛异常
        print(line, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- turn-state --
class State:
    __slots__ = ("value", "issued", "blocks", "fingerprint")

    def __init__(self, value, issued, blocks):
        self.value = value
        self.issued = issued
        self.blocks = blocks
        self.fingerprint = hashlib.sha256(value.encode()).hexdigest()[:12]


def parse_state(value: str):
    """按原项目 token.go 的封装规则解析；解析失败返回 None。"""
    if not value:
        return None
    value = value.strip()
    if len(value) > 2048 or re.search(r"[\s\r\n\t]", value):
        return None
    core = value.rstrip("=")
    if len(value) - len(core) > 2:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", value):
        return None
    try:
        raw = base64.urlsafe_b64decode(core + "=" * (-len(core) % 4))
    except Exception:
        return None
    if len(raw) < 73 or raw[0] != 0x80 or (len(raw) - 57) % 16 != 0:
        return None
    issued = int.from_bytes(raw[1:9], "big")
    if not (1577836800 <= issued < 4102444800):
        return None
    return State(value, issued, (len(raw) - 57) // 16)


def state_accepts(st: "State | None", blocks: set, now: float) -> bool:
    return (st is not None and st.blocks in blocks
            and st.issued <= now + 30
            and now < st.issued + TTL_SECONDS - 30)


class StateStore:
    """active + 备用实例池 + strike 计数；对应原项目 store.go 的增强移植。"""

    POOL_SIZE = 3

    def __init__(self, blocks):
        self.blocks = blocks if isinstance(blocks, set) else {blocks}
        self.lock = threading.Lock()
        self.active = None
        self.pool = []             # 备用实例，按 issued 降序
        self.strikes = 0
        self.version = 0

    def acquire(self, now):
        with self.lock:
            return self.active if state_accepts(self.active, self.blocks, now) else None

    def offer(self, st: State):
        with self.lock:
            if not state_accepts(st, self.blocks, time.time()):
                return False
            if self.active and self.active.fingerprint == st.fingerprint:
                return True
            if not state_accepts(self.active, self.blocks, time.time()):
                self.version += 1
                self.active = st
                self.strikes = 0
                return True
            pool = [p for p in self.pool if p.fingerprint != st.fingerprint]
            pool.append(st)
            pool.sort(key=lambda s: s.issued, reverse=True)
            self.pool = pool[:self.POOL_SIZE]
            self._promote()
            return True

    def _promote(self):  # caller holds lock
        now = time.time()
        while self.pool:
            cand = self.pool[0]
            if not state_accepts(cand, self.blocks, now):
                self.pool.pop(0)
                continue
            if (cand.fingerprint != (self.active.fingerprint if self.active else "")
                    and (not state_accepts(self.active, self.blocks, now) or self.strikes >= 2)):
                self.version += 1
                self.active = self.pool.pop(0)
                self.strikes = 0
            break

    def observe(self, header_value: str, used: "State | None") -> bool:
        """观察注入请求响应里的 state；返回是否可疑。绝不据此销毁响应体。"""
        if not header_value:
            return False
        st = parse_state(header_value)
        suspect = st is None or not state_accepts(st, self.blocks, time.time())
        with self.lock:
            if used is not None and self.active and used.fingerprint == self.active.fingerprint:
                self.strikes = self.strikes + 1 if suspect else 0
            self._promote()
        if st is not None and not suspect:
            self.offer(st)   # 响应里带来的合格新实例也入池（offer 自带锁，必须在锁外调用）
        return suspect

    def needs_refresh(self, now):
        with self.lock:
            a = self.active
            return (not state_accepts(a, self.blocks, now)
                    or self.strikes >= 2
                    or (a is not None and now + REFRESH_AHEAD > a.issued + TTL_SECONDS))

    def clear(self):
        with self.lock:
            self.active = None
            self.pool = []
            self.strikes = 0
            self.version += 1

    def snapshot(self):
        with self.lock:
            a = self.active
            return {
                "usable": state_accepts(a, self.blocks, time.time()),
                "fingerprint": a.fingerprint if a else "",
                "blocks": a.blocks if a else 0,
                "issued": a.issued if a else 0,
                "remaining": max(0, int(a.issued + TTL_SECONDS - time.time())) if a else 0,
                "strikes": self.strikes,
                "version": self.version,
                "pool_size": len(self.pool),
                "expected_blocks": sorted(self.blocks),
            }


# ---------------------------------------------------------------- 出口管理 --
class Egress:
    def __init__(self, eid, label, kind, host, port):
        self.id = eid
        self.label = label
        self.kind = kind          # direct | http | socks5
        self.host = host
        self.port = port
        self.last_ok = 0.0
        self.last_error = ""
        self.uses = 0
        self.quality = "unknown"      # 综合判定（多数表决结果）
        self.quality_answer = ""
        self.quality_history = []     # 最近 3 次单次判定 [{"q","at","src"}]
        self.attribution = None       # ModelTrace 深度归因结果

    def record_quality(self, single: str, src: str = "probe") -> str:
        """滚动窗口多数表决：最近 3 次中 2 票一致才定性；单次 unknown 不投票。
        深度归因 (src=attribution) 为终审，直接覆盖历史。返回综合判定。"""
        now = int(time.time())
        prev = self.quality
        if src == "attribution":
            self.quality_history = [{"q": single, "at": now, "src": "attribution"}]
            self.quality = single
            self._emit_heal_event(prev)
            return self.quality
        if single and single != "unknown":
            self.quality_history.append({"q": single, "at": now, "src": src})
            self.quality_history = self.quality_history[-3:]
        votes = [h["q"] for h in self.quality_history]
        if votes:
            counts = {v: votes.count(v) for v in set(votes)}
            best, n = max(counts.items(), key=lambda kv: kv[1])
            self.quality = best if (n >= 2 or len(votes) == 1 and len(self.quality_history) == 1) else "unknown"
        self._emit_heal_event(prev)
        return self.quality

    def _emit_heal_event(self, prev: str):
        """记录降智检出 / 恢复事件（供面板统计自愈效果）。"""
        now = int(time.time())
        bad = ("degraded", "severely")
        if prev not in bad and self.quality in bad:
            self.quality_events = getattr(self, "quality_events", collections.deque(maxlen=50))
            self.quality_events.append({"at": now, "kind": "detected"})
        elif prev in bad and self.quality not in bad and self.quality != "unknown":
            self.quality_events = getattr(self, "quality_events", collections.deque(maxlen=50))
            self.quality_events.append({"at": now, "kind": "recovered"})
            self.recovered_at = now          # 供引擎下一轮立即补采新鲜满血 state

    def open_socket(self, dst_host, dst_port, timeout) -> socket.socket:
        if self.kind == "direct":
            s = socket.create_connection((dst_host, dst_port), timeout=timeout)
            s.settimeout(timeout)
            return s
        if self.kind == "http":
            return self._http_connect(dst_host, dst_port, timeout)
        if self.kind == "socks5":
            return self._socks5_connect(dst_host, dst_port, timeout)
        raise OSError(f"unknown egress kind {self.kind}")

    def _http_connect(self, dst_host, dst_port, timeout):
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(timeout)
        req = (f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\n"
               f"Host: {dst_host}:{dst_port}\r\n\r\n").encode()
        s.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise OSError("proxy closed during CONNECT")
            buf += chunk
            if len(buf) > 65536:
                raise OSError("proxy CONNECT response too large")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 200 " not in status_line + " ":
            raise OSError(f"proxy CONNECT failed: {status_line.strip()}")
        return s

    def _socks5_connect(self, dst_host, dst_port, timeout):
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")                       # greeting: no-auth
        resp = self._recv_exact(s, 2)
        if resp != b"\x05\x00":
            raise OSError("socks5 proxy requires auth or refused")
        host_b = dst_host.encode("idna") if dst_host.isascii() else dst_host.encode()
        req = (b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
               + struct.pack(">H", dst_port))
        s.sendall(req)
        head = self._recv_exact(s, 4)
        if head[1] != 0:
            raise OSError(f"socks5 connect failed code={head[1]}")
        atyp = head[3]
        if atyp == 1:
            self._recv_exact(s, 4)
        elif atyp == 3:
            n = self._recv_exact(s, 1)[0]
            self._recv_exact(s, n)
        elif atyp == 4:
            self._recv_exact(s, 16)
        self._recv_exact(s, 2)
        return s

    @staticmethod
    def _recv_exact(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise OSError("egress closed unexpectedly")
            buf += chunk
        return buf

    def https_connection(self, host, timeout):
        raw = self.open_socket(host, 443, timeout)
        parent = self

        class Conn(http.client.HTTPSConnection):
            def connect(self):
                ctx = ssl.create_default_context()
                # 严格绑定已建立的隧道，连接断开时绝不回落直连
                if raw.fileno() < 0:
                    raise OSError("egress tunnel already closed")
                self.sock = ctx.wrap_socket(raw, server_hostname=host)

        conn = Conn(host, timeout=timeout)
        conn._egress = parent
        return conn

    def status(self):
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "addr": f"{self.host}:{self.port}" if self.kind != "direct" else "直连",
            "last_ok": int(self.last_ok), "last_error": self.last_error,
            "uses": self.uses, "quality": self.quality,
            "quality_answer": self.quality_answer,
            "quality_history": list(self.quality_history),
            "attribution": self.attribution,
        }


def detect_egresses():
    found = []

    def add(kind, host, port, label):
        eid = f"{kind}-{host}-{port}"
        if not any(e.id == eid for e in found):
            found.append(Egress(eid, label, kind, host, port))

    # 系统代理（注册表）
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        if winreg.QueryValueEx(k, "ProxyEnable")[0] == 1:
            srv = winreg.QueryValueEx(k, "ProxyServer")[0]
            for part in srv.split(";"):
                if "=" in part:
                    key, addr = part.split("=", 1)
                    if key.lower() not in ("http", "https"):
                        continue
                else:
                    addr = part
                if ":" in addr:
                    h, p = addr.rsplit(":", 1)
                    add("http", h, int(p), f"系统代理 {addr}")
        winreg.CloseKey(k)
    except Exception:
        pass

    # 环境变量
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(var)
        if not v:
            continue
        u = urllib.parse.urlparse(v if "//" in v else "//" + v)
        if u.hostname:
            kind = "socks5" if "socks" in (u.scheme or "") else "http"
            add(kind, u.hostname, u.port or (1080 if kind == "socks5" else 8080),
                f"环境变量 {var}")

    # 常见本地端口（mixed 端口按 HTTP CONNECT 处理，经典 SOCKS 端口按 socks5）
    http_ports = (7897, 7890, 7891, 2080, 8118, 8888, 33210)
    socks_ports = (1080, 10808, 10809)
    for p in (*http_ports, *socks_ports):
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.3):
                pass
            kind = "socks5" if p in socks_ports else "http"
            add(kind, "127.0.0.1", p, f"本地代理 :{p} ({'SOCKS5' if kind == 'socks5' else 'HTTP'})")
        except OSError:
            continue

    found.append(Egress("direct", "直连（不经代理）", "direct", "", 0))
    return found


# ---------------------------------------------------------------- SSE 解析 --
def sse_walk(data: bytes):
    """产出 (event_name, payload_bytes)。"""
    for block in re.split(rb"\r?\n\r?\n", data):
        name, payload_lines = "", []
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                name = line[6:].decode("utf8", "replace").strip()
            elif line.startswith(b"data:"):
                payload_lines.append(line[5:].strip())
        if payload_lines:
            yield name, b"\n".join(payload_lines)


def stream_outcome(data: bytes):
    """返回 (completed, failure_kind, failure_status)。对应 engine.go 的分类器。"""
    completed = False
    failure, status = "", 0
    for _name, payload in sse_walk(data):
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        kind = ev.get("type") or _name
        if kind == "response.completed":
            completed = True
        elif kind in ("response.failed", "response.incomplete", "error"):
            code = (ev.get("response", {}).get("error", {}).get("code")
                    or ev.get("error", {}).get("code") or ev.get("code") or "")
            cur, st = "response_failed", 0
            if code in ("server_is_overloaded", "slow_down"):
                cur = "model_capacity"
            elif code in ("rate_limit_exceeded", "insufficient_quota"):
                cur, st = "upstream_rate_limited", 429
            if not failure or st:
                failure, status = cur, st
    return completed, failure, status


def is_compact_trigger(body: bytes) -> bool:
    """远程压缩 v2：input 最后一项是单独的 compaction_trigger。"""
    try:
        req = json.loads(body)
        inp = req.get("input")
        if not isinstance(inp, list) or not inp:
            return False
        last = inp[-1]
        return isinstance(last, dict) and len(last) == 1 and last.get("type") == "compaction_trigger"
    except Exception:
        return False


class StreamObserver:
    """旁路解析 SSE 流：记录服务端自报的真实模型 ID、token 用量。
    只读不改流内容，用于对比注入开/关是否有可观测差异。"""

    def __init__(self):
        self.buf = b""
        self.model = ""
        self.usage = {}
        self.completed = False
        self.ttft_ms = None

    def feed(self, chunk: bytes):
        if self.ttft_ms is None:
            self.ttft_ms = int(time.time() * 1000)
        self.buf += chunk
        while True:
            m = re.search(rb"\r?\n\r?\n", self.buf)
            if not m:
                break
            block, self.buf = self.buf[:m.start()], self.buf[m.end():]
            self._parse(block)
        if len(self.buf) > 65536:  # 异常大的半截块，丢弃防止内存增长
            self.buf = b""

    def _parse(self, block: bytes):
        payload_lines = [l[5:].strip() for l in block.split(b"\n") if l.startswith(b"data:")]
        if not payload_lines:
            return
        try:
            ev = json.loads(b"\n".join(payload_lines))
        except Exception:
            return
        kind = ev.get("type", "")
        if kind == "response.created":
            self.model = (ev.get("response") or {}).get("model", "")
        elif kind == "response.completed":
            self.completed = True
            resp = ev.get("response") or {}
            if not self.model:
                self.model = resp.get("model", "")
            u = resp.get("usage") or {}
            self.usage = {
                "in": u.get("input_tokens", 0),
                "out": u.get("output_tokens", 0),
                "reason": ((u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)),
            }


# 知识新鲜度探针（社区共识做法，来自 tzf1003/csss：答 iPhone 17 = 满血，
# iPhone 16 = 降智，iPhone 15 = 严重降智。规则会随时间过时，随社区更新。）
# 单题噪声大（幻觉/表述敏感），所以：①同一知识准备两种表述，保活轮换、体检复测；
# ②判定进滚动窗口多数表决（见 Egress.record_quality），单次异常不定性。
QUALITY_PROBES = [
    "What is the latest iPhone model? Reply with just the model name.",
    "If I walked into a store today to buy the newest iPhone, which model would I get? Name only the model.",
]
QUALITY_PROBE_SYSTEM = ("Answer from your own knowledge only, in a few words, no explanation. "
                        "Do not use any tools or web search.")
QUALITY_LABELS = {"healthy": "满血", "degraded": "降智", "severely": "严重降智", "unknown": "未知"}


def pick_probe(rng=None):
    return (rng or __import__("random")).choice(QUALITY_PROBES)


def sse_answer_text(data: bytes) -> str:
    """拼接 SSE 流里的模型回答文本（delta 优先，completed 全文兜底）。"""
    parts, full = [], ""
    for _name, payload in sse_walk(data):
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        kind = ev.get("type", "")
        if kind == "response.output_text.delta":
            parts.append(ev.get("delta", ""))
        elif kind == "response.completed":
            for item in (ev.get("response") or {}).get("output", []):
                for c in (item or {}).get("content", []):
                    if (c or {}).get("type") in ("output_text", "text"):
                        full += c.get("text", "")
    return ("".join(parts) or full).strip()


def judge_quality(answer: str) -> str:
    # 全角数字归一化（csss 细节：中文模型可能输出 １７）
    t = (answer or "")
    t = "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in t)
    t = t.lower().replace(" ", "").replace("-", "").replace("‑", "").replace("–", "").replace("—", "")
    if "iphone17" in t or ("17" in t and "iphone" in t):
        return "healthy"
    if "iphone16" in t or ("16" in t and "iphone" in t):
        return "degraded"
    if "iphone15" in t or ("15" in t and "iphone" in t):
        return "severely"
    return "unknown"



# ---------------------------------------------------------------- 认证与账号 --
def load_codex_auth():
    try:
        d = json.loads(auth_path().read_text(encoding="utf8"))
    except Exception:
        return None
    tokens = d.get("tokens") or {}
    access = tokens.get("access_token") or d.get("OPENAI_API_KEY") or ""
    account = tokens.get("account_id") or ""
    if not access:
        return None
    headers = {
        "Authorization": f"Bearer {access}",
        "User-Agent": "codex_cli_rs/0.153.0 (Windows 11; x86_64)",
        "Originator": "codex_cli_rs",
    }
    if account:
        headers["chatgpt-account-id"] = account
    return headers


def plan_from_token(auth_header: str, account_header: str = ""):
    """从 JWT 的套餐提示推断 personal/team；识别不了返回 personal。"""
    token = auth_header.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return "personal"
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get("https://api.openai.com/auth", {})
        plan = (auth.get("chatgpt_plan_type") or "").lower()
        default_account = auth.get("chatgpt_account_id") or ""
        if account_header and default_account and account_header != default_account:
            return "personal"  # 选中的工作区与令牌默认不一致，不做 Team 判定
        if plan in ("team", "business"):
            return "team"
    except Exception:
        pass
    return "personal"


# ---------------------------------------------------------------- 引擎 --
class Session:
    def __init__(self, key, model):
        self.key = key
        self.model = model
        self.lock = threading.Lock()
        self.headers = {}
        self.plan = "personal"
        self.store = StateStore(set(PLAN_SHAPES["personal"]))
        self.limit_status = 0          # 0 / 401 / 403 / 429
        self.limit_until = 0.0
        self.blocked_auth = ""
        self.next_probe = 0.0
        self.probing = False
        self.last_probe_result = "尚未采集"
        self.observed_blocks = 0
        self.egress_cursor = 0
        self.state_egress = "direct"
        self.last_active = time.time()
        self.fail_rounds = 0           # 连续未采到合格 state 的轮数（指数退避）
        self.state_healthy = True      # 当前 state 是否采集自满血出口
        self.next_keepalive = 0.0      # 保活下一次触发时间
        self.degraded_rounds = 0       # 连续综合降智的轮数（升级自愈用）
        self.obs_reason_ema = None     # 注入请求 reasoning tokens 指数均值基线
        self.obs_samples = 0
        self.last_autocheck = 0.0      # 观测触发自动体检的节流


KEEPALIVE_DEFAULT = 600       # 保活间隔（秒）
PROBE_DAILY_LIMIT = 200       # 每日探针请求上限（防失控烧额度）
DEGRADED_RETRY = 240          # 出口降智后的自愈重试间隔（对齐社区观察的 ~240s 凭据窗口）


class Engine:
    def __init__(self, settings, persist):
        self.settings = settings            # dict: injection_enabled/fallback/account_mode/model
        self.persist = persist              # StatePersist
        self.egresses = detect_egresses()
        self.sessions = {}
        self.lock = threading.Lock()
        self.requests_total = 0
        self.probes_total = 0
        self.probe_date = time.strftime("%Y%m%d")
        self.probes_today = 0
        self.last_seen_tz = ""        # 最近在请求体 environment_context 观测到的时区
        self.tz_overrides = 0         # 已改写的请求数
        self.attribution_state = {"running": False, "started": 0, "finished": 0,
                                  "result": None, "error": ""}
        self.heal_events = collections.deque(maxlen=50)   # detected/recovered 事件流
        self.started_at = time.time()
        self.stop = threading.Event()
        self.observations = collections.deque(maxlen=300)
        self._load_observations()
        self._load_sessions()

    # ---- 效果观测（旁路、落盘） ----
    def _obs_path(self):
        return data_dir() / "observations.jsonl"

    def _load_observations(self):
        try:
            lines = self._obs_path().read_text(encoding="utf8").splitlines()[-300:]
            for line in lines:
                try:
                    self.observations.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    def record_observation(self, inject: bool, obs: StreamObserver, status: int, model_hint: str, duration_ms: int):
        entry = {
            "ts": int(time.time()), "inject": bool(inject), "status": status,
            "model": obs.model or model_hint, "in": obs.usage.get("in", 0),
            "out": obs.usage.get("out", 0), "reason": obs.usage.get("reason", 0),
            "ttft_ms": obs.ttft_ms, "duration_ms": duration_ms,
        }
        self.observations.append(entry)
        try:
            with open(self._obs_path(), "a", encoding="utf8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def heal_summary(self):
        detected = recovered = 0
        last = None
        for e in self.egresses:
            for ev in getattr(e, "quality_events", []) or []:
                if ev["kind"] == "detected":
                    detected += 1
                elif ev["kind"] == "recovered":
                    recovered += 1
                last = ev
        return {"detected": detected, "recovered": recovered,
                "last": last, "note": "detected=降智检出次数，recovered=自动恢复次数"}

    def observations_summary(self):
        groups = {"on": [], "off": []}
        for o in self.observations:
            if o.get("status") == 200 and o.get("out"):
                groups["on" if o.get("inject") else "off"].append(o)

        def agg(items):
            if not items:
                return {"n": 0}
            models = collections.Counter(i["model"] for i in items if i.get("model"))
            ttfts = [i["ttft_ms"] for i in items if i.get("ttft_ms")]
            outs = [i["out"] for i in items]
            reasons = [i["reason"] for i in items if i.get("reason")]
            return {
                "n": len(items),
                "models": [{"model": m, "count": c} for m, c in models.most_common(4)],
                "avg_ttft_ms": int(sum(ttfts) / len(ttfts)) if ttfts else 0,
                "avg_out_tokens": int(sum(outs) / len(outs)),
                "avg_reason_tokens": int(sum(reasons) / len(reasons)) if reasons else 0,
            }

        return {"on": agg(groups["on"]), "off": agg(groups["off"]),
                "note": "对比注入开/关的自报模型与用量；注意社区发现 served 字段可能失真，token 用量更可靠；样本少时不足以下结论。"}

    # ---- 持久化 ----
    def _load_sessions(self):
        try:
            data = self.persist.read()
            for key, s in (data.get("sessions") or {}).items():
                model = s.get("model") or self.settings["model"]
                sess = Session(key, model)
                sess.plan = s.get("plan") or "personal"
                sess.store.blocks = set(PLAN_SHAPES.get(sess.plan, PLAN_SHAPES["personal"]))
                for slot in ("active", "backup0", "backup1", "backup2"):
                    v = s.get(slot)
                    if not v:
                        continue
                    st = parse_state(v)
                    if st:
                        if slot == "active":
                            sess.store.active = st
                        else:
                            sess.store.pool.append(st)
                sess.store.pool.sort(key=lambda x: x.issued, reverse=True)
                sess.store.pool = sess.store.pool[:StateStore.POOL_SIZE]
                sess.state_egress = s.get("state_egress") or "direct"
                self.sessions[key] = sess
        except Exception as e:
            log("persist_load_failed", level="warn", err=str(e))

    def save(self):
        sessions = {}
        for key, sess in self.sessions.items():
            entry = {
                "model": sess.model, "plan": sess.plan,
                "active": sess.store.active.value if sess.store.active else "",
                "state_egress": sess.state_egress,
            }
            for i, p in enumerate(sess.store.pool[:StateStore.POOL_SIZE]):
                entry[f"backup{i}"] = p.value
            sessions[key] = entry
        # 合并写入，保留 port/pid/panel_key/settings 等全局键
        data = self.persist.read()
        data["sessions"] = sessions
        data["saved_at"] = int(time.time())
        self.persist.write(data)

    # ---- 会话借用（凭据只留在内存） ----
    def account_mode_effective(self, sess: Session):
        mode = self.settings.get("account_mode") or "auto"
        return sess.plan if mode == "auto" else mode

    def blocks_for(self, sess: Session):
        return set(PLAN_SHAPES[self.account_mode_effective(sess)])

    def borrow(self, headers, model):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 16:
            return None, "bearer authentication required"
        account = headers.get("chatgpt-account-id", "")
        key = hashlib.sha256(f"{account}\x00{model}".encode()).hexdigest()[:16]
        with self.lock:
            sess = self.sessions.get(key)
            if sess is None:
                sess = Session(key, model)
                self.sessions[key] = sess
        with sess.lock:
            for h in ("Authorization", "chatgpt-account-id", "User-Agent", "Version",
                      "Originator", "OpenAI-Beta", "session_id"):
                v = headers.get(h)
                if v:
                    sess.headers[h] = v
            sess.plan = plan_from_token(auth, account)
            expected = self.blocks_for(sess)
            if sess.store.blocks != expected:
                sess.store.blocks = expected
            # 新令牌（codex 刷新过）自动解除旧的 401 封锁
            if sess.blocked_auth and sess.blocked_auth != auth:
                sess.blocked_auth = ""
                if sess.limit_status in (401, 403):
                    sess.limit_status, sess.limit_until = 0, 0
            sess.last_active = time.time()
        return sess, ""

    def limited(self, sess):
        with sess.lock:
            if sess.limit_status in (401, 403):
                return sess.limit_status, 0
            if sess.limit_status == 429 and time.time() < sess.limit_until:
                return 429, int(sess.limit_until - time.time()) + 1
            if sess.limit_status == 429:
                sess.limit_status = 0
            return 0, 0

    def mark_limit(self, sess, status, retry_after=0):
        with sess.lock:
            if status in (401, 403):
                sess.limit_status = status
                sess.blocked_auth = sess.headers.get("Authorization", "")
            elif status == 429:
                sess.limit_status = 429
                delay = max(retry_after, COOLDOWN_SECONDS)
                sess.limit_until = max(sess.limit_until, time.time() + delay)

    # ---- 探测 ----
    def _generate_once(self, sess: Session, egress: Egress, prompt: str,
                       system: str = "", timeout: int = PROBE_TIMEOUT):
        """通用单次生成：发 prompt 读全文。返回文本/状态头/限流信息。"""
        headers = dict(sess.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        body = json.dumps({
            "model": sess.model,
            "instructions": system or QUALITY_PROBE_SYSTEM,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": prompt}]}],
            "stream": True, "store": False, "parallel_tool_calls": True,
            "include": ["reasoning.encrypted_content"],
        }).encode()
        conn = egress.https_connection(UPSTREAM_HOST, timeout)
        try:
            conn.request("POST", UPSTREAM_BASE + "/responses", body=body, headers=headers)
            resp = conn.getresponse()
            state_raw = resp.headers.get(STATE_HEADER) or resp.headers.get(STATE_HEADER.lower()) or ""
            if resp.status != 200:
                retry = resp.headers.get("Retry-After") or ""
                try:
                    retry = int(retry)
                except ValueError:
                    retry = 0
                return {"ok": False, "status": resp.status, "retry_after": retry}
            data = resp.read(2 << 21)
            completed, failure, fstatus = stream_outcome(data)
            if failure:
                return {"ok": False, "status": fstatus or resp.status, "retry_after": 0}
            if not completed:
                return {"ok": False, "status": resp.status, "retry_after": 0}
            return {"ok": True, "status": 200, "state": state_raw,
                    "text": sse_answer_text(data)}
        except OSError as e:
            return {"ok": False, "status": 0, "retry_after": 0, "error": str(e)}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def probe_once(self, sess: Session, egress: Egress, prompt: str = ""):
        """发一条极短的知识探针请求：一次请求同时采集 turn-state 并判定出口
        serving 质量（iPhone 知识新鲜度法，多表述题库随机轮换）。消耗真实额度。"""
        headers = dict(sess.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        body = json.dumps({
            "model": sess.model,
            "instructions": QUALITY_PROBE_SYSTEM,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": prompt or pick_probe()}]}],
            "stream": True, "store": False, "parallel_tool_calls": True,
            "include": ["reasoning.encrypted_content"],
        }).encode()
        conn = egress.https_connection(UPSTREAM_HOST, PROBE_TIMEOUT)
        try:
            conn.request("POST", UPSTREAM_BASE + "/responses", body=body, headers=headers)
            resp = conn.getresponse()
            state_raw = resp.headers.get(STATE_HEADER) or resp.headers.get(STATE_HEADER.lower())
            if resp.status != 200:
                retry = resp.headers.get("Retry-After") or ""
                try:
                    retry = int(retry)
                except ValueError:
                    retry = 0
                return {"ok": False, "status": resp.status, "retry_after": retry,
                        "result": "upstream_rejected"}
            data = resp.read(1 << 21)
            st = parse_state(state_raw or "")
            completed, failure, fstatus = stream_outcome(data)
            answer = sse_answer_text(data)
            if failure:
                return {"ok": False, "status": fstatus or resp.status, "result": failure,
                        "retry_after": 0}
            if not completed:
                return {"ok": False, "status": resp.status, "result": "incomplete_response"}
            if not state_raw:
                return {"ok": False, "status": resp.status, "result": "missing_state_header"}
            if st is None:
                return {"ok": False, "status": resp.status, "result": "invalid_state_envelope"}
            single = judge_quality(answer)
            combined = egress.record_quality(single)
            egress.quality_answer = f"单次{QUALITY_LABELS.get(single, single)}·{answer[:48]}"
            return {"ok": True, "status": 200, "state": st,
                    "shape_ok": st.blocks in self.blocks_for(sess), "blocks": st.blocks,
                    "quality": single, "combined_quality": combined}
        except OSError as e:
            return {"ok": False, "status": 0, "result": "network_failed", "error": str(e)}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def deep_attribution(self, sess: Session, egress: Egress):
        """ModelTrace 深度归因：3 条长整数挑战 → 数字指纹归因 8 个候选模型。
        比 iPhone 知识探针更硬的证据；成本为 3 次完整生成。"""
        if _mt is None:
            return {"ok": False, "error": "指纹库或 modeltrace 模块缺失"}
        challenges = _mt.generate_challenges(3)
        outputs = []
        for ch in challenges:
            r = self._generate_once(sess, egress, ch["prompt"], timeout=180)
            if not r.get("ok"):
                return {"ok": False, "status": r.get("status", 0), "retry_after": r.get("retry_after", 0),
                        "error": f"挑战 {ch['id']} 未完成（status={r.get('status', 0)}）"}
            outputs.append({"text": r.get("text", ""), "expected_count": ch["expected_count"]})
        try:
            res = _mt.analyze_outputs(outputs, _mt.load_bank())
        except ValueError as e:
            return {"ok": False, "error": str(e)[:120]}
        egress.attribution = {
            "prediction": res["prediction"], "probability": round(res["probability"], 3),
            "top": [{"model": r["model"], "p": round(r["probability"], 3)} for r in res["results"][:3]],
            "at": int(time.time()),
        }
        # 归因结论联动质量判定与自愈（终审：覆盖滚动表决历史）
        if res["prediction"] == sess.model:
            egress.record_quality("healthy", src="attribution")
            egress.quality_answer = f"归因 {res['prediction']} {res['probability']:.0%}"
            log("attribution_finished", egress=egress.id, prediction=res["prediction"],
                probability=round(res["probability"], 3), verdict="match")
        else:
            egress.record_quality("degraded", src="attribution")
            egress.quality_answer = f"归因 {res['prediction']} {res['probability']:.0%}"
            if sess.state_egress == egress.id:
                sess.store.clear()
                sess.state_healthy = False
            sess.last_probe_result = "attribution_mismatch"
            sess.next_probe = min(sess.next_probe, time.time() + DEGRADED_RETRY)
            log("attribution_finished", egress=egress.id, prediction=res["prediction"],
                expected=sess.model, probability=round(res["probability"], 3),
                verdict="mismatch", level="warn",
                detail="归因与请求模型不符，已清空 state 池并进入自愈")
        self.save()
        return {"ok": True, **egress.attribution}

    def refresh(self, sess: Session, manual=False):
        """一轮采集：按出口轮换尝试，直到拿到合格 state 或触发上游限制。"""
        if not self.settings.get("injection_enabled", True) and not manual:
            return
        with sess.lock:
            if sess.probing:
                return
            sess.probing = True
        try:
            if self.limited(sess)[0] and not manual:
                return
            limit = min(MAX_PROBES_PER_ROUND, len(self.egresses))
            attempts = 0
            accepted = False
            # 出口绑定策略：常规采集固定用 state 绑定出口（链路一致，避免出口
            # 跳变）；仅当连续两轮综合降智（升级路径）才轮换到下一个出口
            bound = self.egress_by_id(sess.state_egress)
            preferred = bound if (bound and sess.degraded_rounds < 2) else None
            for _ in range(len(self.egresses)):
                if attempts >= limit or self.stop.is_set():
                    break
                if preferred is not None:
                    egress = preferred
                else:
                    idx = sess.egress_cursor % len(self.egresses)
                    sess.egress_cursor += 1
                    egress = self.egresses[idx]
                if self._probe_budget_exceeded():
                    log("probe_skipped", level="warn", detail="已达每日探针上限，防额度失控")
                    return
                self.probes_total += 1
                self.probes_today += 1
                attempts += 1
                r = self.probe_once(sess, egress)
                if r.get("ok"):
                    egress.last_ok = time.time()
                    egress.uses += 1
                    egress.last_error = ""
                    combined = r.get("combined_quality", egress.quality)
                    if r["shape_ok"] and r.get("quality") == "healthy" and \
                            combined not in ("degraded", "severely"):
                        sess.degraded_rounds = 0
                        sess.store.offer(r["state"])
                        sess.state_egress = egress.id
                        sess.state_healthy = True
                        sess.last_probe_result = "accepted"
                        sess.observed_blocks = r["blocks"]
                        accepted = True
                        log("probe_finished", egress=egress.id, result="accepted",
                            quality=r.get("quality"), combined=combined,
                            blocks=r["blocks"], expected=sorted(self.blocks_for(sess)))
                        self.save()
                        return
                    if r["shape_ok"] and r.get("quality") == "unknown" and \
                            combined not in ("degraded", "severely"):
                        sess.store.offer(r["state"])
                        sess.state_egress = egress.id
                        sess.last_probe_result = "accepted(质量未知)"
                        accepted = True
                        log("probe_finished", egress=egress.id, result="accepted",
                            quality="unknown", combined=combined,
                            blocks=r["blocks"])
                        self.save()
                        return
                    if r["shape_ok"] and combined in ("degraded", "severely"):
                        # 综合判定（多数票）降智：仅隔离"本出口"采集的 state——
                        # 其他出口的满血 state 与本出口的降智无关，不清、不跳出口
                        if sess.state_egress == egress.id:
                            sess.store.clear()
                            sess.state_healthy = False
                        else:
                            log("probe_finished", egress=egress.id,
                                result="other_egress_degraded",
                                detail="该出口降智但不影响绑定出口的 state，跳过")
                        sess.last_probe_result = "quality_" + combined
                        if sess.state_egress == egress.id or not sess.store.active:
                            sess.degraded_rounds += 1
                            if sess.degraded_rounds >= 2 and len(self.egresses) > 1:
                                sess.egress_cursor += 1   # 升级：下一轮换出口
                                log("probe_finished", egress=egress.id,
                                    result="degraded_escalated",
                                    rounds=sess.degraded_rounds,
                                    detail="同出口连续降智，下一轮自动尝试备用出口")
                                return
                        log("probe_finished", egress=egress.id,
                            result="degraded_state_discarded",
                            quality=r.get("quality"), combined=combined,
                            answer=egress.quality_answer,
                            detail="多数表决为降智，同出口自愈重试")
                        return
                    sess.observed_blocks = r["blocks"]
                    sess.last_probe_result = "shape_mismatch"
                    log("probe_finished", egress=egress.id, result="shape_mismatch",
                        blocks=r["blocks"], expected=sorted(self.blocks_for(sess)))
                else:
                    if r.get("error"):
                        egress.last_error = r["error"][:120]
                    if r.get("status") in (401, 403, 429):
                        self.mark_limit(sess, r["status"], r.get("retry_after", 0))
                        sess.last_probe_result = r["result"]
                        log("probe_finished", egress=egress.id, result=r["result"],
                            status=r["status"], level="warn")
                        return
                    sess.last_probe_result = r["result"]
                    log("probe_finished", egress=egress.id, result=r["result"],
                        status=r.get("status", 0), level="warn")
        finally:
            with sess.lock:
                sess.probing = False
                if accepted:
                    sess.fail_rounds = 0
                    sess.next_probe = time.time() + COOLDOWN_SECONDS
                elif not sess.state_healthy and sess.store.active is None:
                    # 降智自愈：短退避等上游裁决窗口滑动，不用指数退避
                    sess.fail_rounds = 0
                    sess.next_probe = time.time() + DEGRADED_RETRY
                else:
                    # 连续采不到合格 state 时指数退避，避免烧额度：3min→10min→30min→60min 封顶
                    delay = min(COOLDOWN_SECONDS * (2 ** min(sess.fail_rounds, 4)), 3600)
                    sess.fail_rounds += 1
                    sess.next_probe = time.time() + delay

    def _probe_budget_exceeded(self) -> bool:
        today = time.strftime("%Y%m%d")
        if self.probe_date != today:
            self.probe_date = today
            self.probes_today = 0
        return self.probes_today >= PROBE_DAILY_LIMIT

    def autocheck(self, sess: Session):
        """观测异常后的确认体检：对 state 绑定出口连跑两题记票。消耗真实额度。"""
        egress = self.egress_by_id(sess.state_egress) or self.default_egress()
        for q in QUALITY_PROBES:
            r = self.probe_once(sess, egress, prompt=q)
            log("autocheck", egress=egress.id, ok=r.get("ok"),
                quality=egress.quality, answer=egress.quality_answer)
            if not r.get("ok") and r.get("status") in (401, 403, 429):
                return
        if egress.quality in ("degraded", "severely") and sess.state_egress == egress.id:
            sess.store.clear()
            sess.state_healthy = False
            log("autocheck_isolated", egress=egress.id, detail="确认降智（绑定出口），已清池自愈")

    def bootstrap_from_auth(self):
        """增强点：直接从 auth.json 建立会话并采集，无需先在 Codex 发消息。"""
        headers = load_codex_auth()
        if not headers:
            log("bootstrap_no_auth", level="warn",
                hint="未读取到 ~/.codex/auth.json，等待第一条 Codex 请求")
            return
        sess, err = self.borrow(headers, self.settings.get("model") or DEFAULT_MODEL)
        if not sess:
            log("bootstrap_failed", level="warn", err=err)
            return
        log("bootstrap_session", plan=sess.plan, model=sess.model,
            blocks=sorted(self.blocks_for(sess)))
        threading.Thread(target=self.refresh, args=(sess,), daemon=True).start()

    def probe_target_session(self):
        """手动采集的目标会话：最近活跃的会话，否则用 auth.json 自举。"""
        active = [s for s in self.sessions.values() if s.headers.get("Authorization")]
        if active:
            active.sort(key=lambda s: -s.last_active)
            return active[0]
        headers = load_codex_auth()
        if headers:
            sess, _ = self.borrow(headers, self.settings.get("model") or DEFAULT_MODEL)
            return sess
        return None

    def default_egress(self):
        for e in self.egresses:
            if e.kind != "direct" and not e.last_error:
                return e
        for e in self.egresses:
            if e.kind != "direct":
                return e
        return self.egresses[-1]

    def egress_by_id(self, eid):
        for e in self.egresses:
            if e.id == eid:
                return e
        return None

    def refresher_loop(self):
        """保活循环：临期刷新 + 定期保活（维持满血 state 链路）+ 降智自愈重试。"""
        while not self.stop.wait(10):
            try:
                if not self.settings.get("injection_enabled", True):
                    continue
                now = time.time()
                keepalive_on = self.settings.get("keepalive_enabled", True)
                keepalive_iv = int(self.settings.get("keepalive_interval") or KEEPALIVE_DEFAULT)
                for sess in list(self.sessions.values()):
                    if now - sess.last_active > 1800:
                        continue
                    if now < sess.next_probe or sess.probing or self.limited(sess)[0]:
                        continue
                    if not sess.headers.get("Authorization"):
                        continue
                    need = sess.store.needs_refresh(now) or not sess.state_healthy
                    if keepalive_on and now >= sess.next_keepalive:
                        need = True
                    # 刚从降智恢复：立即补采满血 state，不受探测冷却限制
                    recovered_recently = any(
                        now - getattr(e, "recovered_at", 0) < 30 for e in self.egresses)
                    if recovered_recently and sess.store.active is None:
                        sess.next_probe = 0
                        need = True
                    if not need:
                        continue
                    self.refresh(sess)
                    sess.next_keepalive = now + keepalive_iv
            except Exception as e:
                log("refresher_error", level="error", err=str(e))

    def status(self):
        now = time.time()
        sessions = []
        for key, sess in self.sessions.items():
            lim, wait = self.limited(sess)
            usable = state_accepts(sess.store.active, sess.store.blocks, now)
            phase = ("auth_blocked" if lim in (401, 403)
                     else "rate_limited" if lim == 429
                     else "ready" if usable
                     else "collecting" if sess.probing else "waiting")
            sessions.append({
                "key": key[:8], "model": sess.model,
                "state_healthy": sess.state_healthy,
                "plan": self.account_mode_effective(sess),
                "token_plan": sess.plan,
                "phase": phase, "state": sess.store.snapshot(),
                "limit_status": lim, "limit_wait": wait,
                "last_probe_result": sess.last_probe_result,
                "observed_blocks": sess.observed_blocks,
                "state_egress": sess.state_egress,
                "next_probe_in": max(0, int(sess.next_probe - now)) if sess.next_probe else 0,
            })
        return {
            "app": APP, "version": "1.0.0",
            "uptime": int(now - self.started_at),
            "injection_enabled": bool(self.settings.get("injection_enabled", True)),
            "fallback": self.settings.get("fallback", "passthrough"),
            "account_mode": self.settings.get("account_mode", "auto"),
            "model": self.settings.get("model", DEFAULT_MODEL),
            "requests_total": self.requests_total,
            "probes_total": self.probes_total,
            "egresses": [e.status() for e in self.egresses],
            "sessions": sessions,
            "codex_linked": config_linked(),
            "listen": f"{LISTEN_HOST}:{LISTEN_PORT}",
            "log_tail": list(_LOG)[-140:],
            "ttl": TTL_SECONDS,
            "observations": self.observations_summary(),
            "keepalive": {
                "enabled": bool(self.settings.get("keepalive_enabled", True)),
                "interval": int(self.settings.get("keepalive_interval") or KEEPALIVE_DEFAULT),
                "probes_today": self.probes_today,
                "daily_limit": PROBE_DAILY_LIMIT,
            },
            "attribution": self.attribution_state,
            "heal": self.heal_summary(),
            "timezone": {
                "mode": self.settings.get("tz_mode", "off"),
                "value": self.settings.get("tz_value", ""),
                "last_seen": self.last_seen_tz,
                "overrides": self.tz_overrides,
            },
        }


def _strip_managed_block(text: str) -> str:
    """删除 managed provider 块与标记注释；按表头定位，不依赖标记注释存在。"""
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == MANAGED_MARK or stripped == "# restore: python sleep_plus.py restore":
            i += 1
            continue
        if re.match(r"\s*\[model_providers\." + re.escape(PROVIDER_ID) + r"\]", lines[i]):
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if lines[i].lstrip().startswith("["):
                    break
                if s in (MANAGED_MARK, "# restore: python sleep_plus.py restore") \
                        or ("managed by ccodex-sleep-plus" in s and s.startswith("#")):
                    i += 1
                    continue
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out).rstrip("\r\n") + "\n"


# ---------------------------------------------------------------- 配置接管 --
def _top_level_insert(text: str, line: str):
    """把顶层键插入到第一个 table 头之前；若已存在则替换。返回 (new_text, old_line)。"""
    mkey = re.match(r"([A-Za-z0-9_\-]+)\s*=", line)
    key = mkey.group(1) if mkey else ""
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    old = None
    for i in range(first_table):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            old = lines[i].rstrip("\r\n")
            lines[i] = line + "\n"
            return "".join(lines), old
    lines.insert(first_table, line + "\n")
    return "".join(lines), old


def install_provider(port=LISTEN_PORT, model=None):
    """给 config.toml 加一个指向本地网关的 provider（幂等，先备份）。"""
    path = config_path()
    if not path.exists():
        return {"ok": False, "error": f"config not found: {path}"}
    text = path.read_text(encoding="utf8")
    if tomllib is not None:
        try:
            tomllib.loads(text)
        except Exception as e:
            return {"ok": False, "error": f"config.toml 不是有效 TOML，未改动: {e}"}

    changed = False
    managed = None
    if tomllib is not None:
        doc = tomllib.loads(text)
        managed = (doc.get("model_providers") or {}).get(PROVIDER_ID)

    # 备份定位（全防御：任何异常都降级为新建备份，绝不因备份问题中断接管）
    backup = None
    try:
        existing = sorted(path.parent.glob("config.toml.bak-before-sleep-plus-*"))
        if managed is None and not existing:
            backup = path.with_name(
                f"config.toml.bak-before-sleep-plus-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
        elif existing:
            backup = existing[-1]
    except Exception as e:
        log("backup_locate_failed", level="warn", err=str(e)[:120])
        backup = None
    if managed is None and backup is None:
        # 仍无备份（如备份目录不可写）——强制新建一次，失败则放弃接管而不是崩溃
        try:
            backup = path.with_name(
                f"config.toml.bak-before-sleep-plus-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
        except Exception as e:
            return {"ok": False, "error": f"无法创建配置备份，已放弃接管: {e}"}

    base_url = f"http://{LISTEN_HOST}:{port}/backend-api/codex"
    if managed is not None and managed.get("base_url") == base_url and \
            tomllib.loads(text).get("model_provider") == PROVIDER_ID:
        return {"ok": True, "already": True, "backup": str(backup) if backup else ""}

    # 顶层 model_provider
    text, old_line = _top_level_insert(text, f'model_provider = "{PROVIDER_ID}"')
    changed = True
    # 顶层 model（缺失或不受支持时设为默认）
    try:
        doc = tomllib.loads(text)
        top_model = None
        for m in re.finditer(r"^model\s*=\s*\"([^\"]+)\"", text, re.M):
            top_model = m.group(1)
            break
        if not top_model or top_model not in SUPPORTED_MODELS:
            text, _ = _top_level_insert(text, f'model = "{model or DEFAULT_MODEL}"')
    except Exception:
        pass

    # 移除旧的 managed 块再追加新的（幂等；不依赖标记注释）
    text = _strip_managed_block(text)
    block = (
        f"\n{MANAGED_MARK}\n"
        f"# restore: python sleep_plus.py restore\n"
        f"[model_providers.{PROVIDER_ID}]\n"
        f'name = "OpenAI"\n'
        f'base_url = "{base_url}"\n'
        f'wire_api = "responses"\n'
        f"requires_openai_auth = true\n"
        f"supports_websockets = false\n"
        f"request_max_retries = 0\n"
        f"stream_max_retries = 0\n"
    )
    text = text.rstrip("\r\n") + "\n" + block
    if tomllib is not None:
        try:
            doc = tomllib.loads(text)
            assert doc["model_provider"] == PROVIDER_ID
            assert (doc.get("model_providers") or {}).get(PROVIDER_ID, {}).get("base_url") == base_url
        except Exception as e:
            return {"ok": False, "error": f"补丁后校验失败，已放弃写入: {e}"}
    tmp = path.with_suffix(".toml.tmp-sleep-plus")
    tmp.write_text(text, encoding="utf8")
    os.replace(tmp, path)
    log("config_patched", provider=PROVIDER_ID, base_url=base_url,
        backup=str(backup) if backup else "", old_provider=old_line or "(none)")
    return {"ok": True, "backup": str(backup) if backup else "", "old_provider": old_line or ""}


def restore_provider():
    path = config_path()
    if not path.exists():
        return {"ok": False, "error": "config.toml 不存在"}
    text = path.read_text(encoding="utf8")
    changed = False
    if MANAGED_MARK in text:
        text = _strip_managed_block(text)
        changed = True
    # 移除顶层 model_provider = "sleep-plus"
    lines = text.splitlines(keepends=True)
    kept = [l for l in lines
            if not re.match(rf'\s*model_provider\s*=\s*"{PROVIDER_ID}"', l)]
    if len(kept) != len(lines):
        text = "".join(kept)
        changed = True
    if not changed:
        return {"ok": True, "already": True}
    if tomllib is not None:
        try:
            tomllib.loads(text)
        except Exception as e:
            return {"ok": False, "error": f"恢复后 TOML 校验失败，未写入: {e}"}
    tmp = path.with_suffix(".toml.tmp-sleep-plus")
    tmp.write_text(text, encoding="utf8")
    os.replace(tmp, path)
    log("config_restored", path=str(path))
    return {"ok": True}


def config_linked():
    path = config_path()
    if not path.exists() or tomllib is None:
        return False
    try:
        doc = tomllib.loads(path.read_text(encoding="utf8"))
        prov = (doc.get("model_providers") or {}).get(PROVIDER_ID) or {}
        return (doc.get("model_provider") == PROVIDER_ID
                and prov.get("base_url", "").startswith(f"http://{LISTEN_HOST}:{LISTEN_PORT}"))
    except Exception:
        return False


def watch_config_loop(engine: Engine):
    """桌面版 Codex 可能重写 config；30 秒一次自动补回 managed 块。"""
    while not engine.stop.wait(30):
        if not config_linked():
            r = install_provider(port=engine_port())
            if r.get("ok"):
                log("config_repaired", detail="检测到 provider 丢失，已自动补回")


def engine_port():
    return LISTEN_PORT


# ---------------------------------------------------------------- 持久化 ------
class Persist:
    def __init__(self):
        self.path = data_dir() / "state.json"

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf8"))
        except Exception:
            return {}

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")


# ---------------------------------------------------------------- HTTP 服务 --
PANEL_KEY = ""


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    engine: Engine = None
    timeout = 900

    def log_message(self, fmt, *args):
        pass  # 自有日志

    # ---- 工具 ----
    def _json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status, code, message, extra=None):
        self._json({"error": {"type": "sleep_plus_error", "code": code, "message": message}},
                   status=status, headers=extra)

    def _key_ok(self):
        """面板本机访问控制：回环 Host + Sec-Fetch-Site 白名单；key 可选
        （带了就必须匹配——兼容旧链接；不带也放行，从根上避免'链接过期'）。"""
        q = urllib.parse.urlparse(self.path).query
        key = urllib.parse.parse_qs(q).get("key", [""])[0]
        sec = self.headers.get("Sec-Fetch-Site") or ""
        if sec not in ("", "same-origin", "none"):
            log("panel_denied", level="warn", reason="sec_fetch_site", value=sec[:32])
            return False
        if key and key != PANEL_KEY:
            log("panel_denied", level="warn", reason="key_mismatch",
                key_prefix=key[:6], expect_prefix=PANEL_KEY[:6])
            return False
        return True

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/panel":
            if not self._key_ok():
                self._fail(403, "bad_key", "面板访问受限：请从托盘图标右键菜单重新打开面板")
                return
            body = _panel_html().encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/panel/api/status":
            if not self._key_ok():
                self._fail(403, "bad_key", "面板访问受限：请从托盘图标右键菜单重新打开面板")
                return
            self._json(self.engine.status())
            return
        if path.startswith(UPSTREAM_BASE):
            self._proxy()
            return
        self._fail(404, "unsupported_endpoint", "本服务只暴露 Codex responses 与本地面板。")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/panel/api/action":
            if not self._key_ok():
                self._fail(403, "bad_key", "面板访问受限：请从托盘图标右键菜单重新打开面板")
                return
            self._panel_action()
            return
        if path.startswith(UPSTREAM_BASE):
            self._proxy()
            return
        self._fail(404, "unsupported_endpoint", "本服务只暴露 Codex responses 与本地面板。")

    def _panel_action(self):
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._fail(400, "invalid_json", "请求体不是合法 JSON")
            return
        action = req.get("action")
        eng = self.engine
        if action == "probe_now":
            sess = self._first_session_or_bootstrap()
            if not sess:
                self._json({"ok": False, "error": "没有可用会话（缺少登录凭据）"})
                return
            threading.Thread(target=eng.refresh, args=(sess, True), daemon=True).start()
            self._json({"ok": True})
        elif action == "toggle_injection":
            eng.settings["injection_enabled"] = not eng.settings.get("injection_enabled", True)
            eng.persist.write(_merge_persist(eng))
            log("injection_toggled", enabled=eng.settings["injection_enabled"])
            self._json({"ok": True, "enabled": eng.settings["injection_enabled"]})
        elif action == "set_fallback":
            mode = req.get("mode") if req.get("mode") in ("strict", "passthrough") else None
            if not mode:
                self._fail(400, "invalid_mode", "mode 必须是 strict 或 passthrough")
                return
            eng.settings["fallback"] = mode
            eng.persist.write(_merge_persist(eng))
            self._json({"ok": True})
        elif action == "set_account_mode":
            mode = req.get("mode") if req.get("mode") in ("auto", "personal", "team") else None
            if not mode:
                self._fail(400, "invalid_mode", "mode 必须是 auto / personal / team")
                return
            eng.settings["account_mode"] = mode
            for s in eng.sessions.values():
                s.store.blocks = eng.blocks_for(s)
            eng.persist.write(_merge_persist(eng))
            self._json({"ok": True})
        elif action == "clear_state":
            for s in eng.sessions.values():
                s.store.clear()
            eng.save()
            log("state_cleared")
            self._json({"ok": True})
        elif action == "egress_check":
            sess = self._first_session_or_bootstrap()
            if not sess:
                self._json({"ok": False, "error": "没有可用会话（缺少登录凭据）"})
                return

            def _check_all():
                for e in eng.egresses:
                    for i, q in enumerate(QUALITY_PROBES):
                        r = eng.probe_once(sess, e, prompt=q)
                        log("egress_quality", egress=e.id, round=i + 1,
                            ok=r.get("ok"), single=r.get("quality"),
                            combined=e.quality, answer=e.quality_answer)
                        if not r.get("ok") and r.get("status") in (401, 403, 429):
                            return
                    if e.quality in ("degraded", "severely"):
                        if sess.state_egress == e.id:
                            sess.store.clear()
                            sess.state_healthy = False
                            sess.next_probe = min(sess.next_probe, time.time() + DEGRADED_RETRY)
                            log("egress_quality_isolated", egress=e.id,
                                detail="体检综合判定降智（绑定出口），已清池自愈")
                        else:
                            log("egress_quality_noted", egress=e.id,
                                detail="该出口降智，但 state 绑定其他出口，无需隔离")
                eng.save()
            threading.Thread(target=_check_all, daemon=True).start()
            self._json({"ok": True})
        elif action == "set_keepalive":
            eng.settings["keepalive_enabled"] = bool(req.get("enabled", True))
            if req.get("interval"):
                try:
                    eng.settings["keepalive_interval"] = max(240, min(3600, int(req["interval"])))
                except (TypeError, ValueError):
                    self._fail(400, "invalid_interval", "interval 需为 240-3600 的秒数")
                    return
            eng.persist.write(_merge_persist(eng))
            log("keepalive_updated", enabled=eng.settings["keepalive_enabled"],
                interval=eng.settings.get("keepalive_interval"))
            self._json({"ok": True})
        elif action == "attribute":
            if _mt is None:
                self._json({"ok": False, "error": "指纹库缺失，请重装或从源码运行"})
                return
            if eng.attribution_state.get("running"):
                self._json({"ok": True, "running": True})
                return
            sess = self._first_session_or_bootstrap()
            if not sess:
                self._json({"ok": False, "error": "没有可用会话（缺少登录凭据）"})
                return
            egress = eng.egress_by_id(sess.state_egress) or eng.default_egress()
            eng.attribution_state.update(running=True, started=int(time.time()),
                                         finished=0, error="")

            def _run():
                try:
                    r = eng.deep_attribution(sess, egress)
                    if r.get("ok"):
                        eng.attribution_state["result"] = r
                        eng.attribution_state["error"] = ""
                    else:
                        eng.attribution_state["error"] = r.get("error") or f"失败（status={r.get('status', 0)}）"
                except Exception as e:
                    eng.attribution_state["error"] = f"{e.__class__.__name__}: {e}"[:160]
                finally:
                    eng.attribution_state["running"] = False
                    eng.attribution_state["finished"] = int(time.time())
                log("panel_attribution", ok=not eng.attribution_state["error"],
                    error=eng.attribution_state["error"][:80])
            threading.Thread(target=_run, daemon=True).start()
            self._json({"ok": True, "running": True})
        elif action == "set_timezone":
            mode = req.get("mode") if req.get("mode") in ("off", "fixed") else None
            if not mode:
                self._fail(400, "invalid_mode", "mode 必须是 off 或 fixed")
                return
            eng.settings["tz_mode"] = mode
            if req.get("value"):
                eng.settings["tz_value"] = str(req["value"])[:64]
            eng.persist.write(_merge_persist(eng))
            log("timezone_updated", mode=mode, value=eng.settings.get("tz_value"))
            self._json({"ok": True})
        elif action == "restore_config":
            r = restore_provider()
            self._json(r)
        else:
            self._fail(400, "unknown_action", f"未知操作: {action}")

    def _first_session_or_bootstrap(self):
        return self.engine.probe_target_session()

    # ---- 请求体 ----
    def _read_body(self):
        """返回 (raw, decoded, err)：raw 为原始字节（含压缩），decoded 为解压副本。
        转发默认用 raw（与 Codex 直连零差异），decoded 仅用于内容检查与改写。"""
        enc = (self.headers.get("Content-Encoding") or "").lower()
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return None, None, "request_too_large"
        raw = self.rfile.read(length) if length else b""
        body = raw
        if enc in ("gzip", "x-gzip") and raw:
            body = gzip.decompress(raw)
        elif enc == "deflate" and raw:
            body = zlib.decompress(raw)
        elif enc == "zstd" and raw:
            if _zstd is None:
                return None, None, "zstd_unsupported"
            try:
                body = _zstd.decompress(raw)
            except Exception:
                body = _zstd.ZstdDecompressor().decompressobj().decompress(raw)
        return raw, body, None

    # ---- 核心代理 ----
    def _proxy(self):
        eng = self.engine
        if self.headers.get("Upgrade"):
            self._fail(421, "http_sse_required", "本网关使用 HTTP/SSE，不支持 WebSocket。")
            return
        path = urllib.parse.urlparse(self.path).path
        generation = path in (UPSTREAM_BASE + "/responses", UPSTREAM_BASE + "/responses/compact")
        passthrough_get = self.command == "GET"
        if not generation and not (self.command == "POST" and path == UPSTREAM_BASE + "/alpha/search") \
                and not passthrough_get:
            self._fail(404, "unsupported_endpoint", "Endpoint is not exposed by this service.")
            return

        eng.requests_total += 1
        raw, body, body_err = (None, None, None)
        forward_body = None          # None = 尚未决定；默认转发 raw（零差异保真）
        compact = False
        model = eng.settings.get("model") or DEFAULT_MODEL
        if self.command == "POST":
            raw, body, body_err = self._read_body()
            if body_err:
                self._fail(415 if body_err != "request_too_large" else 413,
                           body_err, "请求体无法解码（编码不受支持或超过 64 MiB）。")
                return
            if generation:
                try:
                    model = json.loads(body).get("model") or model
                except Exception:
                    self._fail(400, "invalid_json", "请求体不是合法 JSON。")
                    return
                if model not in SUPPORTED_MODELS:
                    self._fail(400, "unsupported_model",
                               f"支持的模型：{'、'.join(SUPPORTED_MODELS)}；请在 Codex 中切换。")
                    return
                compact = path.endswith("/compact") or is_compact_trigger(body)
                # 时区归一化（社区实测：本地时区与出口 IP 地区不一致是降级/风控信号；
                # 参考 oai-adversarial-plugin 与 NodeSeek 实测帖，改写请求体内的
                # environment_context 时区，可回滚、默认关闭）
                if eng.settings.get("tz_mode") == "fixed" and eng.settings.get("tz_value"):
                    m = re.search(rb"<timezone>([^<]{1,64})</timezone>", body)
                    if m:
                        eng.last_seen_tz = m.group(1).decode("utf8", "replace")
                        target = eng.settings["tz_value"].encode()
                        body = re.sub(rb"<timezone>[^<]{1,64}</timezone>",
                                      b"<timezone>" + target + b"</timezone>", body)
                        forward_body = body       # 改写后以 identity 转发
                        eng.tz_overrides += 1

        sess, err = eng.borrow(self.headers, model)
        if not sess:
            self._fail(401, "authentication_required", "请先在 Codex 完成登录。")
            return
        lim, wait = eng.limited(sess)
        if lim:
            extra = {"Retry-After": str(wait)} if lim == 429 else None
            self._fail(lim, "upstream_rate_limited" if lim == 429 else "upstream_auth_rejected",
                       "上游要求暂停或已拒绝该凭据；请求未转发。"
                       if lim == 429 else "上游拒绝了该凭据，请先在 Codex 处理登录。",
                       extra)
            return

        inject = generation and not compact and eng.settings.get("injection_enabled", True)
        used = None
        if inject:
            used = sess.store.acquire(time.time())
            if used is None:
                eng.refresh(sess)
                used = sess.store.acquire(time.time())
            if used is None:
                if eng.settings.get("fallback", "passthrough") == "passthrough":
                    inject = False
                    log("request_fallback_passthrough", model=model,
                        detail="无合格 state，按兜底策略正常转发")
                else:
                    self._fail(503, "state_unavailable",
                               f"尚未采到合格 state（{sess.last_probe_result}），"
                               f"严格模式下不转发；可在面板切换兜底或稍后再试。",
                               {"Retry-After": "30"})
                    return

        egress = eng.egress_by_id(sess.state_egress) if (inject and used) else eng.default_egress()
        if egress is None:
            egress = eng.default_egress()

        out_headers = {}
        # 请求保真：除逐跳头与分帧头外全部透传（含 Cookie / Accept-Encoding），
        # 让上游视角与 Codex 直连一致，不引入本地工具特征
        skip = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "te",
                "upgrade", "proxy-authorization", "host", "content-length"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                out_headers[k] = v
        out_headers["Content-Type"] = self.headers.get("Content-Type") or "application/json"
        if self.command == "POST":
            if forward_body is None:
                # 转发原始字节：配套透传原 Content-Encoding，与直连零差异
                forward_body = raw or b""
                if self.headers.get("Content-Encoding"):
                    out_headers["Content-Encoding"] = self.headers["Content-Encoding"]
                else:
                    out_headers.pop("Content-Encoding", None)
            else:
                # 时区改写路径：以 identity 发送改写后的明文
                out_headers.pop("Content-Encoding", None)
        if inject and used:
            out_headers[STATE_HEADER] = used.value

        started = time.time()
        try:
            conn = egress.https_connection(UPSTREAM_HOST, 600)
            upath = urllib.parse.urlparse(self.path).path
            if self.command == "GET":
                conn.request("GET", upath, headers=out_headers)
            else:
                conn.request("POST", upath, body=forward_body or b"", headers=out_headers)
            resp = conn.getresponse()
        except OSError as e:
            egress.last_error = str(e)[:120]
            self._fail(502, "upstream_unavailable",
                       f"连接上游失败（出口 {egress.label}）：请求未重放。{e.__class__.__name__}")
            return

        resp_state = resp.headers.get(STATE_HEADER) or resp.headers.get(STATE_HEADER.lower()) or ""

        if resp.status in (401, 403, 429):
            retry = resp.headers.get("Retry-After") or "0"
            try:
                retry = int(retry)
            except ValueError:
                retry = 0
            eng.mark_limit(sess, resp.status, retry)

        # 转发响应头（逐跳头剔除，长度自行分帧）
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk in ("connection", "keep-alive", "transfer-encoding", "content-length",
                      "proxy-authenticate", "te", "upgrade"):
                continue
            self.send_header(k, v)
        has_len = resp.headers.get("Content-Length") is not None
        if has_len:
            self.send_header("Content-Length", resp.headers["Content-Length"])
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        observer = StreamObserver() if (generation and not compact) else None
        if resp.status >= 400:
            # 错误响应体很小（JSON），读全记日志用于诊断（不含凭据）
            err_body = resp.read(8192)
            log("upstream_error_body", status=resp.status,
                model=model, inject=inject,
                detail=err_body[:400].decode("utf8", "replace"))
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                lk = k.lower()
                if lk in ("connection", "keep-alive", "transfer-encoding",
                          "proxy-authenticate", "te", "upgrade", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            duration_ms = int((time.time() - started) * 1000)
            if observer is not None:
                eng.record_observation(inject, observer, resp.status, model, duration_ms)
            log("request_finished", status=resp.status, model=model, compact=compact,
                inject=inject, egress=egress.id, ms=duration_ms)
            return
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if observer is not None:
                    observer.feed(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client_disconnected", path=urllib.parse.urlparse(self.path).path)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        duration_ms = int((time.time() - started) * 1000)
        if observer is not None:
            eng.record_observation(inject, observer, resp.status, model, duration_ms)
            # 观测驱动自愈：注入请求的 reasoning tokens 相对基线骤降（<40%）时
            # 自动触发两题确认体检（节流 10 分钟；样本不足 3 次不判定）
            if inject and observer.usage.get("reason"):
                r_tok = observer.usage["reason"]
                if sess.obs_reason_ema is None:
                    sess.obs_reason_ema = float(r_tok)
                    sess.obs_samples = 1
                else:
                    sess.obs_samples += 1
                    sess.obs_reason_ema = 0.7 * sess.obs_reason_ema + 0.3 * r_tok
                if (sess.obs_samples >= 3 and r_tok < 0.4 * sess.obs_reason_ema
                        and time.time() - sess.last_autocheck > 600):
                    sess.last_autocheck = time.time()
                    log("observation_anomaly", model=model,
                        reason_tokens=r_tok, baseline=int(sess.obs_reason_ema),
                        detail="推理深度骤降，自动触发出口体检")
                    threading.Thread(target=eng.autocheck, args=(sess,), daemon=True).start()
        if inject and used and 200 <= resp.status < 300:
            if sess.store.observe(resp_state, used):
                log("state_strike", detail="响应 state 形状异常，已计数并切换备用；响应体未销毁")
        log("request_finished", status=resp.status, model=model, compact=compact,
            inject=inject, egress=egress.id, ms=int((time.time() - started) * 1000))


def _merge_persist(engine: Engine):
    data = engine.persist.read()
    settings = engine.settings
    data["settings"] = {k: settings.get(k) for k in
                        ("injection_enabled", "fallback", "account_mode", "model",
                         "keepalive_enabled", "keepalive_interval",
                         "tz_mode", "tz_value")}
    data["panel_key"] = PANEL_KEY
    data["port"] = LISTEN_PORT
    return data


def make_server(engine: Engine, port: int, strict: bool = False):
    Gateway.engine = engine
    candidates = [port] if strict else [port, port + 1, port + 2]
    for cand in candidates:
        try:
            httpd = ThreadingHTTPServer((LISTEN_HOST, cand), Gateway)
            httpd.daemon_threads = True
            return httpd
        except OSError:
            continue
    raise OSError("no free port" + (f"（{LISTEN_HOST}:{port} 已被占用）" if strict else ""))


# ---------------------------------------------------------------- 面板 --------
def _panel_html() -> str:
    """面板页面：独立 panel.html 资源（源码目录或 PyInstaller 解包目录）。"""
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).parent
    for cand in (base / "panel.html", Path(__file__).parent / "panel.html"):
        if cand.exists():
            return cand.read_text(encoding="utf8")
    return ("<html><body style=\"font-family:system-ui;padding:40px;color:#5f6672\">"
            "面板资源缺失（panel.html），请重新安装。</body></html>")




# ---------------------------------------------------------------- 图标与托盘 --
def build_icon_image():
    """渐变圆角底 + 白色月牙的托盘图标（Pillow 现场绘制，免资源文件）。"""
    from PIL import Image, ImageChops, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / size
        color = (int(0x4F + (0x8B - 0x4F) * t), int(0x6B + (0x5C - 0x6B) * t),
                 int(0xF0 + (0xF6 - 0xF0) * t), 255)
        d.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], radius=16, fill=255)
    img.putalpha(mask)
    c1 = Image.new("L", (size, size), 0)
    ImageDraw.Draw(c1).ellipse([16, 14, 50, 48], fill=255)
    c2 = Image.new("L", (size, size), 0)
    ImageDraw.Draw(c2).ellipse([27, 7, 59, 39], fill=255)
    img.paste((255, 255, 255, 240), (0, 0), ImageChops.subtract(c1, c2))
    return img


def icon_file() -> Path:
    """托盘/快捷方式用的 .ico，首次使用时生成并缓存。"""
    p = data_dir() / "tray.ico"
    if not p.exists():
        img = build_icon_image()
        img.save(p, format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
    return p


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _self_cmd(*extra) -> list:
    """以子进程方式重新调用本程序（兼容源码运行与 PyInstaller exe）。"""
    if is_frozen():
        return [sys.executable, *extra]
    return [sys.executable, os.path.abspath(__file__), *extra]


CREATE_NO_WINDOW = 0x08000000


def gateway_alive(port=None) -> bool:
    d = Persist().read()
    port = port or d.get("port") or LISTEN_PORT
    key = d.get("panel_key") or ""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", f"/panel/api/status?key={key}")
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status == 200
    except Exception:
        return False


def spawn_panel():
    """面板窗口独立子进程（pywebview 要求独占主线程）。"""
    url_port_key()
    try:
        subprocess.Popen(_self_cmd("panel"), close_fds=True,
                         creationflags=CREATE_NO_WINDOW if not is_frozen() else 0)
        log("panel_spawned")
        return True
    except Exception as e:
        log("panel_spawn_failed", level="error", err=str(e)[:160])
        return False


def url_port_key():
    d = Persist().read()
    return f"http://{LISTEN_HOST}:{d.get('port') or LISTEN_PORT}/panel", d.get("port") or LISTEN_PORT, d.get("panel_key", "")


def _enable_dpi_awareness():
    """声明 per-monitor DPI 感知；否则 Win32 托盘菜单/图标在高缩放下被位图拉伸发糊。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def run_tray(engine: "Engine", url: str):
    """系统托盘模式：主线程跑托盘消息循环，网关在线程池中。"""
    _enable_dpi_awareness()
    import pystray
    from pystray import Menu, MenuItem

    def open_panel(item):
        spawn_panel()

    def probe_now(item):
        sess = engine.probe_target_session()
        if sess:
            threading.Thread(target=engine.refresh, args=(sess, True), daemon=True).start()
            log("tray_probe_requested")
        else:
            log("tray_probe_failed", level="warn", detail="没有可用凭据（未在 Codex 登录）")

    def toggle_injection(item):
        engine.settings["injection_enabled"] = not engine.settings.get("injection_enabled", True)
        engine.persist.write(_merge_persist(engine))
        log("injection_toggled", enabled=engine.settings["injection_enabled"])

    def toggle_autorun(item):
        if autorun_registered():
            autorun_unregister()
        else:
            autorun_register()

    def restore_now(item):
        r = restore_provider()
        log("tray_restore_config", ok=r.get("ok"))

    def quit_app(icon):
        icon.stop()

    menu = Menu(
        MenuItem("打开状态面板", open_panel, default=True),
        MenuItem("立即采集 state", probe_now),
        MenuItem("turn-state 注入", toggle_injection,
                 checked=lambda item: bool(engine.settings.get("injection_enabled", True))),
        Menu.SEPARATOR,
        MenuItem("开机自启", toggle_autorun, checked=lambda item: autorun_registered()),
        MenuItem("恢复 Codex 配置", restore_now),
        Menu.SEPARATOR,
        MenuItem("退出（自动恢复 Codex 配置）", quit_app),
    )
    icon = pystray.Icon(APP, build_icon_image(),
                        "ccodex sleep plus — turn-state 网关", menu)
    log("tray_started", detail="面板可从托盘菜单打开")
    icon.run()   # 阻塞至退出


# ---------------------------------------------------------------- 安装器 ------
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "Programs" / APP
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autorun_cmd() -> str:
    if is_frozen():
        return f'"{INSTALL_DIR / (APP + ".exe")}" tray'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" tray'


def autorun_register():
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, autorun_cmd())
    winreg.CloseKey(k)
    log("autorun_registered", cmd=autorun_cmd())


def autorun_unregister():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, APP)
        winreg.CloseKey(k)
    except OSError:
        pass
    log("autorun_unregistered")


def autorun_registered() -> bool:
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        winreg.QueryValueEx(k, APP)
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def _desktop_dir() -> Path:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True).stdout.decode("gbk", errors="ignore").strip()
    return Path(out) if out else Path.home() / "Desktop"


def _start_menu_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"


def shortcut_create(lnk: Path, target: str, args: str, icon: str, desc: str):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{l}');"
          "$s.TargetPath='{t}';$s.Arguments='{a}';$s.IconLocation='{i}';"
          "$s.Description='{d}';$s.Save()").format(
        l=str(lnk), t=target, a=args, i=icon, d=desc)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True)
    return r.returncode == 0


def install_shortcuts(target: str, args: str):
    icon = str(icon_file())
    for d, name in ((_desktop_dir(), "ccodex sleep plus.lnk"),
                    (_start_menu_dir(), "ccodex sleep plus.lnk")):
        try:
            d.mkdir(parents=True, exist_ok=True)
            ok = shortcut_create(d / name, target, args, icon, "ccodex sleep plus 控制面板")
            log("shortcut_created", path=str(d / name), ok=ok)
        except Exception as e:
            log("shortcut_failed", level="warn", path=str(d / name), err=str(e)[:120])


def remove_shortcuts():
    for d, name in ((_desktop_dir(), "ccodex sleep plus.lnk"),
                    (_start_menu_dir(), "ccodex sleep plus.lnk")):
        try:
            p = d / name
            if p.exists():
                p.unlink()
                log("shortcut_removed", path=str(p))
        except OSError as e:
            log("shortcut_remove_failed", level="warn", err=str(e)[:120])


def test_egress_egress(e: Egress) -> str:
    """只测出口到 chatgpt.com 的 TLS 握手，不发 HTTP 请求、不耗额度。"""
    try:
        raw = e.open_socket(UPSTREAM_HOST, 443, 5)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=UPSTREAM_HOST) as s:
            s.do_handshake()
        e.last_ok = time.time()
        return "ok"
    except Exception as ex:
        e.last_error = str(ex)[:100]
        return f"fail: {e.last_error}"


def _say(msg):
    """安装器输出：控制台 + 日志双通道（noconsole 下仍落文件）。"""
    try:
        print(msg)
    except Exception:
        pass
    log("install", detail=str(msg)[:200])


def cmd_install() -> int:
    _say("=" * 56)
    _say(" ccodex-sleep-plus 安装")
    _say("=" * 56)

    # 1. 识别 Codex 位置
    chome = codex_home()
    cfg, auth = config_path(), auth_path()
    _say(f"\n[1/5] Codex 位置识别")
    _say(f"      CODEX_HOME = {chome}")
    _say(f"      config.toml: {'√ 存在' if cfg.exists() else '× 缺失'}")
    _say(f"      auth.json  : {'√ 已登录' if auth.exists() else '× 未登录'}")
    if not cfg.exists() or not auth.exists():
        _say("\n未找到有效的 Codex 配置/登录。请先安装 Codex 并完成登录再运行本安装器。")
        return 1

    # 2. 识别代理出口
    _say(f"\n[2/5] 代理出口识别（仅握手测试，不发请求、不耗额度）")
    egs = detect_egresses()
    healthy = []
    for e in egs:
        r = test_egress_egress(e)
        mark = "√" if r == "ok" else "×"
        _say(f"      {mark} {e.label:<28} {r}")
        if r == "ok" and e.kind != "direct":
            healthy.append(e)
    if not healthy:
        _say("      （未发现可用代理出口；将使用直连，可能无法访问 chatgpt.com）")

    # 3. 安装文件
    _say(f"\n[3/5] 安装程序文件")
    if is_frozen():
        try:
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            exe = INSTALL_DIR / f"{APP}.exe"
            shutil.copy2(sys.executable, exe)
            _say(f"      已安装: {exe}")
            target, args = str(exe), "smart"
        except PermissionError:
            exe = INSTALL_DIR / f"{APP}.exe"
            _say(f"      文件被占用，保持现有: {exe}")
            target, args = str(exe), "smart"
    else:
        _say(f"      源码模式：不复制文件，直接使用 {os.path.abspath(__file__)}")
        target, args = sys.executable, f'"{os.path.abspath(__file__)}" smart'

    # 4. 快捷方式（桌面 + 开始菜单，指向 smart：服务在跑就开面板，没跑就启动托盘）
    _say(f"\n[4/5] 创建快捷方式（桌面 + 开始菜单）")
    install_shortcuts(target, args)
    _say(f"      开机自启：默认关闭，可在托盘右键菜单中开启")

    # 5. 启动
    _say(f"\n[5/5] 启动服务")
    if gateway_alive():
        _say("      服务已在运行，直接打开面板")
        spawn_panel()
    else:
        if is_frozen():
            subprocess.Popen(_self_cmd("tray"))
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            subprocess.Popen([str(pyw), os.path.abspath(__file__), "tray"],
                             creationflags=CREATE_NO_WINDOW, close_fds=True)
        import time as _t
        _t.sleep(2.5)
        d = Persist().read()
        _say(f"      网关: http://{LISTEN_HOST}:{d.get('port') or LISTEN_PORT}{UPSTREAM_BASE}"
              + ("（运行中）" if gateway_alive() else "（启动中，稍后从托盘打开面板）"))
    _say("\n安装完成。托盘图标常驻后台；关闭面板窗口不会停止服务。")
    _say("卸载：ccodex-sleep-plus.exe uninstall（或 python sleep_plus.py uninstall）")
    return 0


def cmd_uninstall() -> int:
    _say("卸载 ccodex-sleep-plus …")
    d = Persist().read()
    pid = d.get("pid")
    if pid:
        try:
            os.kill(pid, 9)
            _say(f"  已停止服务 pid={pid}")
        except OSError:
            pass
    r = restore_provider()
    _say(f"  恢复 Codex 配置: {'√' if r.get('ok') else r.get('error')}")
    autorun_unregister()
    remove_shortcuts()
    try:
        if INSTALL_DIR.exists():
            exe = INSTALL_DIR / f"{APP}.exe"
            if exe.exists():
                exe.unlink()
            try:
                INSTALL_DIR.rmdir()
                _say(f"  已删除 {INSTALL_DIR}")
            except OSError:
                _say(f"  程序目录非空，保留: {INSTALL_DIR}")
    except OSError as e:
        _say(f"  删除程序目录失败: {e}")
    _say(f"  数据保留在 {data_dir()}（含采集的 state 与观测记录），可手动删除")
    _say("卸载完成。请重启 Codex 使配置生效。")
    return 0


# ---------------------------------------------------------------- 自检 --------
def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # 合成一个合法 state（10 块）走 parse
    issued = int(time.time()) - 30
    raw = b"\x80" + issued.to_bytes(8, "big") + b"\x11" * 48 + b"\x22" * (16 * 10)
    val = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    st = parse_state(val)
    check("parse_state blocks=10", st is not None and st.blocks == 10 and st.issued == issued)
    check("state length 292", len(val + "=" * (-len(val) % 4)) == 292)

    team_raw = b"\x80" + issued.to_bytes(8, "big") + b"\x11" * 48 + b"\x22" * (16 * 12)
    team_val = base64.urlsafe_b64encode(team_raw).decode().rstrip("=")
    st2 = parse_state(team_val)
    check("team blocks=12 / 332 chars", st2.blocks == 12 and len(team_val + "=" * (-len(team_val) % 4)) == 332)

    check("reject garbage", parse_state("abc!!!") is None)
    check("reject empty", parse_state("") is None)
    check("reject whitespace", parse_state(val[:20] + " " + val[20:]) is None)

    store = StateStore({10})
    check("store needs refresh when empty", store.needs_refresh(time.time()))
    check("store offer accepts", store.offer(st))
    check("store acquire usable", store.acquire(time.time()) is not None)
    check("store rejects wrong blocks", not store.offer(st2))

    sse = (b"event: response.completed\n" + b'data: {"type":"response.completed"}\n\n')
    check("sse completed", stream_outcome(sse)[0] is True)
    sse_fail = b'data: {"type":"error","code":"rate_limit_exceeded"}\n\n'
    c, f, s = stream_outcome(sse_fail)
    check("sse rate limit", not c and f == "upstream_rate_limited" and s == 429)

    body = json.dumps({"input": [{"type": "message"}, {"type": "compaction_trigger"}]}).encode()
    check("compact trigger detect", is_compact_trigger(body))
    check("compact trigger negative", not is_compact_trigger(b'{"input":[{"type":"message"}]}'))

    # 33 块（780 字符）官方新形状（issue #13）
    raw33 = bytes([0x80]) + issued.to_bytes(8, "big") + bytes([0x11]) * 48 + bytes([0x22]) * (16 * 33)
    st33 = parse_state(base64.urlsafe_b64encode(raw33).decode().rstrip("="))
    check("33-block state parses", st33 is not None and st33.blocks == 33)
    store33 = StateStore({10, 33})
    check("store accepts 33-block (new shape)", store33.offer(st33))
    check("store pool keeps multiple", StateStore.POOL_SIZE >= 3)

    # StreamObserver 跨块解析
    obs = StreamObserver()
    ev1 = ('data: {"type":"response.created","response":{"model":"gpt-6-astra"}}'
           + chr(10) + chr(10)).encode()
    ev2 = ('data: {"type":"response.completed","response":{"model":"gpt-6-astra",'
           '"usage":{"input_tokens":100,"output_tokens":250,'
           '"output_tokens_details":{"reasoning_tokens":120}}}}'
           + chr(10) + chr(10)).encode()
    obs.feed(ev1[:15]); obs.feed(ev1[15:] + ev2[:30]); obs.feed(ev2[30:])
    check("observer model parsed", obs.model == "gpt-6-astra")
    check("observer usage parsed", obs.usage == {"in": 100, "out": 250, "reason": 120})

    check("judge quality healthy", judge_quality("iPhone 17 Pro Max") == "healthy")
    check("judge quality degraded", judge_quality("The latest is iPhone 16 Pro") == "degraded")
    check("judge quality severely", judge_quality("iPhone 15") == "severely")
    check("judge quality unknown", judge_quality("I cannot answer") == "unknown")
    a1 = sse_answer_text(
        ('data: {"type":"response.output_text.delta","delta":"iPhone "}' + chr(10) + chr(10)
         + 'data: {"type":"response.output_text.delta","delta":"17 Pro"}' + chr(10) + chr(10)).encode())
    check("sse answer delta concat", a1 == "iPhone 17 Pro")

    # 托盘/图标栈可用性（exe 打包缺 PIL C 扩展时在此暴露）
    try:
        from PIL import Image, ImageChops, ImageDraw  # noqa: F401
        check("PIL importable (tray icon)", True)
    except Exception as e:
        check(f"PIL importable ({e})", False)
    try:
        import pystray  # noqa: F401
        check("pystray importable (tray)", True)
    except Exception as e:
        check(f"pystray importable ({e})", False)
    try:
        img = build_icon_image()
        check("icon builds", img.size == (64, 64))
    except Exception as e:
        check(f"icon builds ({e})", False)

    html = _panel_html()
    check("panel.html present", "重新体检" in html and "btn-check" in html)

    # ModelTrace 归因栈
    if _mt is not None:
        chs = _mt.generate_challenges(3)
        check("modeltrace challenges", len(chs) == 3 and
              all(292 <= c["expected_count"] <= 332 for c in chs))
        bank = _mt.load_bank()
        ids = [m["id"] for m in bank["models"]]
        check("modeltrace bank", "gpt-6-astra" in ids and "gpt-5.6-luna" in ids)
        check("modeltrace parse", _mt.parse_numbers("1, 5, 999, 7 8") == [1, 5, 7, 8])
    else:
        check("modeltrace available", False)

    print("selftest", "OK" if ok else "FAILED")
    return ok


def panel_window_available() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def run_panel_window(url: str):
    """在主线程运行独立面板窗口（pywebview 要求主线程）。阻塞至窗口关闭。"""
    import webview
    webview.create_window("ccodex sleep plus · 状态面板", url,
                          width=1180, height=1020, min_size=(760, 640),
                          background_color="#F4F6FB")
    webview.start()


# ---------------------------------------------------------------- 主入口 ------
def serve_supervised(httpd, engine):
    """网关监督线程：崩了自动重启 serve_forever，避免 Codex 指向死端口。"""
    def loop():
        while not engine.stop.is_set():
            try:
                httpd.serve_forever(poll_interval=1)
                return
            except Exception as e:
                log("gateway_thread_crashed", level="error", err=str(e)[:160])
                time.sleep(5)
    threading.Thread(target=loop, daemon=True, name="gateway-supervisor").start()


def main():
    global PANEL_KEY, LISTEN_PORT
    ap = argparse.ArgumentParser(description="ccodex-sleep-plus — Codex turn-state 本地网关（增强版）")
    ap.add_argument("command", choices=["serve", "tray", "panel", "smart", "install",
                                        "uninstall", "link", "restore", "stop", "status",
                                        "probe", "selftest"])
    ap.add_argument("--no-install", action="store_true", help="启动时不修改 Codex 配置")
    ap.add_argument("--no-open", action="store_true", help="serve 模式不打开面板窗口")
    ap.add_argument("--browser", action="store_true",
                    help="serve 模式显式用系统浏览器打开面板（默认独立窗口/托盘，不用浏览器）")
    args = ap.parse_args()

    if args.command == "selftest":
        sys.exit(0 if selftest() else 1)

    if args.command == "install":
        sys.exit(cmd_install())

    if args.command == "uninstall":
        sys.exit(cmd_uninstall())

    if args.command == "link":
        r = install_provider()
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)

    if args.command == "restore":
        r = restore_provider()
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)

    if args.command == "stop":
        r = restore_provider()
        pid = (Persist().read() or {}).get("pid")
        if pid:
            try:
                os.kill(pid, 9)
                print(f"stopped pid={pid}")
            except (ProcessLookupError, PermissionError, OSError) as e:
                print(f"kill pid={pid} failed: {e}")
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)

    persist = Persist()
    saved = persist.read()
    settings = saved.get("settings") or {}
    settings.setdefault("injection_enabled", True)
    settings.setdefault("fallback", "passthrough")
    settings.setdefault("account_mode", "auto")
    settings.setdefault("model", DEFAULT_MODEL)
    settings.setdefault("keepalive_enabled", True)
    settings.setdefault("keepalive_interval", KEEPALIVE_DEFAULT)
    settings.setdefault("tz_mode", "off")            # off | fixed
    settings.setdefault("tz_value", "America/Los_Angeles")
    # 面板密钥：优先沿用已保存值；否则按本机派生（跨重启稳定，面板链接不会过期）
    PANEL_KEY = saved.get("panel_key") or hashlib.sha256(
        f"ccodex-sleep-plus:{Path.home()}".encode()).hexdigest()[:16]
    LISTEN_PORT = saved.get("port") or LISTEN_PORT

    if args.command == "panel":
        # 端口互斥防止叠开多个面板窗口
        try:
            _panel_mutex = socket.socket()
            _panel_mutex.bind(("127.0.0.1", 17899))
            _panel_mutex.listen(1)
            globals()["_PANEL_MUTEX"] = _panel_mutex
        except OSError:
            print("面板窗口已打开")
            return
        url, _port, _key = url_port_key()
        if panel_window_available():
            _enable_dpi_awareness()
            log("panel_window_opened", url_host=url.split("/panel")[0])
            run_panel_window(url)          # 阻塞至窗口关闭
        else:
            print(f"未安装 pywebview（pip install pywebview），面板地址: {url}")
        return

    if args.command == "smart":
        # 桌面快捷方式入口：服务在跑就开面板，没跑就拉起托盘
        if gateway_alive():
            spawn_panel()
            return
        args = argparse.Namespace(**{**vars(args), "command": "tray"})

    engine = Engine(settings, persist)

    if args.command == "status":
        print(json.dumps(engine.status(), ensure_ascii=False, indent=2))
        return

    if args.command == "probe":
        headers = load_codex_auth()
        if not headers:
            print("未读取到 ~/.codex/auth.json，请先在 Codex 登录")
            sys.exit(1)
        sess, err = engine.borrow(headers, settings["model"])
        if not sess:
            print("borrow 失败:", err)
            sys.exit(1)
        print(f"plan={sess.plan} expect_blocks={sorted(engine.blocks_for(sess))} model={sess.model}")
        for e in engine.egresses:
            r = engine.probe_once(sess, e)
            print(json.dumps({k: v for k, v in r.items() if k != "state"}, ensure_ascii=False))
            if r.get("ok") and r.get("shape_ok"):
                print(f"采到合格 state：blocks={r['blocks']} fp={r['state'].fingerprint} egress={e.id}")
                sess.store.offer(r["state"])
                sess.state_egress = e.id
                engine.save()
                sys.exit(0)
            if r.get("status") in (401, 403, 429):
                sys.exit(2)
        print("本轮未采到合格 state（详见上方结果）")
        sys.exit(3)

    # serve / tray 启动
    if gateway_alive():
        log("already_running", detail="网关已在运行，本实例退出")
        if args.command in ("tray", "serve"):
            spawn_panel()
        sys.exit(0)

    httpd = make_server(engine, LISTEN_PORT, strict=True)
    real_port = httpd.server_address[1]
    LISTEN_PORT = real_port
    persist.write({**saved, "panel_key": PANEL_KEY, "port": real_port,
                   "pid": os.getpid(), "settings": settings})
    url = f"http://{LISTEN_HOST}:{real_port}/panel?key={PANEL_KEY}"
    log("gateway_started", mode=args.command, listen=f"{LISTEN_HOST}:{real_port}",
        egresses=",".join(e.id for e in engine.egresses))

    if not args.no_install:
        r = install_provider(port=real_port, model=settings.get("model"))
        log("install_provider", ok=r.get("ok"), detail=r.get("error") or ("already" if r.get("already") else "patched"))

    threading.Thread(target=engine.refresher_loop, daemon=True).start()
    threading.Thread(target=watch_config_loop, args=(engine,), daemon=True).start()
    threading.Thread(target=engine.bootstrap_from_auth, daemon=True).start()
    serve_supervised(httpd, engine)

    use_window = (args.command == "serve" and not args.no_open
                  and not args.browser and panel_window_available())
    if args.command == "serve" and args.browser and not args.no_open:
        webbrowser.open(url)
    try:
        if use_window:
            log("panel_window_opened", url_host=url.split("/panel")[0])
            run_panel_window(url)          # serve 模式：窗口关闭即退出
        elif args.command == "tray":
            run_tray(engine, url)          # 托盘模式：退出菜单即退出
        else:
            if args.command == "serve" and not args.no_open and not args.browser:
                log("panel_window_unavailable", level="warn",
                    hint="可选: pip install pywebview 后重启即有独立窗口")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop.set()
        engine.save()
        httpd.shutdown()
        if not args.no_install:
            restore_provider()
        log("gateway_stopped", detail="配置已恢复，请重启 Codex")


def _crash_dialog(exc: BaseException):
    """noconsole exe 崩溃时写日志并弹窗，绝不静默消失。"""
    import traceback
    msg = "".join(traceback.format_exception(exc)).strip()
    try:
        (data_dir() / "crash.log").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S") + chr(10) + msg, encoding="utf8")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "ccodex-sleep-plus 发生错误，详情见 crash.log：" + chr(10) + chr(10)
            + msg[-500:], APP, 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _exc:
        _crash_dialog(_exc)
        sys.exit(1)
