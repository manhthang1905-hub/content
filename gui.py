"""
gui.py — giao diện chạy CONTENT theo Google Sheet.

- Chọn topic active
- Sync danh sách job chưa viết
- AUTO chạy tuần tự và ghi kết quả lên Sheet
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.dont_write_bytecode = True
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CORE = ROOT / "core"
TOPICS = ROOT / "topics"
sys.path.insert(0, str(CORE))


def load_env() -> None:
    env_path = ROOT / "config" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ[key.strip()] = val.strip()  # overwrite để đảm bảo key luôn đúng


load_env()  # phải chạy trước khi import api (api.py đọc key lúc module load)

import yaml  # noqa: E402

import api as api_mod  # noqa: E402
import pipeline  # noqa: E402
import sheets  # noqa: E402

TH = {
    "bg": "#0d0f14",
    "surface": "#131720",
    "card": "#1a1f2e",
    "border": "#2a3045",
    "accent": "#3b82f6",
    "green": "#22c55e",
    "yellow": "#ca8a04",
    "red": "#ef4444",
    "text": "#f1f5f9",
    "sub": "#94a3b8",
    "muted": "#64748b",
    "crust": "#080a0e",
    "overlay": "#222840",
}

PIPELINE_STEPS = [
    ("fetch", "Fetch"),
    ("title", "Title"),
    ("write", "Write"),
    ("check", "Check"),
    ("seo", "SEO"),
    ("sheet", "Sheet"),
]

AUTO_CYCLE_MINUTES = int(os.environ.get("CONTENT_AUTO_CYCLE_MINUTES", "30"))
AUTO_START_DELAY_SEC = int(os.environ.get("CONTENT_AUTOSTART_DELAY_SEC", "3"))
AUTO_START_ENABLED = os.environ.get("CONTENT_NO_AUTOSTART", "").strip().lower() not in {"1", "true", "yes"}
DEFAULT_WORKERS = max(1, min(4, int(os.environ.get("CONTENT_DEFAULT_WORKERS", "3"))))


def load_config() -> dict:
    with open(ROOT / "config" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_version() -> str:
    try:
        import subprocess as _sp
        r = _sp.run(["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, cwd=str(ROOT), timeout=3)
        return r.stdout.strip() or "dev"
    except Exception:
        return "dev"


def _patch_yaml_key(key: str, value: str) -> None:
    path = ROOT / "config" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(rf'^({re.escape(key)}:\s*).*$', rf'\g<1>{value}', text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def _patch_network_drives(drives: dict) -> None:
    path = ROOT / "config" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    block = "network_drives:\n" + "\n".join(f"  {k}: '{drives[k]}'" for k in sorted(drives))
    text = re.sub(r'^network_drives:(?:\n  \w[^\n]*)*', lambda _: block, text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def _save_smb_env(user: str, pwd: str) -> None:
    env_path = ROOT / "config" / ".env"
    text = env_path.read_text(encoding="utf-8")
    if "SMB_USER=" in text:
        text = re.sub(r'^SMB_USER=.*$', f'SMB_USER={user}', text, flags=re.MULTILINE)
    else:
        text += f"\nSMB_USER={user}"
    if "SMB_PASS=" in text:
        text = re.sub(r'^SMB_PASS=.*$', f'SMB_PASS={pwd}', text, flags=re.MULTILINE)
    else:
        text += f"\nSMB_PASS={pwd}"
    env_path.write_text(text, encoding="utf-8")


def save_active_topic(topic: str) -> None:
    _patch_yaml_key("active_topic", topic)


def save_backend(backend: str) -> None:
    _patch_yaml_key("api_backend", backend)



def list_topics() -> list[str]:
    if not TOPICS.exists():
        return []
    return sorted(p.name for p in TOPICS.iterdir() if p.is_dir())


class DriveConfigDialog(tk.Toplevel):
    _NET_USE_RE = re.compile(
        r'net\s+use\s+([A-Za-z]):?\s+(\\\\[^\s]+)\s+/user:(\S+)\s+(\S+)',
        re.IGNORECASE,
    )

    def __init__(self, app: "ContentApp"):
        super().__init__(app)
        self.app = app
        self.title("Network Drives")
        self.configure(bg=TH["bg"])
        self.geometry("580x280")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.lift()
        self.focus_force()

        tk.Label(self, text="Net use commands (1 lenh 1 dong):",
                 bg=TH["bg"], fg=TH["sub"], font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=14, pady=(12, 4))

        self.text = tk.Text(self, height=7, bg=TH["card"], fg=TH["text"],
                            insertbackground=TH["text"], relief="flat", bd=0,
                            font=("Consolas", 10), padx=10, pady=8)
        self.text.pack(fill="x", padx=12)
        self.text.insert("1.0", self._current_commands())

        self.status_lbl = tk.Label(self, text="", bg=TH["bg"], fg=TH["sub"], font=("Segoe UI", 9))
        self.status_lbl.pack(anchor="w", padx=14, pady=(6, 0))

        btn_row = tk.Frame(self, bg=TH["bg"])
        btn_row.pack(fill="x", padx=12, pady=10)
        tk.Button(btn_row, text="Apply & Reconnect", command=self._apply,
                  bg=TH["accent"], fg=TH["text"], relief="flat", bd=0,
                  font=("Segoe UI Semibold", 9), padx=12, pady=5, cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=TH["overlay"], fg=TH["sub"], relief="flat", bd=0,
                  font=("Segoe UI Semibold", 9), padx=12, pady=5, cursor="hand2").pack(side="left", padx=(8, 0))

    def _current_commands(self) -> str:
        drives = self.app.cfg.get("network_drives", {})
        user = os.environ.get("SMB_USER", "smbuser")
        pwd  = os.environ.get("SMB_PASS", "")
        return "\n".join(
            f"net use {k}: {drives[k]} /user:{user} {pwd} /persistent:yes"
            for k in sorted(drives)
        )

    def _apply(self) -> None:
        raw = self.text.get("1.0", "end").strip()
        drives, user, pwd = {}, "", ""
        for line in raw.splitlines():
            m = self._NET_USE_RE.search(line.strip())
            if m:
                drives[m.group(1).upper()] = m.group(2)
                user, pwd = m.group(3), m.group(4)
        if not drives:
            self.status_lbl.config(text="Khong phan tich duoc lenh nao", fg=TH["red"])
            return
        _patch_network_drives(drives)
        _save_smb_env(user, pwd)
        self.app.cfg["network_drives"] = drives
        os.environ["SMB_USER"] = user
        os.environ["SMB_PASS"] = pwd
        self.status_lbl.config(text="Da luu — dang ket noi...", fg=TH["yellow"])
        self.update()

        def do_connect():
            import pipeline as _pl
            _pl.ensure_drives(self.app.cfg, log=lambda m: self.app.log_q.put(("log", m)))
            self.app.log_q.put(("log", f"[drive] Drives da cap nhat: {sorted(drives.keys())}"))
            self.after(0, lambda: self.status_lbl.config(text="Hoan thanh!", fg=TH["green"]))

        threading.Thread(target=do_connect, daemon=True).start()


class RunnerThread(threading.Thread):
    def __init__(self, jobs: list[dict], cfg: dict, log_q: queue.Queue, done_cb, worker_label: str = ""):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.cfg = cfg
        self.log_q = log_q
        self.done_cb = done_cb
        self.worker_label = worker_label
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def log(self, msg: str) -> None:
        prefix = f"[W{self.worker_label}] " if self.worker_label else ""
        self.log_q.put(("log", prefix + msg))

    def run(self) -> None:
        api = api_mod.make_client(self.cfg, log_fn=self.log, stop_event=self.stop_event)
        ok = 0
        for job in self.jobs:
            if self.stop_event.is_set():
                break
            ma = job.get("ma", "?")
            self.log_q.put(("job_status", ma, "running"))
            self.log(f"[{ma}] Bắt đầu {job.get('channel', '')} — {job.get('title', '')[:80]}")
            try:
                def on_title_thumb(_ma: str, title: str, thumb: str) -> None:
                    sheets.write_title_thumb(
                        self.cfg["sheet"],
                        _ma,
                        title,
                        thumb,
                        log=lambda m, _ma=_ma: self.log(f"[{_ma}] {m}"),
                    )

                result = pipeline.run_job(
                    job,
                    self.cfg,
                    api,
                    log=lambda m, _ma=ma: self.log(f"[{_ma}] {m}"),
                    on_title_thumb=on_title_thumb,
                )
                if result.get("ok"):
                    sheets.write_content(
                        self.cfg["sheet"],
                        result["ma"],
                        result["script"],
                        seo=result.get("seo", ""),
                        log=lambda m, _ma=ma: self.log(f"[{_ma}] {m}"),
                    )
                    ok += 1
                    self.log_q.put(("job_status", ma, "done"))
                    self.log(f"[{ma}] Hoàn thành — {result.get('chars', 0):,} ký tự")
                else:
                    self.log_q.put(("job_status", ma, "error"))
                    self.log(f"[{ma}] Lỗi: result không ok")
            except Exception as exc:  # noqa: BLE001
                self.log_q.put(("job_status", ma, "error"))
                self.log(f"[{ma}] Lỗi: {exc}")
                if self.stop_event.is_set():
                    break
        self.log_q.put(("all_done", ok, len(self.jobs)))
        self.done_cb()


class ContentApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CONTENT Pipeline")
        self.configure(bg=TH["bg"])
        self.geometry(os.environ.get("CONTENT_GUI_GEOMETRY", "1040x640+8+8"))
        self.minsize(900, 540)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cfg = load_config()
        self.jobs: list[dict] = []
        self.job_status: dict[str, str] = {}
        self.log_q: queue.Queue = queue.Queue()
        self.runner: RunnerThread | None = None
        self.runners: list[RunnerThread] = []
        self.workers_done = 0
        self.auto_running = False
        self.selected_log = "system"
        self.job_logs: dict[str, list[str]] = {}
        self.system_logs: list[str] = []
        self.pipe_states = ["pending"] * len(PIPELINE_STEPS)
        self.filter_var = tk.StringVar(value="todo")
        self.search_var = tk.StringVar(value="")
        self.cycle_active = False
        self.stop_cycle = False
        self.cycle_after_id = None
        self.countdown_after_id = None
        self.countdown_left = 0

        self.build_ui()
        self.after(200, self.pump_logs)
        self.after(300, self.load_jobs)
        if AUTO_START_ENABLED:
            self.after(max(0, AUTO_START_DELAY_SEC) * 1000, self.auto_start_once)

    def _clabel(self, parent, text: str) -> None:
        tk.Label(parent, text=text, font=("Segoe UI Semibold", 8),
                 bg=TH["card"], fg=TH["sub"]).pack(side="left")

    def _sep(self, parent) -> None:
        tk.Frame(parent, bg=TH["border"], width=1).pack(side="left", fill="y", padx=10)

    def build_ui(self) -> None:
        # ── Row 1: brand + main actions ─────────────────────────
        header = tk.Frame(self, bg=TH["bg"], padx=12, pady=10)
        header.pack(fill="x")

        row1 = tk.Frame(header, bg=TH["bg"])
        row1.pack(fill="x")
        tk.Label(row1, text="CONTENT", font=("Segoe UI Bold", 16),
                 bg=TH["bg"], fg=TH["text"]).pack(side="left")
        tk.Label(row1, text=f"  v{_get_version()}",
                 font=("Segoe UI", 10), bg=TH["bg"], fg=TH["muted"]).pack(side="left")

        self.auto_btn = self.btn(row1, "AUTO", self.toggle_auto, primary=True)
        self.auto_btn.pack(side="right", padx=(6, 0))
        self.sync_btn = self.btn(row1, "Sync", self.load_jobs)
        self.sync_btn.pack(side="right", padx=(6, 0))
        self.update_btn = self.btn(row1, "Update", self.do_update)
        self.update_btn.pack(side="right", padx=(6, 0))

        # ── Row 2: controls bar ──────────────────────────────────
        row2 = tk.Frame(header, bg=TH["card"], padx=10, pady=7)
        row2.pack(fill="x", pady=(8, 0))

        # Workers
        self._clabel(row2, "Luong")
        self.workers_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        self.workers_spin = tk.Spinbox(
            row2, from_=1, to=4, width=2, textvariable=self.workers_var,
            bg=TH["card"], fg=TH["text"], buttonbackground=TH["border"],
            relief="flat", bd=0, justify="center", font=("Segoe UI Semibold", 10),
        )
        self.workers_spin.pack(side="left", padx=(4, 0))

        self._sep(row2)

        # Topic
        self._clabel(row2, "Topic")
        topics = list_topics()
        self.topic_var = tk.StringVar(value=self.cfg.get("active_topic", topics[0] if topics else ""))
        self.topic_combo = ttk.Combobox(row2, textvariable=self.topic_var, values=topics,
                                        width=14, state="readonly")
        self.topic_combo.pack(side="left", padx=(4, 0))
        self.topic_combo.bind("<<ComboboxSelected>>", lambda _e: self.change_topic())

        self._sep(row2)

        # Backend
        self._clabel(row2, "Backend")
        self.backend_var = tk.StringVar(value=self.cfg.get("api_backend", "http"))
        self.backend_combo = ttk.Combobox(row2, textvariable=self.backend_var,
                                          values=["cli", "http"], width=5, state="readonly")
        self.backend_combo.pack(side="left", padx=(4, 0))
        self.backend_combo.bind("<<ComboboxSelected>>", lambda _e: self.change_backend())
        self.check_btn = self.btn(row2, "Check", self.check_backend)
        self.check_btn.config(font=("Segoe UI", 8), padx=6, pady=2)
        self.check_btn.pack(side="left", padx=(6, 0))

        # Right side of controls bar
        self.sys_btn = self.btn(row2, "System Log", lambda: self.switch_log("system"))
        self.sys_btn.pack(side="right")
        self.drives_btn = self.btn(row2, "Drives", self.open_drive_config)
        self.drives_btn.pack(side="right", padx=(0, 8))

        # ── Row 3: badges + filter ───────────────────────────────
        row3 = tk.Frame(header, bg=TH["bg"])
        row3.pack(fill="x", pady=(6, 6))

        self.badges: dict[str, tk.Label] = {}
        for key, text, color in [
            ("running", "0 chay",  TH["accent"]),
            ("queued",  "0 cho",   TH["yellow"]),
            ("done",    "0 xong",  TH["green"]),
            ("error",   "0 loi",   TH["red"]),
        ]:
            lbl = tk.Label(row3, text=text, bg=color, fg=TH["text"],
                           font=("Segoe UI Semibold", 8), padx=8, pady=3)
            lbl.pack(side="left", padx=(0, 4))
            self.badges[key] = lbl

        filter_box = tk.Frame(row3, bg=TH["bg"])
        filter_box.pack(side="right")
        tk.Label(filter_box, text="Loc", font=("Segoe UI Semibold", 8),
                 bg=TH["bg"], fg=TH["muted"]).pack(side="left", padx=(0, 4))
        self.filter_combo = ttk.Combobox(
            filter_box, textvariable=self.filter_var,
            values=["todo", "all", "queued", "running", "done", "error"],
            width=9, state="readonly",
        )
        self.filter_combo.pack(side="left", padx=(0, 6))
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.rebuild_cards())
        self.search_entry = tk.Entry(
            filter_box, textvariable=self.search_var, width=22,
            bg=TH["overlay"], fg=TH["text"], insertbackground=TH["text"],
            relief="flat", font=("Segoe UI", 9),
        )
        self.search_entry.pack(side="left")
        self.search_entry.bind("<KeyRelease>", lambda _e: self.rebuild_cards())

        # ── Body ────────────────────────────────────────────────
        body = tk.Frame(self, bg=TH["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        body.columnconfigure(0, minsize=320)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=TH["surface"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.build_left(left)

        right = tk.Frame(body, bg=TH["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        self.build_right(right)

    def build_left(self, parent) -> None:
        hdr = tk.Frame(parent, bg=TH["surface"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="JOB LIST", font=("Segoe UI Semibold", 9), bg=TH["surface"], fg=TH["muted"]).pack(side="left")
        self.count_lbl = tk.Label(hdr, text="", font=("Segoe UI", 8), bg=TH["surface"], fg=TH["muted"])
        self.count_lbl.pack(side="right")
        tk.Frame(parent, bg=TH["border"], height=1).pack(fill="x")

        wrap = tk.Frame(parent, bg=TH["surface"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=TH["surface"], highlightthickness=0, bd=0)
        sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=TH["surface"])
        self.win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win_id, width=e.width))

    def build_right(self, parent) -> None:
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)

        prog = tk.Frame(parent, bg=TH["card"], padx=10, pady=8)
        prog.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.pipe_labels: list[tk.Label] = []
        for _, name in PIPELINE_STEPS:
            lbl = tk.Label(prog, text=name, bg=TH["card"], fg=TH["muted"], font=("Segoe UI Semibold", 8), padx=8)
            lbl.pack(side="left", expand=True, fill="x")
            self.pipe_labels.append(lbl)

        self.status_lbl = tk.Label(parent, text="Chọn job để xem log, hoặc bấm AUTO để chạy queue", bg=TH["card"], fg=TH["text"], font=("Segoe UI Semibold", 10), padx=10, pady=8, anchor="w")
        self.status_lbl.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        log_frame = tk.Frame(parent, bg=TH["crust"], padx=2, pady=2)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", bg=TH["crust"], fg=TH["sub"], insertbackground=TH["sub"], relief="flat", bd=0, padx=10, pady=8, font=("Consolas", 9), state="disabled")
        log_sb = tk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_sb.grid(row=0, column=1, sticky="ns")

    def btn(self, parent, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=TH["accent"] if primary else TH["overlay"],
            fg=TH["text"] if primary else TH["sub"],
            activebackground=TH["border"],
            activeforeground=TH["text"],
            relief="flat",
            bd=0,
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=5,
            cursor="hand2",
        )

    def change_topic(self) -> None:
        if self.auto_running:
            self.log("Không đổi topic khi AUTO đang chạy")
            return
        topic = self.topic_var.get().strip()
        if not topic:
            return
        save_active_topic(topic)
        self.cfg = load_config()
        self.log(f"Đã đổi active_topic: {topic}")
        self.load_jobs()

    def do_update(self) -> None:
        self.update_btn.config(state="disabled", text="...")
        import threading as _t

        def _via_git() -> str:
            import subprocess as _sp
            _sp.run(["git", "--version"], capture_output=True, check=True, timeout=5)
            r = _sp.run(
                ["git", "pull"], capture_output=True, text=True, cwd=str(ROOT), timeout=60,
            )
            return (r.stdout or r.stderr or "").strip()

        def _via_zip() -> str:
            import shutil, tempfile, urllib.request, zipfile
            url = "https://github.com/manhthang1905-hub/content/archive/refs/heads/main.zip"
            self.log_q.put(("log", "[Update] Git khong co — tai ZIP tu GitHub..."))
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = os.path.join(tmp, "update.zip")
                urllib.request.urlretrieve(url, zip_path)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(tmp)
                src = os.path.join(tmp, "content-main")
                skip = {"config", "output"}
                for item in os.listdir(src):
                    if item in skip:
                        continue
                    s = os.path.join(src, item)
                    d = os.path.join(str(ROOT), item)
                    if os.path.isdir(s):
                        if os.path.exists(d):
                            shutil.rmtree(d)
                        shutil.copytree(s, d)
                    else:
                        shutil.copy2(s, d)
            return "Xong (ZIP). Khoi dong lai de ap dung."

        def worker():
            try:
                try:
                    out = _via_git()
                except Exception:
                    out = _via_zip()
                self.log_q.put(("log", f"[Update] {out[:300]}"))
                self.after(0, self._restart)
            except Exception as exc:
                self.log_q.put(("log", f"[Update] LOI: {exc}"))
                self.after(0, lambda: self.update_btn.config(state="normal", text="Update"))

        _t.Thread(target=worker, daemon=True).start()

    def _restart(self) -> None:
        import subprocess as _sp
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        exe = str(pythonw) if pythonw.exists() else sys.executable
        _sp.Popen([exe, str(ROOT / "gui.py")])
        self.destroy()

    def open_drive_config(self) -> None:
        try:
            DriveConfigDialog(self)
        except Exception as exc:
            self.log(f"[Drives] LOI mo dialog: {exc}")

    def change_backend(self) -> None:
        if self.auto_running:
            self.log("Không đổi backend khi AUTO đang chạy")
            self.backend_var.set(self.cfg.get("api_backend", "http"))
            return
        backend = self.backend_var.get()
        save_backend(backend)
        self.cfg = load_config()
        self.log(f"Đã đổi backend: {backend}")

    def check_backend(self) -> None:
        self.check_btn.config(state="disabled", text="...")
        cfg = self.cfg.copy()

        def worker():
            try:
                client = api_mod.make_client(cfg, log_fn=lambda m: None)
                resp = client.call(
                    "check",
                    system="You are a test assistant.",
                    user_message="Reply with just: OK",
                    max_tokens=16,
                )
                model_name = getattr(resp, "model", "?")
                self.log_q.put(("log", f"[Check] OK — model: {model_name}"))
            except Exception as exc:
                self.log_q.put(("log", f"[Check] LOI: {exc}"))
            self.log_q.put(("check_done",))

        threading.Thread(target=worker, daemon=True).start()


    def load_jobs(self, auto_cycle: bool = False) -> None:
        if self.auto_running:
            return
        self.log("Đọc job từ Google Sheet...")
        self.sync_btn.config(state="disabled")

        def worker():
            while True:
                if auto_cycle and self.stop_cycle:
                    break
                try:
                    cfg = load_config()
                    jobs = sheets.get_pending(cfg["sheet"], log=lambda m: self.log_q.put(("log", m)))
                    skipped = [j for j in jobs if not pipeline.channel_exists(j.get("channel", ""), cfg)]
                    jobs = [j for j in jobs if pipeline.channel_exists(j.get("channel", ""), cfg)]
                    self.log_q.put(("jobs", jobs, len(skipped), cfg, auto_cycle))
                    return
                except Exception as exc:  # noqa: BLE001
                    self.log_q.put(("log", f"Lỗi đọc Sheet: {exc}; sẽ thử lại sau 60s"))
                    time.sleep(60)
            self.log_q.put(("sync_done",))

        threading.Thread(target=worker, daemon=True).start()

    def set_jobs(self, jobs: list[dict], skipped: int, cfg: dict, auto_cycle: bool = False) -> None:
        self.cfg = cfg
        seen = {j["ma"] for j in jobs}
        for old in list(self.job_status):
            if old not in seen and self.job_status.get(old) != "running":
                self.job_status.pop(old, None)
                self.job_logs.pop(old, None)
        self.jobs = jobs
        for job in jobs:
            ma = job["ma"]
            if self.job_status.get(ma) not in ("done", "error", "running"):
                self.job_status[ma] = "queued"
            self.job_logs.setdefault(ma, [])
        self.rebuild_cards()
        self.sync_btn.config(state="normal")
        self.log(f"{len(jobs)} job hợp lệ; bỏ qua {skipped} job thiếu cấu hình kênh")
        if self.cycle_active and not self.stop_cycle and not self.auto_running:
            if any(self.job_status.get(j["ma"], "queued") == "queued" for j in jobs):
                self.start_runner()
            elif auto_cycle:
                self.schedule_next_cycle()

    def visible_jobs(self) -> list[dict]:
        mode = self.filter_var.get()
        needle = self.search_var.get().strip().lower()
        out = []
        for job in self.jobs:
            ma = job["ma"]
            st = self.job_status.get(ma, "queued")
            if mode == "todo" and st not in ("queued", "running", "error"):
                continue
            if mode not in ("all", "todo") and st != mode:
                continue
            haystack = " ".join(str(job.get(k, "")) for k in ("ma", "channel", "title", "link")).lower()
            if needle and needle not in haystack:
                continue
            out.append(job)
        return out

    def rebuild_cards(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        counts = {"running": 0, "queued": 0, "done": 0, "error": 0}
        for job in self.jobs:
            st = self.job_status.get(job["ma"], "queued")
            counts[st] = counts.get(st, 0) + 1

        visible = self.visible_jobs()
        for job in visible:
            ma = job["ma"]
            st = self.job_status.get(ma, "queued")
            card = tk.Frame(self.inner, bg=TH["card"], padx=10, pady=8, cursor="hand2")
            card.pack(fill="x", padx=8, pady=5)
            top = tk.Frame(card, bg=TH["card"])
            top.pack(fill="x")
            tk.Label(top, text=ma, bg=TH["card"], fg=TH["muted"], font=("Segoe UI", 9)).pack(side="left")
            tk.Label(top, text=job.get("channel", ""), bg=TH["overlay"], fg=TH["sub"], font=("Segoe UI", 8), padx=6, pady=2).pack(side="right")
            title = job.get("title") or "—"
            if len(title) > 48:
                title = title[:46] + "…"
            tk.Label(card, text=title, bg=TH["card"], fg=TH["text"], font=("Segoe UI Bold", 11), anchor="w").pack(fill="x", pady=(4, 0))
            status_color = {"queued": TH["yellow"], "running": TH["accent"], "done": TH["green"], "error": TH["red"]}.get(st, TH["muted"])
            tk.Label(card, text=st, bg=TH["card"], fg=status_color, font=("Segoe UI Semibold", 8), anchor="w").pack(fill="x", pady=(2, 0))

            def bind_click(widget, _ma=ma):
                widget.bind("<Button-1>", lambda _e: self.switch_log(_ma))
                for c in widget.winfo_children():
                    bind_click(c, _ma)
            bind_click(card)

        self.count_lbl.config(text=f"{len(visible)}/{len(self.jobs)} jobs")
        self.badges["running"].config(text=f"{counts['running']} đang chạy")
        self.badges["queued"].config(text=f"{counts['queued']} chờ")
        self.badges["done"].config(text=f"{counts['done']} xong")
        self.badges["error"].config(text=f"{counts['error']} lỗi")

    def toggle_auto(self) -> None:
        if self.auto_running:
            self.stop_runner()
        else:
            self.start_runner()

    def start_runner(self) -> None:
        pending = [j for j in self.jobs if self.job_status.get(j["ma"], "queued") == "queued"]
        if not pending:
            self.log("Không có job nào để chạy")
            return
        self.cycle_active = True
        self.stop_cycle = False
        self.auto_running = True
        self.workers_done = 0
        self.auto_btn.config(text="AUTO ĐANG CHẠY", bg=TH["red"])
        self.sync_btn.config(state="disabled")
        self.topic_combo.config(state="disabled")
        self.workers_spin.config(state="disabled")
        self.backend_combo.config(state="disabled")
        self.check_btn.config(state="disabled")
        self.drives_btn.config(state="disabled")
        n_workers = min(self.get_worker_count(), len(pending))
        queues = [pending[i::n_workers] for i in range(n_workers)]
        self.runners = []
        for i, jobs in enumerate(queues, start=1):
            runner = RunnerThread(jobs, self.cfg, self.log_q, done_cb=self.worker_done, worker_label=str(i))
            self.runners.append(runner)
            runner.start()
        self.runner = self.runners[0] if self.runners else None
        summary = " | ".join(f"W{i + 1}: {len(q)} job" for i, q in enumerate(queues))
        self.log(f"AUTO bắt đầu: {n_workers} luồng — {summary}")

    def get_worker_count(self) -> int:
        try:
            return max(1, min(4, int(self.workers_var.get())))
        except ValueError:
            return 1

    def worker_done(self) -> None:
        self.log_q.put(("worker_done",))

    def stop_runner(self) -> None:
        for runner in self.runners:
            runner.stop()
        if self.runner:
            self.runner.stop()
        self.runners = []
        self.auto_running = False
        self.cycle_active = False
        self.stop_cycle = True
        if self.cycle_after_id:
            self.after_cancel(self.cycle_after_id)
            self.cycle_after_id = None
        if self.countdown_after_id:
            self.after_cancel(self.countdown_after_id)
            self.countdown_after_id = None
        self.auto_btn.config(text="AUTO", bg=TH["accent"])
        self.sync_btn.config(state="normal")
        self.topic_combo.config(state="readonly")
        self.workers_spin.config(state="normal")
        self.backend_combo.config(state="readonly")
        self.check_btn.config(state="normal")
        self.drives_btn.config(state="normal")
        self.log("Đã yêu cầu dừng; job hiện tại sẽ dừng sau khi bước đang chạy kết thúc")

    def switch_log(self, ma: str) -> None:
        self.selected_log = ma
        self.status_lbl.config(text="System log" if ma == "system" else f"Đang xem log: {ma}")
        self.render_log()

    def auto_start_once(self) -> None:
        if self.auto_running or self.cycle_active:
            return
        self.cycle_active = True
        self.stop_cycle = False
        if any(self.job_status.get(j["ma"], "queued") == "queued" for j in self.jobs):
            self.start_runner()
        else:
            self.log("AUTO nền bật: chưa có job, sẽ tự sync định kỳ")
            self.schedule_next_cycle()

    def schedule_next_cycle(self) -> None:
        if self.stop_cycle:
            return
        self.countdown_left = AUTO_CYCLE_MINUTES
        self.log(f"AUTO chờ {AUTO_CYCLE_MINUTES} phút rồi sync lại")
        self.tick_countdown()
        self.cycle_after_id = self.after(AUTO_CYCLE_MINUTES * 60 * 1000, self.cycle_next)

    def tick_countdown(self) -> None:
        if self.stop_cycle or not self.cycle_active:
            return
        if self.countdown_left > 0:
            self.status_lbl.config(text=f"AUTO đang chờ sync lại sau {self.countdown_left} phút")
            self.countdown_left -= 1
            self.countdown_after_id = self.after(60_000, self.tick_countdown)

    def cycle_next(self) -> None:
        if self.stop_cycle:
            return
        self.log("AUTO sync Sheet để tìm job mới")
        self.load_jobs(auto_cycle=True)

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.system_logs.append(line)
        if self.selected_log == "system":
            self.append_log(line)

    def append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 900:
            self.log_text.delete("1.0", "120.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def render_log(self) -> None:
        lines = self.system_logs if self.selected_log == "system" else self.job_logs.get(self.selected_log, [])
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for line in lines[-900:]:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def update_pipeline_from_log(self, msg: str) -> None:
        step = None
        if "[fetch]" in msg:
            step = "fetch"
        elif "[title/thumb]" in msg:
            step = "title"
        elif "[write]" in msg:
            step = "write"
        elif "[check]" in msg:
            step = "check"
        elif "[seo]" in msg:
            step = "seo"
        elif "[sheets]" in msg:
            step = "sheet"
        if not step:
            return
        keys = [k for k, _ in PIPELINE_STEPS]
        idx = keys.index(step)
        for i, lbl in enumerate(self.pipe_labels):
            if i < idx:
                lbl.config(fg=TH["green"])
            elif i == idx:
                lbl.config(fg=TH["accent"])
            else:
                lbl.config(fg=TH["muted"])

    def pump_logs(self) -> None:
        changed = False
        while True:
            try:
                item = self.log_q.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "log":
                msg = item[1]
                ts = time.strftime("%H:%M:%S")
                line = f"[{ts}] {msg}"
                dest = "system"
                for job in self.jobs:
                    ma = job["ma"]
                    if f"[{ma}]" in msg:
                        dest = ma
                        break
                if dest == "system":
                    self.system_logs.append(line)
                else:
                    self.job_logs.setdefault(dest, []).append(line)
                    if any(mark in msg for mark in ("[title/thumb]", "[sheets]", "Hoàn thành", "Lỗi:")):
                        self.system_logs.append(line)
                    self.update_pipeline_from_log(msg)
                if self.selected_log == dest or (dest != "system" and self.selected_log == "system" and line in self.system_logs[-1:]):
                    self.append_log(line)
            elif kind == "jobs":
                _, jobs, skipped, cfg, auto_cycle = item
                self.set_jobs(jobs, skipped, cfg, auto_cycle=auto_cycle)
            elif kind == "job_status":
                _, ma, st = item
                self.job_status[ma] = st
                changed = True
            elif kind == "all_done":
                _, ok, total = item
                self.log(f"Hoàn tất: {ok}/{total} job thành công")
                changed = True
            elif kind == "worker_done":
                self.workers_done += 1
                if self.workers_done >= max(1, len(self.runners)):
                    self.auto_running = False
                    self.runners = []
                    self.auto_btn.config(text="AUTO", bg=TH["accent"])
                    self.sync_btn.config(state="normal")
                    self.topic_combo.config(state="readonly")
                    self.workers_spin.config(state="normal")
                    self.backend_combo.config(state="readonly")
                    self.check_btn.config(state="normal")
                    self.drives_btn.config(state="normal")
                    if self.cycle_active and not self.stop_cycle:
                        self.schedule_next_cycle()
                    changed = True
            elif kind == "sync_done":
                self.sync_btn.config(state="normal")
            elif kind == "check_done":
                self.check_btn.config(state="normal", text="Check")
        if changed:
            self.rebuild_cards()
        self.after(200, self.pump_logs)

    def on_close(self) -> None:
        for runner in self.runners:
            runner.stop()
        if self.runner:
            self.runner.stop()
        if self.cycle_after_id:
            self.after_cancel(self.cycle_after_id)
        if self.countdown_after_id:
            self.after_cancel(self.countdown_after_id)
        self.destroy()
        os._exit(0)


if __name__ == "__main__":
    ContentApp().mainloop()
