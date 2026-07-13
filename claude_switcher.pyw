#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
Claude VS Code Switcher v2.0
==============================
Tool độc lập chuyển VS Code Claude Code extension giữa Claude Max ↔ Digi Gateway.

Tính năng:
  - Toggle Max / Digi bằng 1 click
  - Quản lý danh sách Digi API keys (thêm/xóa/check quota)
  - Setup máy mới: tự cài CCS + persist profile
  - Settings: tùy chỉnh base URL, model, auto-restart
  - Portable: copy file này + config.json sang máy khác

Config lưu tại cùng thư mục: claude_switcher_config.json
"""

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "claude_switcher_config.json")
_CLAUDE_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
_CCS_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".ccs")
_CREATE_NO_WINDOW = 0x08000000

# ── Default Config ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "base_url": "https://vip.digishop.work",
    "quota_url": "https://token-quota.digishop.work",
    "model": "claude-opus-4-8",
    "ccs_profile_name": "digiclaude",
    "auto_restart_vscode": False,
    "keys": []
}

def load_config() -> dict:
    """Load config từ file, merge với defaults"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return cfg

def save_config(cfg: dict):
    """Lưu config ra file"""
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

CONFIG = load_config()

# ── Claude Settings helpers ───────────────────────────────────────────────────
def get_current_mode() -> dict:
    """Detect chế độ hiện tại từ ~/.claude/settings.json"""
    try:
        with open(_CLAUDE_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        auth_token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        model = env.get("ANTHROPIC_MODEL", "")
        if base_url and ("digishop" in base_url or base_url == CONFIG["base_url"]):
            short = f"...{auth_token[-6:]}" if auth_token and len(auth_token) > 6 else "N/A"
            return {"mode": "digi", "base_url": base_url, "api_key": auth_token,
                    "key_short": short, "model": model}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return {"mode": "max", "base_url": "", "api_key": "", "key_short": "", "model": ""}

_CLAUDE_CREDENTIALS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
_CLAUDE_CREDENTIALS_BACKUP = _CLAUDE_CREDENTIALS + ".max_backup"

def switch_to_max():
    """Chuyển sang Claude Max — xóa env vars EVERYWHERE + khôi phục credentials"""
    _ANTHROPIC_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")
    # 1. Xóa env vars khỏi settings.json
    try:
        with open(_CLAUDE_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env", {})
        for k in _ANTHROPIC_KEYS:
            env.pop(k, None)
        data["env"] = env
        with open(_CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # 2. Xóa env vars khỏi Windows Registry (User Environment Variables)
    for k in _ANTHROPIC_KEYS:
        delete_user_env(k)
    # 3. Khôi phục credentials.json từ backup (nếu có)
    if os.path.isfile(_CLAUDE_CREDENTIALS_BACKUP) and not os.path.isfile(_CLAUDE_CREDENTIALS):
        os.rename(_CLAUDE_CREDENTIALS_BACKUP, _CLAUDE_CREDENTIALS)

def switch_to_digi(api_key: str):
    """Chuyển sang Digi Gateway — ẩn credentials + ghi CCS profile + persist"""
    # Ẩn credentials.json (tránh extension check OAuth expired → bắt đăng nhập)
    if os.path.isfile(_CLAUDE_CREDENTIALS):
        try:
            os.rename(_CLAUDE_CREDENTIALS, _CLAUDE_CREDENTIALS_BACKUP)
        except OSError:
            pass
    model = CONFIG["model"]
    base_url = CONFIG["base_url"]
    profile_name = CONFIG["ccs_profile_name"]
    # Ghi CCS profile
    profile_path = os.path.join(_CCS_PROFILE_DIR, f"{profile_name}.settings.json")
    profile = {"env": {"ANTHROPIC_MODEL": model, "ANTHROPIC_AUTH_TOKEN": api_key,
                        "ANTHROPIC_BASE_URL": base_url}}
    os.makedirs(_CCS_PROFILE_DIR, exist_ok=True)
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=4, ensure_ascii=False)
    # Persist
    try:
        subprocess.run(["ccs", "persist", profile_name, "--yes"],
                        capture_output=True, text=True, timeout=15,
                        creationflags=_CREATE_NO_WINDOW)
    except Exception:
        _fallback_write_settings(api_key, model, base_url)

def _fallback_write_settings(api_key, model, base_url):
    """Fallback: ghi trực tiếp vào settings.json nếu CCS không có"""
    try:
        with open(_CLAUDE_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    env = data.get("env", {})
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["ANTHROPIC_MODEL"] = model
    data["env"] = env
    os.makedirs(os.path.dirname(_CLAUDE_SETTINGS), exist_ok=True)
    with open(_CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Quota check ───────────────────────────────────────────────────────────────
def check_key_quota(key: str) -> dict:
    result = {"key": key, "short": f"...{key[-6:]}" if len(key) > 6 else key,
              "status": "unknown", "remaining": 0, "total": 0, "used": 0,
              "expires": "", "name": "", "error": ""}
    try:
        req = Request(CONFIG["quota_url"], headers={"Authorization": f"Bearer {key}"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        q = data.get("quota", {})
        result["remaining"] = int(q.get("remaining", 0))
        result["total"] = int(q.get("total", 0))
        result["used"] = int(q.get("used", 0))
        result["name"] = q.get("name", "")
        exp_iso = q.get("expires_at_iso", "")
        if exp_iso:
            exp_dt = datetime.fromisoformat(exp_iso.replace("Z", "+00:00"))
            local_dt = exp_dt.astimezone(timezone(timedelta(hours=7)))
            result["expires"] = local_dt.strftime("%d/%m %H:%M")
        is_expired = q.get("is_expired", False)
        status = q.get("status", "")
        if is_expired or status != "active":
            result["status"] = "expired"
        elif result["remaining"] <= 0:
            result["status"] = "depleted"
        elif result["remaining"] < 100000:
            result["status"] = "low"
        else:
            result["status"] = "active"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:80]
    return result

# ── Setup helpers ─────────────────────────────────────────────────────────────
def check_ccs_installed() -> bool:
    try:
        r = subprocess.run(["ccs", "--version"], capture_output=True, text=True,
                            timeout=10, creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False

def check_claude_code_installed() -> bool:
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                            timeout=10, creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False

def install_ccs() -> tuple[bool, str]:
    try:
        r = subprocess.run(["npm", "install", "-g", "@kaitranntt/ccs"],
                            capture_output=True, text=True, timeout=120,
                            creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)

def restart_vscode():
    subprocess.run(["taskkill", "/F", "/IM", "Code.exe"], capture_output=True,
                    creationflags=_CREATE_NO_WINDOW)
    time.sleep(1.5)
    try:
        subprocess.Popen(["code"], shell=True, creationflags=_CREATE_NO_WINDOW)
    except Exception:
        pass

# ── Colors ────────────────────────────────────────────────────────────────────
BG_DARK = "#0f0f17"
BG_CARD = "#1a1a2e"
FG_PRIMARY = "#e2e8f0"
FG_SECONDARY = "#94a3b8"
FG_DIM = "#64748b"
ACCENT_BLUE = "#3b82f6"
ACCENT_GREEN = "#10b981"
ACCENT_YELLOW = "#f59e0b"
ACCENT_RED = "#ef4444"
ACCENT_PURPLE = "#8b5cf6"
BORDER_COLOR = "#2d2d4a"

# ── GUI ───────────────────────────────────────────────────────────────────────
class ClaudeSwitcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude VS Code Switcher v2")
        self.geometry("540x740")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self._key_results: list[dict] = []
        self._selected_key_idx = tk.IntVar(value=0)
        self._current_page = "main"  # main | settings | setup
        self._build_main_page()
        self._refresh_mode()
        self._refresh_keys_async()

    # ── Header (shared) ──────────────────────────────────────────
    def _build_header(self, parent, title, subtitle, show_settings=True, show_back=False):
        hdr = tk.Frame(parent, bg=BG_DARK, pady=12)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="🤖", font=("Segoe UI Emoji", 24), bg=BG_DARK, fg=FG_PRIMARY).pack(side="left")
        tf = tk.Frame(hdr, bg=BG_DARK)
        tf.pack(side="left", padx=10)
        tk.Label(tf, text=title, font=("Segoe UI", 14, "bold"), bg=BG_DARK, fg=FG_PRIMARY).pack(anchor="w")
        tk.Label(tf, text=subtitle, font=("Segoe UI", 9), bg=BG_DARK, fg=FG_SECONDARY).pack(anchor="w")
        if show_settings:
            tk.Button(hdr, text="⚙️", font=("Segoe UI Emoji", 14), bg=BG_DARK, fg=FG_SECONDARY,
                      relief="flat", cursor="hand2", command=self._show_settings).pack(side="right")
        if show_back:
            tk.Button(hdr, text="← Back", font=("Segoe UI", 10), bg=BG_CARD, fg=FG_SECONDARY,
                      relief="flat", cursor="hand2", padx=8, pady=2, command=self._show_main).pack(side="right")
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", padx=20)

    def _clear_page(self):
        for w in self.winfo_children():
            w.destroy()

    # ══════════════════════════════════════════════════════════════
    # MAIN PAGE
    # ══════════════════════════════════════════════════════════════
    def _build_main_page(self):
        self._clear_page()
        self._current_page = "main"
        self._build_header(self, "Claude VS Code Switcher", "Chuyển đổi Max ↔ Digi Gateway")

        # ── Mode Card ──
        self._mode_card = tk.Frame(self, bg=BG_CARD, pady=14, padx=18,
                                    highlightbackground=BORDER_COLOR, highlightthickness=1)
        self._mode_card.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(self._mode_card, text="CHẾ ĐỘ HIỆN TẠI", font=("Segoe UI", 8, "bold"),
                 bg=BG_CARD, fg=FG_DIM).pack(anchor="w")
        mr = tk.Frame(self._mode_card, bg=BG_CARD)
        mr.pack(fill="x", pady=(6, 0))
        self._mode_icon = tk.Label(mr, text="⚡", font=("Segoe UI Emoji", 22), bg=BG_CARD)
        self._mode_icon.pack(side="left")
        mtf = tk.Frame(mr, bg=BG_CARD)
        mtf.pack(side="left", padx=10)
        self._mode_label = tk.Label(mtf, text="...", font=("Segoe UI", 14, "bold"), bg=BG_CARD, fg=FG_PRIMARY)
        self._mode_label.pack(anchor="w")
        self._mode_detail = tk.Label(mtf, text="...", font=("Segoe UI", 8), bg=BG_CARD, fg=FG_SECONDARY)
        self._mode_detail.pack(anchor="w")

        # ── Toggle Buttons ──
        bf = tk.Frame(self, bg=BG_DARK, pady=10)
        bf.pack(fill="x", padx=20)
        self._btn_max = tk.Button(bf, text="⚡  Claude Max", font=("Segoe UI", 11, "bold"),
                                   bg=ACCENT_BLUE, fg="white", activebackground="#2563eb",
                                   relief="flat", cursor="hand2", padx=14, pady=8,
                                   command=self._on_switch_max)
        self._btn_max.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._btn_digi = tk.Button(bf, text="🌐  Digi Gateway", font=("Segoe UI", 11, "bold"),
                                    bg=ACCENT_PURPLE, fg="white", activebackground="#7c3aed",
                                    relief="flat", cursor="hand2", padx=14, pady=8,
                                    command=self._on_switch_digi)
        self._btn_digi.pack(side="left", expand=True, fill="x", padx=(5, 0))

        # ── Keys Section ──
        kh = tk.Frame(self, bg=BG_DARK)
        kh.pack(fill="x", padx=20, pady=(6, 0))
        tk.Label(kh, text="🔑 DIGI KEYS", font=("Segoe UI", 8, "bold"), bg=BG_DARK, fg=FG_DIM).pack(side="left")
        btn_row = tk.Frame(kh, bg=BG_DARK)
        btn_row.pack(side="right")
        tk.Button(btn_row, text="➕", font=("Segoe UI", 9), bg=BG_CARD, fg=ACCENT_GREEN,
                  relief="flat", cursor="hand2", padx=4, command=self._on_add_key).pack(side="left", padx=2)
        self._refresh_btn = tk.Button(btn_row, text="🔄", font=("Segoe UI", 9), bg=BG_CARD,
                                       fg=FG_SECONDARY, relief="flat", cursor="hand2", padx=4,
                                       command=self._refresh_keys_async)
        self._refresh_btn.pack(side="left", padx=2)

        self._keys_frame = tk.Frame(self, bg=BG_DARK)
        self._keys_frame.pack(fill="both", expand=True, padx=20, pady=(6, 0))
        self._keys_loading = tk.Label(self._keys_frame, text="⏳ Đang kiểm tra keys...",
                                       font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY)
        self._keys_loading.pack(pady=16)

        # ── Bottom ──
        tk.Frame(self, bg=BORDER_COLOR, height=1).pack(fill="x", padx=20, pady=(10, 0))
        bot = tk.Frame(self, bg=BG_DARK, pady=10)
        bot.pack(fill="x", padx=20)
        tk.Button(bot, text="🔧 Setup Máy Mới", font=("Segoe UI", 9), bg="#374151", fg=FG_SECONDARY,
                  relief="flat", cursor="hand2", padx=10, pady=4, command=self._show_setup).pack(side="left")
        tk.Button(bot, text="⚡ Restart VS Code", font=("Segoe UI", 9), bg="#374151", fg=FG_SECONDARY,
                  relief="flat", cursor="hand2", padx=10, pady=4, command=self._on_restart).pack(side="right")
        self._status_label = tk.Label(bot, text="", font=("Segoe UI", 8), bg=BG_DARK, fg=FG_DIM)
        self._status_label.pack(side="right", padx=8)

    def _refresh_mode(self):
        info = get_current_mode()
        if info["mode"] == "max":
            self._mode_icon.config(text="⚡", fg=ACCENT_BLUE)
            self._mode_label.config(text="Claude Max", fg=ACCENT_BLUE)
            self._mode_detail.config(text="OAuth session · Dùng subscription Max")
            self._mode_card.config(highlightbackground=ACCENT_BLUE)
            self._btn_max.config(bg="#1e3a5f", state="disabled")
            self._btn_digi.config(bg=ACCENT_PURPLE, state="normal")
        else:
            self._mode_icon.config(text="🌐", fg=ACCENT_PURPLE)
            self._mode_label.config(text="Digi Gateway", fg=ACCENT_PURPLE)
            self._mode_detail.config(text=f"Key {info['key_short']} · {info.get('model','')} · {info['base_url']}")
            self._mode_card.config(highlightbackground=ACCENT_PURPLE)
            self._btn_max.config(bg=ACCENT_BLUE, state="normal")
            self._btn_digi.config(bg="#3b2069", state="disabled")

    def _refresh_keys_async(self):
        self._refresh_btn.config(state="disabled", text="⏳")
        for w in self._keys_frame.winfo_children():
            w.destroy()
        tk.Label(self._keys_frame, text="⏳ Đang kiểm tra keys...",
                 font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY).pack(pady=16)
        def _work():
            results = [check_key_quota(k) for k in CONFIG["keys"]]
            self.after(0, lambda: self._render_keys(results))
        threading.Thread(target=_work, daemon=True).start()

    def _render_keys(self, results):
        self._key_results = results
        self._refresh_btn.config(state="normal", text="🔄")
        for w in self._keys_frame.winfo_children():
            w.destroy()
        if not results:
            tk.Label(self._keys_frame, text="Chưa có key · Bấm ➕ để thêm",
                     font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY).pack(pady=16)
            return
        best_idx, best_rem = 0, -1
        for i, r in enumerate(results):
            if r["status"] == "active" and r["remaining"] > best_rem:
                best_rem = r["remaining"]
                best_idx = i
        self._selected_key_idx.set(best_idx)
        for i, r in enumerate(results):
            self._render_key_card(i, r)

    def _render_key_card(self, idx, r):
        card = tk.Frame(self._keys_frame, bg=BG_CARD, pady=8, padx=12,
                        highlightthickness=1, highlightbackground=BORDER_COLOR)
        card.pack(fill="x", pady=2)
        top = tk.Frame(card, bg=BG_CARD)
        top.pack(fill="x")
        tk.Radiobutton(top, variable=self._selected_key_idx, value=idx,
                        bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_DARK,
                        activebackground=BG_CARD).pack(side="left")
        icons = {"active": ("✅", ACCENT_GREEN), "low": ("⚠️", ACCENT_YELLOW),
                 "depleted": ("❌", ACCENT_RED), "expired": ("💀", FG_DIM),
                 "error": ("❓", ACCENT_RED), "unknown": ("⏳", FG_DIM)}
        icon, color = icons.get(r["status"], ("❓", FG_DIM))
        tk.Label(top, text=icon, font=("Segoe UI Emoji", 11), bg=BG_CARD).pack(side="left", padx=(0, 4))
        tk.Label(top, text=r["short"], font=("Consolas", 10, "bold"), bg=BG_CARD, fg=FG_PRIMARY).pack(side="left")
        # Delete button
        tk.Button(top, text="✕", font=("Segoe UI", 8), bg=BG_CARD, fg=ACCENT_RED,
                  relief="flat", cursor="hand2", command=lambda i=idx: self._on_delete_key(i)).pack(side="right")
        # Remaining
        if r["total"] > 0:
            rm = r["remaining"] / 1_000_000
            tm = r["total"] / 1_000_000
            pct = r["remaining"] / r["total"] * 100
            tk.Label(top, text=f"{rm:.1f}M / {tm:.1f}M ({pct:.0f}%)", font=("Segoe UI", 8),
                     bg=BG_CARD, fg=color).pack(side="right", padx=4)
        elif r["error"]:
            tk.Label(top, text=r["error"][:30], font=("Segoe UI", 8), bg=BG_CARD, fg=ACCENT_RED).pack(side="right")
        # Bottom info
        bot = tk.Frame(card, bg=BG_CARD)
        bot.pack(fill="x", pady=(3, 0))
        if r["name"]:
            tk.Label(bot, text=r["name"], font=("Segoe UI", 7), bg=BG_CARD, fg=FG_DIM).pack(side="left", padx=(24, 0))
        if r["expires"]:
            tk.Label(bot, text=f"⏰ {r['expires']}", font=("Segoe UI", 7), bg=BG_CARD, fg=FG_DIM).pack(side="right")
        # Progress bar
        if r["total"] > 0:
            bar = tk.Frame(card, bg="#1e1e36", height=3)
            bar.pack(fill="x", pady=(4, 0), padx=(24, 0))
            bar.pack_propagate(False)
            pct = max(r["remaining"] / r["total"], 0.005)
            bc = ACCENT_GREEN if pct > 0.3 else (ACCENT_YELLOW if pct > 0.05 else ACCENT_RED)
            tk.Frame(bar, bg=bc, height=3).place(relwidth=pct, relheight=1)

    # ── Actions ──────────────────────────────────────────────────
    def _on_switch_max(self):
        switch_to_max()
        self._refresh_mode()
        self._set_status("✅ Đã chuyển Max — restart VS Code để áp dụng", ACCENT_GREEN)
        if CONFIG.get("auto_restart_vscode"):
            self._on_restart()

    def _on_switch_digi(self):
        idx = self._selected_key_idx.get()
        if not self._key_results:
            messagebox.showwarning("Chưa có key", "Đang load keys, vui lòng đợi...")
            return
        if idx >= len(self._key_results):
            idx = 0
        r = self._key_results[idx]
        switch_to_digi(r["key"])
        self._refresh_mode()
        self._set_status(f"✅ Đã chuyển Digi ({r['short']}) — restart VS Code", ACCENT_GREEN)
        if CONFIG.get("auto_restart_vscode"):
            self._on_restart()

    def _on_add_key(self):
        key = simpledialog.askstring("Thêm Key", "Nhập Digi API key:", parent=self)
        if key and key.strip():
            key = key.strip()
            if key not in CONFIG["keys"]:
                CONFIG["keys"].append(key)
                save_config(CONFIG)
                self._refresh_keys_async()
                self._set_status(f"✅ Đã thêm key ...{key[-6:]}", ACCENT_GREEN)
            else:
                self._set_status("Key đã tồn tại", ACCENT_YELLOW)

    def _on_delete_key(self, idx):
        if idx < len(CONFIG["keys"]):
            key = CONFIG["keys"][idx]
            short = f"...{key[-6:]}"
            if messagebox.askyesno("Xóa key", f"Xóa key {short}?"):
                CONFIG["keys"].pop(idx)
                save_config(CONFIG)
                self._refresh_keys_async()

    def _on_restart(self):
        self._set_status("⏳ Đang restart VS Code...", ACCENT_YELLOW)
        def _do():
            restart_vscode()
            self.after(0, lambda: self._set_status("✅ VS Code đã restart", ACCENT_GREEN))
        threading.Thread(target=_do, daemon=True).start()

    def _set_status(self, text, color=FG_DIM):
        if hasattr(self, '_status_label'):
            self._status_label.config(text=text, fg=color)

    # ══════════════════════════════════════════════════════════════
    # SETTINGS PAGE
    # ══════════════════════════════════════════════════════════════
    def _show_settings(self):
        self._clear_page()
        self._current_page = "settings"
        self._build_header(self, "Settings", "Tùy chỉnh cấu hình", show_settings=False, show_back=True)

        container = tk.Frame(self, bg=BG_DARK, padx=20, pady=10)
        container.pack(fill="both", expand=True)

        fields = [
            ("Base URL", "base_url", CONFIG["base_url"]),
            ("Quota URL", "quota_url", CONFIG["quota_url"]),
            ("Model", "model", CONFIG["model"]),
            ("CCS Profile Name", "ccs_profile_name", CONFIG["ccs_profile_name"]),
        ]
        self._setting_entries = {}
        for label, key, val in fields:
            f = tk.Frame(container, bg=BG_DARK, pady=6)
            f.pack(fill="x")
            tk.Label(f, text=label, font=("Segoe UI", 10, "bold"), bg=BG_DARK, fg=FG_PRIMARY).pack(anchor="w")
            e = tk.Entry(f, font=("Consolas", 10), bg=BG_CARD, fg=FG_PRIMARY,
                         insertbackground=FG_PRIMARY, relief="flat", bd=0)
            e.pack(fill="x", pady=(4, 0), ipady=6, ipadx=8)
            e.insert(0, val)
            self._setting_entries[key] = e

        # Auto restart toggle
        f_auto = tk.Frame(container, bg=BG_DARK, pady=6)
        f_auto.pack(fill="x")
        self._auto_restart_var = tk.BooleanVar(value=CONFIG.get("auto_restart_vscode", False))
        tk.Checkbutton(f_auto, text="Tự động restart VS Code sau khi chuyển mode",
                        font=("Segoe UI", 10), bg=BG_DARK, fg=FG_PRIMARY, selectcolor=BG_CARD,
                        activebackground=BG_DARK, activeforeground=FG_PRIMARY,
                        variable=self._auto_restart_var).pack(anchor="w")

        # VIP Models reference
        f_models = tk.Frame(container, bg=BG_CARD, pady=10, padx=12,
                            highlightthickness=1, highlightbackground=BORDER_COLOR)
        f_models.pack(fill="x", pady=(10, 0))
        tk.Label(f_models, text="📋 VIP Models hỗ trợ:", font=("Segoe UI", 9, "bold"),
                 bg=BG_CARD, fg=FG_SECONDARY).pack(anchor="w")
        models_text = ("claude-opus-4-8, claude-opus-4-7-max, claude-opus-4-7, claude-opus-4-6-max, "
                       "claude-opus-4-6, claude-sonnet-4-6, claude-sonnet-4-6-thinking, "
                       "claude-haiku-4-5, claude-opus-4-5-20251101")
        tk.Label(f_models, text=models_text, font=("Consolas", 8), bg=BG_CARD, fg=FG_DIM,
                 wraplength=460, justify="left").pack(anchor="w", pady=(4, 0))

        # Save button
        tk.Button(container, text="💾  Lưu Settings", font=("Segoe UI", 12, "bold"),
                  bg=ACCENT_GREEN, fg="white", relief="flat", cursor="hand2",
                  padx=20, pady=10, command=self._save_settings).pack(fill="x", pady=(16, 0))

    def _save_settings(self):
        for key, entry in self._setting_entries.items():
            CONFIG[key] = entry.get().strip()
        CONFIG["auto_restart_vscode"] = self._auto_restart_var.get()
        save_config(CONFIG)
        messagebox.showinfo("Saved", "Settings đã lưu!")
        self._show_main()

    # ══════════════════════════════════════════════════════════════
    # SETUP PAGE
    # ══════════════════════════════════════════════════════════════
    def _show_setup(self):
        self._clear_page()
        self._current_page = "setup"
        self._build_header(self, "Setup Máy Mới", "Cài đặt CCS + persist profile", show_settings=False, show_back=True)

        container = tk.Frame(self, bg=BG_DARK, padx=20, pady=10)
        container.pack(fill="both", expand=True)

        # Status checks
        self._setup_checks = tk.Frame(container, bg=BG_DARK)
        self._setup_checks.pack(fill="x")
        tk.Label(self._setup_checks, text="⏳ Đang kiểm tra...", font=("Segoe UI", 10),
                 bg=BG_DARK, fg=FG_SECONDARY).pack(anchor="w")

        # Buttons
        self._setup_btns = tk.Frame(container, bg=BG_DARK, pady=10)
        self._setup_btns.pack(fill="x")
        tk.Button(self._setup_btns, text="📦 Cài CCS", font=("Segoe UI", 11, "bold"),
                  bg=ACCENT_BLUE, fg="white", relief="flat", cursor="hand2",
                  padx=16, pady=8, command=self._on_install_ccs).pack(fill="x", pady=3)
        tk.Button(self._setup_btns, text="🔗 Persist Profile Digi", font=("Segoe UI", 11, "bold"),
                  bg=ACCENT_PURPLE, fg="white", relief="flat", cursor="hand2",
                  padx=16, pady=8, command=self._on_persist).pack(fill="x", pady=3)
        tk.Button(self._setup_btns, text="🚀 Setup All (CCS + Persist + Key)", font=("Segoe UI", 11, "bold"),
                  bg=ACCENT_GREEN, fg="white", relief="flat", cursor="hand2",
                  padx=16, pady=8, command=self._on_setup_all).pack(fill="x", pady=3)

        # Log area
        tk.Label(container, text="📋 Log", font=("Segoe UI", 9, "bold"),
                 bg=BG_DARK, fg=FG_DIM).pack(anchor="w", pady=(10, 4))
        self._setup_log = tk.Text(container, font=("Consolas", 9), bg=BG_CARD, fg=FG_SECONDARY,
                                   relief="flat", height=12, wrap="word", insertbackground=FG_PRIMARY)
        self._setup_log.pack(fill="both", expand=True)

        # Run checks
        threading.Thread(target=self._run_setup_checks, daemon=True).start()

    def _run_setup_checks(self):
        checks = []
        checks.append(("Node.js", subprocess.run(["node", "--version"], capture_output=True,
                        creationflags=_CREATE_NO_WINDOW).returncode == 0
                        if not self._safe_check("node") else False))
        checks.append(("npm", self._safe_check("npm")))
        checks.append(("CCS", check_ccs_installed()))
        checks.append(("Claude Code CLI", check_claude_code_installed()))
        checks.append(("~/.claude/settings.json", os.path.isfile(_CLAUDE_SETTINGS)))
        checks.append(("Keys configured", len(CONFIG["keys"]) > 0))
        self.after(0, lambda: self._render_checks(checks))

    def _safe_check(self, cmd):
        try:
            return subprocess.run([cmd, "--version"], capture_output=True,
                                   creationflags=_CREATE_NO_WINDOW, timeout=10).returncode == 0
        except Exception:
            return False

    def _render_checks(self, checks):
        for w in self._setup_checks.winfo_children():
            w.destroy()
        for name, ok in checks:
            icon = "✅" if ok else "❌"
            color = ACCENT_GREEN if ok else ACCENT_RED
            tk.Label(self._setup_checks, text=f"  {icon}  {name}", font=("Segoe UI", 10),
                     bg=BG_DARK, fg=color).pack(anchor="w", pady=1)

    def _log_setup(self, msg):
        self._setup_log.insert("end", msg + "\n")
        self._setup_log.see("end")

    def _on_install_ccs(self):
        self._log_setup("📦 Đang cài CCS...")
        def _do():
            ok, out = install_ccs()
            self.after(0, lambda: self._log_setup(f"{'✅ CCS đã cài!' if ok else '❌ Lỗi:'}\n{out[:300]}"))
            self.after(0, lambda: threading.Thread(target=self._run_setup_checks, daemon=True).start())
        threading.Thread(target=_do, daemon=True).start()

    def _on_persist(self):
        if not CONFIG["keys"]:
            messagebox.showwarning("Chưa có key", "Thêm key trong trang chính trước!")
            return
        key = CONFIG["keys"][0]  # Dùng key đầu tiên
        self._log_setup(f"🔗 Persist profile với key ...{key[-6:]}...")
        def _do():
            switch_to_digi(key)
            self.after(0, lambda: self._log_setup("✅ Profile đã persist! Restart VS Code để áp dụng."))
            self.after(0, lambda: threading.Thread(target=self._run_setup_checks, daemon=True).start())
        threading.Thread(target=_do, daemon=True).start()

    def _on_setup_all(self):
        if not CONFIG["keys"]:
            key = simpledialog.askstring("Nhập Key", "Nhập Digi API key:", parent=self)
            if not key or not key.strip():
                return
            key = key.strip()
            CONFIG["keys"].append(key)
            save_config(CONFIG)
        self._log_setup("🚀 Bắt đầu setup all...")
        def _do():
            # 1. Install CCS
            if not check_ccs_installed():
                self.after(0, lambda: self._log_setup("📦 Cài CCS..."))
                ok, out = install_ccs()
                self.after(0, lambda: self._log_setup(f"{'✅ CCS OK' if ok else '❌ CCS fail'}: {out[:200]}"))
                if not ok:
                    return
            else:
                self.after(0, lambda: self._log_setup("✅ CCS đã có"))
            # 2. Persist profile
            key = CONFIG["keys"][0]
            self.after(0, lambda: self._log_setup(f"🔗 Persist key ...{key[-6:]}..."))
            switch_to_digi(key)
            self.after(0, lambda: self._log_setup("✅ Profile persisted!"))
            # 3. Check
            self.after(0, lambda: self._log_setup("\n🎉 Setup xong! Restart VS Code để dùng."))
            self.after(0, lambda: threading.Thread(target=self._run_setup_checks, daemon=True).start())
        threading.Thread(target=_do, daemon=True).start()

    # ── Navigation ───────────────────────────────────────────────
    def _show_main(self):
        self._build_main_page()
        self._refresh_mode()
        self._refresh_keys_async()


if __name__ == "__main__":
    app = ClaudeSwitcher()
    app.mainloop()
