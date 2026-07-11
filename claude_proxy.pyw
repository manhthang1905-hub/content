#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Digi Proxy — Auto Key Rotation
=======================================
Local proxy chạy trên localhost:8318, forward request tới Digi Gateway.
Khi key hết quota (403/429) → tự động chuyển key tiếp theo.

VS Code chỉ cần trỏ vào http://127.0.0.1:8318 — KHÔNG cần restart khi đổi key.

Cách dùng:
  1. Chạy: python claude_proxy.pyw
  2. VS Code settings.json: ANTHROPIC_BASE_URL = http://127.0.0.1:8318
  3. Proxy tự rotate key khi 403/429

Config đọc từ claude_switcher_config.json (cùng thư mục).
"""

import http.server
import json
import os
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Config ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "claude_switcher_config.json")
_PROXY_PORT = 8318
_QUOTA_URL = "https://token-quota.digishop.work"

def load_config():
    default = {"base_url": "https://vip.digishop.work", "quota_url": _QUOTA_URL,
               "model": "claude-opus-4-8", "keys": []}
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            default.update(saved)
        except Exception:
            pass
    return default

CONFIG = load_config()

# ── Key Manager ───────────────────────────────────────────────────────────────
class KeyManager:
    """Quản lý và rotate key tự động"""

    def __init__(self, keys: list[str], base_url: str):
        self.keys = list(keys)
        self.base_url = base_url
        self._current_idx = 0
        self._cooldown: dict[str, float] = {}  # key -> cooldown_until timestamp
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {k: {"ok": 0, "fail": 0} for k in keys}

    @property
    def current_key(self) -> str:
        if not self.keys:
            return ""
        return self.keys[self._current_idx % len(self.keys)]

    def short(self, key: str = "") -> str:
        k = key or self.current_key
        return f"...{k[-6:]}" if k and len(k) > 6 else "N/A"

    def mark_success(self, key: str):
        with self._lock:
            if key in self._stats:
                self._stats[key]["ok"] += 1

    def mark_failed(self, key: str):
        """Mark key as failed, cooldown 10 min, rotate to next"""
        with self._lock:
            if key in self._stats:
                self._stats[key]["fail"] += 1
            self._cooldown[key] = time.time() + 600  # 10 min cooldown
            self._rotate()

    def _rotate(self):
        """Tìm key tiếp theo không bị cooldown"""
        now = time.time()
        for offset in range(1, len(self.keys) + 1):
            idx = (self._current_idx + offset) % len(self.keys)
            k = self.keys[idx]
            if self._cooldown.get(k, 0) <= now:
                old = self.short()
                self._current_idx = idx
                _log(f"🔄 KEY ROTATE: {old} → {self.short()} ({self._stats.get(k, {})})")
                return
        # Tất cả cooldown → dùng key hết cooldown sớm nhất
        earliest_idx = min(range(len(self.keys)),
                          key=lambda i: self._cooldown.get(self.keys[i], 0))
        self._current_idx = earliest_idx
        _log(f"⚠️ Tất cả key cooldown — dùng {self.short()} (hết cooldown sớm nhất)")

    def get_best_key(self) -> str:
        """Lấy key tốt nhất (không cooldown)"""
        with self._lock:
            now = time.time()
            for offset in range(len(self.keys)):
                idx = (self._current_idx + offset) % len(self.keys)
                k = self.keys[idx]
                if self._cooldown.get(k, 0) <= now:
                    self._current_idx = idx
                    return k
            return self.current_key

    def status_summary(self) -> str:
        now = time.time()
        lines = []
        for i, k in enumerate(self.keys):
            s = self._stats.get(k, {"ok": 0, "fail": 0})
            cd = self._cooldown.get(k, 0)
            active = "→ " if i == self._current_idx else "  "
            status = "✅" if cd <= now else f"⏳{int(cd - now)}s"
            lines.append(f"{active}{self.short(k)} ok={s['ok']} fail={s['fail']} {status}")
        return "\n".join(lines)

# ── Logging ───────────────────────────────────────────────────────────────────
# Fix Windows encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"[{ts}] {msg.encode('ascii', 'replace').decode()}", flush=True)

# ── Proxy Handler ─────────────────────────────────────────────────────────────
_QUOTA_MARKERS = ("quota", "额度不足", "token quota", "not enough", "rate limit",
                  "too many requests", "usage limit")
_AUTH_MARKERS = ("invalid token", "invalid api key", "authentication")

def _is_quota_error(status: int, body: str) -> bool:
    if status == 429:
        return True
    lower = body.lower()
    if status == 403 and any(m in lower for m in _QUOTA_MARKERS):
        return True
    return False

def _is_auth_error(status: int, body: str) -> bool:
    lower = body.lower()
    return status in (401, 403) and any(m in lower for m in _AUTH_MARKERS)

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    key_mgr: KeyManager = None  # set after init

    def do_POST(self):
        self._proxy_request("POST")

    def do_GET(self):
        self._proxy_request("GET")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def _proxy_request(self, method: str):
        # Đọc body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Thử với key hiện tại, nếu fail thì rotate
        max_tries = min(len(self.key_mgr.keys), 3) if self.key_mgr.keys else 1
        last_status, last_body, last_headers = 500, b"No keys configured", {}

        for attempt in range(max_tries):
            key = self.key_mgr.get_best_key()
            if not key:
                break

            target_url = self.key_mgr.base_url.rstrip("/") + self.path
            try:
                req = Request(target_url, data=body if method == "POST" else None, method=method)
                # Copy headers, replace auth
                for h, v in self.headers.items():
                    h_lower = h.lower()
                    if h_lower in ("host", "x-api-key", "authorization", "anthropic-auth-token"):
                        continue
                    req.add_header(h, v)
                req.add_header("x-api-key", key)
                req.add_header("anthropic-auth-token", key)

                # SSL context (không verify cho proxy)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

                with urlopen(req, timeout=300, context=ctx) as resp:
                    resp_body = resp.read()
                    resp_status = resp.status
                    resp_headers = dict(resp.getheaders())

                self.key_mgr.mark_success(key)
                _log(f"✅ {method} {self.path} → {resp_status} (key {self.key_mgr.short(key)})")

                # Forward response
                self.send_response(resp_status)
                self._send_cors_headers()
                for h, v in resp_headers.items():
                    if h.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(h, v)
                self.end_headers()
                self.wfile.write(resp_body)
                return

            except HTTPError as e:
                resp_body = e.read()
                resp_body_str = resp_body.decode("utf-8", errors="replace")
                last_status = e.code
                last_body = resp_body
                last_headers = dict(e.headers) if e.headers else {}

                if _is_quota_error(e.code, resp_body_str) or _is_auth_error(e.code, resp_body_str):
                    _log(f"❌ Key {self.key_mgr.short(key)} → {e.code}: {resp_body_str[:120]}")
                    self.key_mgr.mark_failed(key)
                    _log(f"🔄 Thử key tiếp... (attempt {attempt + 1}/{max_tries})")
                    continue
                else:
                    # Non-quota error, forward as-is
                    _log(f"⚠️ {method} {self.path} → {e.code} (key {self.key_mgr.short(key)})")
                    self.send_response(e.code)
                    self._send_cors_headers()
                    for h, v in (dict(e.headers) if e.headers else {}).items():
                        if h.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(h, v)
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return

            except Exception as e:
                _log(f"💥 Error: {e}")
                last_status = 502
                last_body = json.dumps({"error": {"type": "proxy_error", "message": str(e)}}).encode()
                continue

        # All keys failed
        _log(f"💀 Tất cả key fail — trả lỗi cuối: {last_status}")
        self.send_response(last_status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(last_body if isinstance(last_body, bytes) else last_body.encode())

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def log_message(self, format, *args):
        pass  # Suppress default logging

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    config = load_config()
    keys = config.get("keys", [])
    base_url = config.get("base_url", "https://vip.digishop.work")

    if not keys:
        print("❌ Không có key trong claude_switcher_config.json!")
        print("   Thêm key vào file config hoặc dùng claude_switcher.pyw")
        input("Enter để thoát...")
        sys.exit(1)

    key_mgr = KeyManager(keys, base_url)
    ProxyHandler.key_mgr = key_mgr

    _log(f"🚀 Claude Digi Proxy starting on http://127.0.0.1:{_PROXY_PORT}")
    _log(f"   Base URL: {base_url}")
    _log(f"   Keys: {len(keys)}")
    for k in keys:
        _log(f"   • {key_mgr.short(k)}")
    _log(f"   Current: {key_mgr.short()}")
    _log("")
    _log("📋 VS Code settings.json cần:")
    _log(f'   "ANTHROPIC_BASE_URL": "http://127.0.0.1:{_PROXY_PORT}"')
    _log(f'   "ANTHROPIC_AUTH_TOKEN": "proxy"')
    _log("")

    server = http.server.HTTPServer(("127.0.0.1", _PROXY_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("⏹ Proxy stopped")
        server.server_close()


if __name__ == "__main__":
    main()
