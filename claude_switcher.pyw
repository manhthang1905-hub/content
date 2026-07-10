#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
Claude VS Code Switcher
========================
GUI tool để chuyển VS Code Claude extension giữa Claude Max (OAuth)
và Digi Gateway (API key).

Cách hoạt động:
  - Max mode:  Xóa ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY khỏi User Env
               → Claude extension dùng OAuth session (Max subscription)
  - Digi mode: Set ANTHROPIC_BASE_URL=https://vip.digishop.work
               + ANTHROPIC_API_KEY=<key> vào User Env
               → Claude extension đi qua Digi gateway

Sau khi chuyển mode, cần restart VS Code để nhận env vars mới.
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
from tkinter import messagebox, ttk
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Config ────────────────────────────────────────────────────────────────────
DIGI_VIP_URL = "https://vip.digishop.work"
QUOTA_URL = "https://token-quota.digishop.work"

# Đọc keys từ config/.env hoặc hardcode
def _load_keys_from_env():
    """Đọc CLAUDE_BACKUP_KEYS từ config/.env"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", ".env")
    keys = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CLAUDE_BACKUP_KEYS="):
                    raw = line.split("=", 1)[1].strip()
                    keys = [k.strip() for k in raw.split(",") if k.strip()]
                    break
    return keys

DIGI_KEYS = _load_keys_from_env()

# ── Windows Registry helpers ──────────────────────────────────────────────────
import winreg

_ENV_KEY_PATH = r"Environment"
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A

def _broadcast_env_change():
    """Thông báo cho Windows rằng env vars đã thay đổi"""
    ctypes.windll.user32.SendMessageTimeoutW(
        _HWND_BROADCAST, _WM_SETTINGCHANGE, 0,
        "Environment", 0x0002, 5000, ctypes.byref(ctypes.wintypes.DWORD())
    )

def get_user_env(name: str) -> str | None:
    """Đọc User Environment Variable"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None

def set_user_env(name: str, value: str):
    """Set User Environment Variable"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _broadcast_env_change()

def delete_user_env(name: str):
    """Xóa User Environment Variable"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        _broadcast_env_change()
    except FileNotFoundError:
        pass

# ── Mode detection ────────────────────────────────────────────────────────────
def get_current_mode() -> dict:
    """Detect chế độ hiện tại từ User Env"""
    base_url = get_user_env("ANTHROPIC_BASE_URL")
    api_key = get_user_env("ANTHROPIC_API_KEY")
    if base_url and "digishop" in base_url:
        short = f"...{api_key[-6:]}" if api_key and len(api_key) > 6 else "N/A"
        return {"mode": "digi", "base_url": base_url, "api_key": api_key, "key_short": short}
    return {"mode": "max", "base_url": base_url, "api_key": api_key, "key_short": ""}

def switch_to_max():
    """Chuyển sang Claude Max — xóa env vars"""
    delete_user_env("ANTHROPIC_BASE_URL")
    delete_user_env("ANTHROPIC_API_KEY")

def switch_to_digi(api_key: str):
    """Chuyển sang Digi Gateway — set env vars"""
    set_user_env("ANTHROPIC_BASE_URL", DIGI_VIP_URL)
    set_user_env("ANTHROPIC_API_KEY", api_key)

# ── Quota check ───────────────────────────────────────────────────────────────
def check_key_quota(key: str) -> dict:
    """Check quota cho 1 key. Trả dict với status, remaining, etc."""
    result = {"key": key, "short": f"...{key[-6:]}" if len(key) > 6 else key,
              "status": "unknown", "remaining": 0, "total": 0, "used": 0,
              "expires": "", "name": "", "error": ""}
    try:
        req = Request(QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
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

def check_all_keys() -> list[dict]:
    """Check quota tất cả keys"""
    results = []
    for key in DIGI_KEYS:
        results.append(check_key_quota(key))
    return results

# ── VS Code restart ──────────────────────────────────────────────────────────
def restart_vscode():
    """Kill VS Code rồi mở lại"""
    subprocess.run(["taskkill", "/F", "/IM", "Code.exe"], capture_output=True)
    time.sleep(1)
    # Mở VS Code (thường nằm trong PATH)
    try:
        subprocess.Popen(["code"], shell=True, creationflags=0x00000008)
    except Exception:
        pass

# ── GUI ───────────────────────────────────────────────────────────────────────
# Colors
BG_DARK = "#0f0f17"
BG_CARD = "#1a1a2e"
BG_CARD_HOVER = "#252540"
FG_PRIMARY = "#e2e8f0"
FG_SECONDARY = "#94a3b8"
FG_DIM = "#64748b"
ACCENT_BLUE = "#3b82f6"
ACCENT_GREEN = "#10b981"
ACCENT_YELLOW = "#f59e0b"
ACCENT_RED = "#ef4444"
ACCENT_PURPLE = "#8b5cf6"
BORDER_COLOR = "#2d2d4a"

class ClaudeSwitcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude VS Code Switcher")
        self.geometry("520x680")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        
        # Icon
        try:
            self.iconbitmap(default="")
        except Exception:
            pass
        
        self._key_results: list[dict] = []
        self._selected_key_idx = tk.IntVar(value=0)
        
        self._build_ui()
        self._refresh_mode()
        self._refresh_keys_async()
    
    def _build_ui(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=BG_DARK, pady=16)
        hdr.pack(fill="x", padx=20)
        
        tk.Label(hdr, text="🤖", font=("Segoe UI Emoji", 28), bg=BG_DARK, fg=FG_PRIMARY).pack(side="left")
        title_frame = tk.Frame(hdr, bg=BG_DARK)
        title_frame.pack(side="left", padx=12)
        tk.Label(title_frame, text="Claude VS Code Switcher", font=("Segoe UI", 16, "bold"),
                 bg=BG_DARK, fg=FG_PRIMARY).pack(anchor="w")
        tk.Label(title_frame, text="Chuyển đổi Max ↔ Digi Gateway", font=("Segoe UI", 10),
                 bg=BG_DARK, fg=FG_SECONDARY).pack(anchor="w")
        
        # ── Separator ──
        tk.Frame(self, bg=BORDER_COLOR, height=1).pack(fill="x", padx=20)
        
        # ── Current Mode Card ──
        self._mode_card = tk.Frame(self, bg=BG_CARD, pady=16, padx=20,
                                    highlightbackground=BORDER_COLOR, highlightthickness=1)
        self._mode_card.pack(fill="x", padx=20, pady=(16, 0))
        
        tk.Label(self._mode_card, text="CHẾ ĐỘ HIỆN TẠI", font=("Segoe UI", 9, "bold"),
                 bg=BG_CARD, fg=FG_DIM).pack(anchor="w")
        
        mode_row = tk.Frame(self._mode_card, bg=BG_CARD)
        mode_row.pack(fill="x", pady=(8, 0))
        
        self._mode_icon = tk.Label(mode_row, text="⚡", font=("Segoe UI Emoji", 24), bg=BG_CARD)
        self._mode_icon.pack(side="left")
        
        mode_text_frame = tk.Frame(mode_row, bg=BG_CARD)
        mode_text_frame.pack(side="left", padx=12)
        
        self._mode_label = tk.Label(mode_text_frame, text="...", font=("Segoe UI", 15, "bold"),
                                     bg=BG_CARD, fg=FG_PRIMARY)
        self._mode_label.pack(anchor="w")
        
        self._mode_detail = tk.Label(mode_text_frame, text="...", font=("Segoe UI", 9),
                                      bg=BG_CARD, fg=FG_SECONDARY)
        self._mode_detail.pack(anchor="w")
        
        # ── Toggle Buttons ──
        btn_frame = tk.Frame(self, bg=BG_DARK, pady=12)
        btn_frame.pack(fill="x", padx=20)
        
        self._btn_max = tk.Button(btn_frame, text="⚡  Claude Max", font=("Segoe UI", 11, "bold"),
                                   bg=ACCENT_BLUE, fg="white", activebackground="#2563eb",
                                   activeforeground="white", relief="flat", cursor="hand2",
                                   padx=16, pady=10, command=self._on_switch_max)
        self._btn_max.pack(side="left", expand=True, fill="x", padx=(0, 6))
        
        self._btn_digi = tk.Button(btn_frame, text="🌐  Digi Gateway", font=("Segoe UI", 11, "bold"),
                                    bg=ACCENT_PURPLE, fg="white", activebackground="#7c3aed",
                                    activeforeground="white", relief="flat", cursor="hand2",
                                    padx=16, pady=10, command=self._on_switch_digi)
        self._btn_digi.pack(side="left", expand=True, fill="x", padx=(6, 0))
        
        # ── Digi Keys Section ──
        keys_header = tk.Frame(self, bg=BG_DARK)
        keys_header.pack(fill="x", padx=20, pady=(8, 0))
        
        tk.Label(keys_header, text="🔑 DIGI KEYS", font=("Segoe UI", 9, "bold"),
                 bg=BG_DARK, fg=FG_DIM).pack(side="left")
        
        self._refresh_btn = tk.Button(keys_header, text="🔄 Refresh", font=("Segoe UI", 9),
                                       bg=BG_CARD, fg=FG_SECONDARY, relief="flat", cursor="hand2",
                                       padx=8, pady=2, command=self._refresh_keys_async)
        self._refresh_btn.pack(side="right")
        
        # Keys list container
        self._keys_frame = tk.Frame(self, bg=BG_DARK)
        self._keys_frame.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        
        self._keys_loading = tk.Label(self._keys_frame, text="⏳ Đang kiểm tra keys...",
                                       font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY)
        self._keys_loading.pack(pady=20)
        
        # ── Bottom Buttons ──
        tk.Frame(self, bg=BORDER_COLOR, height=1).pack(fill="x", padx=20, pady=(12, 0))
        
        bottom = tk.Frame(self, bg=BG_DARK, pady=12)
        bottom.pack(fill="x", padx=20)
        
        self._restart_btn = tk.Button(bottom, text="⚡ Restart VS Code", font=("Segoe UI", 10),
                                       bg="#374151", fg=FG_SECONDARY, relief="flat", cursor="hand2",
                                       padx=12, pady=6, command=self._on_restart_vscode)
        self._restart_btn.pack(side="right")
        
        self._status_label = tk.Label(bottom, text="", font=("Segoe UI", 9),
                                       bg=BG_DARK, fg=FG_DIM)
        self._status_label.pack(side="left")
    
    def _refresh_mode(self):
        """Cập nhật UI theo mode hiện tại"""
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
            self._mode_label.config(text=f"Digi Gateway", fg=ACCENT_PURPLE)
            self._mode_detail.config(text=f"Key {info['key_short']} · {info['base_url']}")
            self._mode_card.config(highlightbackground=ACCENT_PURPLE)
            self._btn_max.config(bg=ACCENT_BLUE, state="normal")
            self._btn_digi.config(bg="#3b2069", state="disabled")
    
    def _refresh_keys_async(self):
        """Check quota tất cả keys (async)"""
        self._refresh_btn.config(state="disabled", text="⏳ Checking...")
        for w in self._keys_frame.winfo_children():
            w.destroy()
        self._keys_loading = tk.Label(self._keys_frame, text="⏳ Đang kiểm tra keys...",
                                       font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY)
        self._keys_loading.pack(pady=20)
        
        def _worker():
            results = check_all_keys()
            self.after(0, lambda: self._render_keys(results))
        
        threading.Thread(target=_worker, daemon=True).start()
    
    def _render_keys(self, results: list[dict]):
        """Render key cards"""
        self._key_results = results
        self._refresh_btn.config(state="normal", text="🔄 Refresh")
        
        for w in self._keys_frame.winfo_children():
            w.destroy()
        
        if not results:
            tk.Label(self._keys_frame, text="Không có key nào trong config/.env",
                     font=("Segoe UI", 10), bg=BG_DARK, fg=FG_SECONDARY).pack(pady=20)
            return
        
        # Auto-select best key
        best_idx = 0
        best_remaining = -1
        for i, r in enumerate(results):
            if r["status"] == "active" and r["remaining"] > best_remaining:
                best_remaining = r["remaining"]
                best_idx = i
        self._selected_key_idx.set(best_idx)
        
        for i, r in enumerate(results):
            self._render_key_card(i, r)
    
    def _render_key_card(self, idx: int, r: dict):
        """Render 1 key card"""
        card = tk.Frame(self._keys_frame, bg=BG_CARD, pady=10, padx=14,
                        highlightthickness=1, highlightbackground=BORDER_COLOR)
        card.pack(fill="x", pady=3)
        
        # Radio button + key info
        top_row = tk.Frame(card, bg=BG_CARD)
        top_row.pack(fill="x")
        
        rb = tk.Radiobutton(top_row, variable=self._selected_key_idx, value=idx,
                            bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_DARK,
                            activebackground=BG_CARD, activeforeground=FG_PRIMARY)
        rb.pack(side="left")
        
        # Status icon
        status_icons = {"active": ("✅", ACCENT_GREEN), "low": ("⚠️", ACCENT_YELLOW),
                        "depleted": ("❌", ACCENT_RED), "expired": ("💀", FG_DIM),
                        "error": ("❓", ACCENT_RED), "unknown": ("⏳", FG_DIM)}
        icon, color = status_icons.get(r["status"], ("❓", FG_DIM))
        
        tk.Label(top_row, text=icon, font=("Segoe UI Emoji", 12), bg=BG_CARD).pack(side="left", padx=(0, 6))
        tk.Label(top_row, text=r["short"], font=("Consolas", 11, "bold"),
                 bg=BG_CARD, fg=FG_PRIMARY).pack(side="left")
        
        # Remaining tokens
        if r["status"] in ("active", "low", "depleted"):
            remaining_m = r["remaining"] / 1_000_000
            total_m = r["total"] / 1_000_000
            pct = (r["remaining"] / r["total"] * 100) if r["total"] > 0 else 0
            remaining_text = f"{remaining_m:.1f}M / {total_m:.1f}M ({pct:.0f}%)"
            tk.Label(top_row, text=remaining_text, font=("Segoe UI", 9),
                     bg=BG_CARD, fg=color).pack(side="right")
        elif r["status"] == "error":
            tk.Label(top_row, text="Lỗi kết nối", font=("Segoe UI", 9),
                     bg=BG_CARD, fg=ACCENT_RED).pack(side="right")
        elif r["status"] == "expired":
            tk.Label(top_row, text="Hết hạn", font=("Segoe UI", 9),
                     bg=BG_CARD, fg=FG_DIM).pack(side="right")
        
        # Bottom row: sub name + expiry
        bottom_row = tk.Frame(card, bg=BG_CARD)
        bottom_row.pack(fill="x", pady=(4, 0))
        
        if r["name"]:
            tk.Label(bottom_row, text=r["name"], font=("Segoe UI", 8),
                     bg=BG_CARD, fg=FG_DIM).pack(side="left", padx=(26, 0))
        if r["expires"]:
            tk.Label(bottom_row, text=f"⏰ {r['expires']}", font=("Segoe UI", 8),
                     bg=BG_CARD, fg=FG_DIM).pack(side="right")
        
        # Progress bar
        if r["total"] > 0:
            bar_frame = tk.Frame(card, bg="#1e1e36", height=4)
            bar_frame.pack(fill="x", pady=(6, 0), padx=(26, 0))
            bar_frame.pack_propagate(False)
            pct = r["remaining"] / r["total"]
            bar_color = ACCENT_GREEN if pct > 0.3 else (ACCENT_YELLOW if pct > 0.05 else ACCENT_RED)
            fill = tk.Frame(bar_frame, bg=bar_color, height=4)
            fill.place(relwidth=max(pct, 0.005), relheight=1)
    
    def _on_switch_max(self):
        switch_to_max()
        self._refresh_mode()
        self._status_label.config(text="✅ Đã chuyển sang Max — restart VS Code để áp dụng",
                                   fg=ACCENT_GREEN)
    
    def _on_switch_digi(self):
        idx = self._selected_key_idx.get()
        if not self._key_results:
            messagebox.showwarning("Chưa có key", "Đang load keys, vui lòng đợi...")
            return
        if idx >= len(self._key_results):
            idx = 0
        r = self._key_results[idx]
        if r["status"] in ("expired", "error"):
            if not messagebox.askyesno("Key có vấn đề",
                                        f"Key {r['short']} đang {r['status']}. Vẫn muốn dùng?"):
                return
        switch_to_digi(r["key"])
        self._refresh_mode()
        self._status_label.config(text=f"✅ Đã chuyển sang Digi ({r['short']}) — restart VS Code",
                                   fg=ACCENT_GREEN)
    
    def _on_restart_vscode(self):
        if messagebox.askyesno("Restart VS Code", "Sẽ đóng tất cả VS Code windows. Tiếp tục?"):
            self._status_label.config(text="⏳ Đang restart VS Code...", fg=ACCENT_YELLOW)
            def _do():
                restart_vscode()
                self.after(0, lambda: self._status_label.config(
                    text="✅ VS Code đã restart", fg=ACCENT_GREEN))
            threading.Thread(target=_do, daemon=True).start()


if __name__ == "__main__":
    app = ClaudeSwitcher()
    app.mainloop()
