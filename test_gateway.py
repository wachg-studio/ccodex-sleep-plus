# -*- coding: utf-8 -*-
"""端到端链路测试（mock 上游，零额度消耗）。"""
import base64
import http.client
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sleep_plus as sp

MOCK_PORT = 18990
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# ---- mock 上游：返回带 state 头的 SSE ----
def make_state(blocks=10, age=30):
    issued = int(time.time()) - age
    raw = b"\x80" + issued.to_bytes(8, "big") + b"\x11" * 48 + b"\x22" * (16 * blocks)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


MOCK_STATE = make_state(10)
seen = {}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        seen["state_header"] = self.headers.get(sp.STATE_HEADER)
        seen["auth"] = self.headers.get("Authorization")
        seen["model"] = json.loads(body).get("model")
        if self.path.endswith("/responses"):
            payload = (b"event: response.completed\n"
                       b'data: {"type":"response.completed"}\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header(sp.STATE_HEADER, MOCK_STATE)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        payload = b'{"models":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---- 把引擎的上游指向 mock（明文本地） ----
class HTTPEgress(sp.Egress):
    def https_connection(self, host, timeout):
        conn = http.client.HTTPConnection("127.0.0.1", MOCK_PORT, timeout=timeout)
        conn._egress = self
        return conn


def run_gateway_test():
    upstream = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockUpstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    sp.UPSTREAM_HOST = "127.0.0.1"
    persist = sp.Persist()
    engine = sp.Engine({"injection_enabled": True, "fallback": "passthrough",
                        "account_mode": "auto", "model": "gpt-6-astra"}, persist)
    engine.egresses = [HTTPEgress("mock", "mock-upstream", "direct", "127.0.0.1", MOCK_PORT)]

    # 预置一个可用 state，让正式请求带注入
    st = sp.parse_state(MAKE_TEST_STATE := make_state(10, 60))
    sess, err = engine.borrow({"Authorization": "Bearer test-token-1234567890",
                               "chatgpt-account-id": "acc-1"}, "gpt-6-astra")
    check("borrow ok", sess is not None)
    check("plan detected personal", sess.plan == "personal")
    sess.store.offer(st)
    sess.state_egress = "mock"

    httpd = sp.make_server(engine, 17899)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 1) 带注入的生成请求
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=45)
    body = json.dumps({"model": "gpt-6-astra", "input": [], "stream": True}).encode()
    c.request("POST", sp.UPSTREAM_BASE + "/responses", body=body,
              headers={"Authorization": "Bearer test-token-1234567890",
                       "chatgpt-account-id": "acc-1",
                       "Content-Type": "application/json",
                       "Accept": "text/event-stream",
                       "session_id": "sess-1"})
    r = c.getresponse()
    data = r.read()
    check("generation status 200", r.status == 200)
    check("generation sse body", b"response.completed" in data)
    check("state injected upstream", seen.get("state_header") == MAKE_TEST_STATE)
    check("auth forwarded", seen.get("auth") == "Bearer test-token-1234567890")
    check("model forwarded", seen.get("model") == "gpt-6-astra")

    # 2) gzip 请求体解码
    import gzip as gz
    gz_body = gz.compress(json.dumps({"model": "gpt-6-astra", "input": []}).encode())
    c.request("POST", sp.UPSTREAM_BASE + "/responses", body=gz_body,
              headers={"Authorization": "Bearer test-token-1234567890",
                       "chatgpt-account-id": "acc-1",
                       "Content-Type": "application/json",
                       "Content-Encoding": "gzip"})
    r = c.getresponse()
    r.read()
    check("gzip request decoded", r.status == 200 and seen.get("model") == "gpt-6-astra")

    # 3) 不支持的模型被拦
    c.request("POST", sp.UPSTREAM_BASE + "/responses",
              body=json.dumps({"model": "gpt-4", "input": []}).encode(),
              headers={"Authorization": "Bearer t", "Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    check("unsupported model rejected", r.status == 400)

    # 4) 无凭据被拦
    c.request("POST", sp.UPSTREAM_BASE + "/responses",
              body=json.dumps({"model": "gpt-6-astra", "input": []}).encode(),
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    check("no auth rejected", r.status == 401)

    # 5) models GET 透传
    c.request("GET", sp.UPSTREAM_BASE + "/models",
              headers={"Authorization": "Bearer test-token-1234567890"})
    r = c.getresponse()
    r.read()
    check("models passthrough", r.status == 200)

    # 6) shape strike：上游返回 team 形状(12 块)时记 strike 但不销毁响应
    global MOCK_STATE
    MOCK_STATE = make_state(12)
    c.request("POST", sp.UPSTREAM_BASE + "/responses",
              body=json.dumps({"model": "gpt-6-astra", "input": []}).encode(),
              headers={"Authorization": "Bearer test-token-1234567890",
                       "chatgpt-account-id": "acc-1", "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    check("shape mismatch keeps body", r.status == 200 and b"response.completed" in data)
    time.sleep(0.5)  # 服务端在响应体发完后才执行 observe，等它记完 strike
    check("strike recorded", sess.store.strikes >= 1)

    # 7) 面板 status API
    sp.PANEL_KEY = "testkey"
    c.request("GET", f"/panel/api/status?key=testkey")
    r = c.getresponse()
    st = json.loads(r.read())
    check("panel status api", r.status == 200 and st["requests_total"] >= 5)
    c.request("GET", "/panel/api/status?key=wrong")
    r = c.getresponse()
    r.read()
    check("panel key rejected", r.status == 403)

    httpd.shutdown()
    upstream.shutdown()


# ---- config 接管/恢复测试（临时 CODEX_HOME，不碰真配置） ----
def run_config_test():
    tmp = Path(os.environ["TEMP"]) / "sleep-plus-test-codex"
    tmp.mkdir(exist_ok=True)
    (tmp / "auth.json").write_text("{}", encoding="utf8")
    (tmp / "config.toml").write_text(
        'responses_websocket = false\nmodel = "gpt-6-astra"\nsandbox_mode = "danger-full-access"\n'
        '[desktop]\nappearanceTheme = "light"\n\n[projects.x]\ntrust_level = "trusted"\n',
        encoding="utf8")
    os.environ["CODEX_HOME"] = str(tmp)
    import importlib
    importlib.reload(sp)

    r1 = sp.install_provider(port=17841)
    check("install ok", r1.get("ok"))
    text = (tmp / "config.toml").read_text(encoding="utf8")
    doc = sp.tomllib.loads(text)
    check("patched provider present", doc["model_provider"] == "sleep-plus")
    check("provider base_url", doc["model_providers"]["sleep-plus"]["base_url"]
          == "http://127.0.0.1:17841/backend-api/codex")
    check("other keys kept", doc["desktop"]["appearanceTheme"] == "light"
          and doc["projects"]["x"]["trust_level"] == "trusted"
          and doc["model"] == "gpt-6-astra")

    r2 = sp.install_provider(port=17841)
    check("install idempotent", r2.get("ok") and (r2.get("already") or r2.get("ok")))

    # 桌面版重写配置（丢掉 managed 块）后再 install 应能补回
    (tmp / "config.toml").write_text(
        'model = "gpt-6-astra"\n\n[desktop]\nappearanceTheme = "dark"\n', encoding="utf8")
    r3 = sp.install_provider(port=17841)
    doc3 = sp.tomllib.loads((tmp / "config.toml").read_text(encoding="utf8"))
    check("re-install after rewrite", r3.get("ok") and doc3["model_provider"] == "sleep-plus")

    check("config_linked true", sp.config_linked())
    r4 = sp.restore_provider()
    doc4 = sp.tomllib.loads((tmp / "config.toml").read_text(encoding="utf8"))
    check("restore removes provider", r4.get("ok")
          and "model_provider" not in doc4
          and "sleep-plus" not in (doc4.get("model_providers") or {})
          and doc4["desktop"]["appearanceTheme"] == "dark")
    check("config_linked false after restore", not sp.config_linked())
    r5 = sp.restore_provider()
    check("restore idempotent", r5.get("ok"))


# ---- 安装器 dry-run（副作用全 stub，捕获 NameError/逻辑回归） ----
def run_installer_dryrun_test():
    import types
    import subprocess
    tmp = Path(os.environ["TEMP"]) / "sleep-plus-test-codex"
    tmp.mkdir(exist_ok=True)
    (tmp / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf8")
    (tmp / "auth.json").write_text("{}", encoding="utf8")
    os.environ["CODEX_HOME"] = str(tmp)

    import sleep_plus as sp
    calls = []
    sp.test_egress_egress = lambda e: (calls.append("egress_test"), "ok")[1]
    sp.install_shortcuts = lambda *a, **k: calls.append("shortcuts")
    sp.gateway_alive = lambda *a: False
    sp.spawn_panel = lambda: calls.append("panel")
    real_popen = subprocess.Popen
    sp.subprocess.Popen = lambda *a, **k: (calls.append("popen"), object())[1]
    sp.time.sleep = lambda s: None
    try:
        rc = sp.cmd_install()
        check("cmd_install dry-run completes", rc in (0, 1))
        check("cmd_install ran stages", "egress_test" in calls and "shortcuts" in calls)
    except Exception as e:
        check("cmd_install dry-run (failed: %s %s)" % (e.__class__.__name__, e), False)
    finally:
        sp.subprocess.Popen = real_popen


if __name__ == "__main__":
    run_gateway_test()
    run_config_test()
    run_installer_dryrun_test()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
