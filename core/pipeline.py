"""pipeline.py — Viết kịch bản voiceover từ transcript đối thủ.

Luồng: fetch transcript → title/thumb → write (oneshot) → check/fix → format → save
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import yaml
import youtube

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(_ROOT, "prompts")
TOPICS_DIR = os.path.join(_ROOT, "topics")
RUNS_DIR = os.path.join(_ROOT, "output", "runs")

_SENTENCE_END = ".!?…。！？"


# ── Tiện ích ─────────────────────────────────────────────────────────────────
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render(template: str, variables: dict) -> str:
    out = template
    for key, val in variables.items():
        out = out.replace(f"<<{key}>>", str(val))
    return out


def clean_voice_text(text: str, blank_line_between_paragraphs: bool = True) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[.*?\]", "", text)                        # strip [music], [narrator], etc.
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("—", ", ").replace(" – ", ", ")
    lines = [ln.strip() for ln in text.split("\n")]
    fixed = []
    for ln in lines:
        ln = re.sub(rf"([{re.escape(_SENTENCE_END)}])([A-Za-zÀ-ÿ¿¡])", r"\1 \2", ln)
        ln = re.sub(r"[ \t]{2,}", " ", ln)
        fixed.append(ln)
    out: list[str] = []
    blank = False
    for ln in fixed:
        if not ln:
            if blank or not blank_line_between_paragraphs:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(ln)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def count_chars(text: str) -> int:
    return len((text or "").strip())


def apply_thumb_case(text: str, style: str = "upper") -> str:
    text = (text or "").strip()
    if style == "preserve":
        return text
    if style == "turkish_upper":
        return text.translate(str.maketrans({"i": "İ", "ı": "I"})).upper()
    return text.upper()


def casing_instruction(style: str) -> str:
    s = (style or "upper").strip().lower()
    if s == "preserve":
        return "Keep natural casing with all diacritics (this language does not use full uppercase)."
    if s == "turkish_upper":
        return "UPPERCASE using Turkish rules (i→İ, ı→I) and KEEP all Turkish characters (ç ğ ö ş ü). Never strip diacritics."
    return "UPPERCASE but KEEP all language diacritics intact (never strip accents, never convert to plain ASCII)."


def trim_title(title: str, max_chars: int = 100) -> str:
    t = (title or "").strip().strip('"').strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars].rsplit(" ", 1)[0].strip()
    return cut or t[:max_chars].strip()


def _drive_ok(letter: str) -> bool:
    try:
        r = subprocess.run(
            ["net", "use", f"{letter}:"],
            capture_output=True, text=True, timeout=5, errors="replace",
        )
        return r.returncode == 0 and "OK" in r.stdout
    except Exception:
        return False


_DEAD_DRIVES: set[str] = set()  # drive treo/loi trong phien nay — bo qua, khong thu lai moi job


def ensure_drives(cfg: dict, log=print) -> None:
    """Drive chi la tien ich phu — moi loi/treo o day KHONG duoc phep giet job
    (tung lam job chet SAU khi viet xong script vi 'net use' treo qua 15s)."""
    drives = cfg.get("network_drives", {})
    if not drives:
        return
    user = os.environ.get("SMB_USER", "")
    pwd  = os.environ.get("SMB_PASS", "")
    for letter, path in drives.items():
        drive = f"{letter}:"
        if letter in _DEAD_DRIVES:
            continue
        try:
            if _drive_ok(letter):
                log(f"[drive] {drive} OK")
                continue
            log(f"[drive] {drive} mat ket noi — ket noi lai {path}...")
            subprocess.run(["net", "use", drive, "/delete", "/yes"],
                           capture_output=True, timeout=10)
            r = subprocess.run(
                ["net", "use", drive, path, f"/user:{user}", pwd, "/persistent:yes"],
                capture_output=True, text=True, timeout=15, errors="replace",
            )
            if r.returncode == 0:
                log(f"[drive] {drive} ket noi lai OK")
            else:
                _DEAD_DRIVES.add(letter)
                log(f"[drive] {drive} THAT BAI (bo qua den het phien): {(r.stderr or r.stdout).strip()[:200]}")
        except Exception as exc:  # noqa: BLE001 — treo/timeout: bo qua drive nay
            _DEAD_DRIVES.add(letter)
            log(f"[drive] {drive} LOI (bo qua den het phien): {exc}")


def copy_to_voice(final_path: str, ma: str, cfg: dict, log=print) -> dict:
    voice_dir = (
        os.environ.get("CONTENT_VOICE_DIR")
        or os.environ.get("TAMLY_VOICE_DIR")
        or cfg.get("output", {}).get("voice_dir")
        or r"C:\Users\Administrator\Desktop\voice\voice"
    )
    dst = os.path.join(voice_dir, f"{ma}.txt")
    last_err = None
    for attempt in range(1, 4):
        try:
            os.makedirs(voice_dir, exist_ok=True)
            shutil.copy2(final_path, dst)
            log(f"[voice] Copied → {dst}")
            return {"status": "ok", "path": dst}
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"[voice] copy attempt {attempt}/3 failed: {exc}")
    log(f"[voice] WARNING: copy failed after retries: {last_err}; script vẫn ở {final_path}")
    return {"status": "error", "path": dst, "message": str(last_err)}


# ── Tự cài thư viện thiếu (máy khác pull code mới về là chạy được ngay) ──────
_DEPS_CHECKED = False


def ensure_deps(log=print) -> list[str]:
    """Kiểm tra các gói pipeline transcript cần; thiếu thì pip install -r
    requirements.txt (ẩn cửa sổ). Trả về danh sách gói đã thiếu ([] = đủ)."""
    global _DEPS_CHECKED
    if _DEPS_CHECKED:
        return []
    _DEPS_CHECKED = True
    checks = [
        ("faster_whisper", "faster-whisper"),
        ("curl_cffi", "curl-cffi"),
        ("socks", "requests[socks]"),
        ("DrissionPage", "DrissionPage"),
    ]
    missing = []
    for mod, pkg in checks:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    try:
        from yt_dlp.version import __version__ as _v
        if tuple(int(x) for x in _v.split(".")[:2]) < (2026, 7):
            missing.append(f"yt-dlp (đang {_v}, cần >=2026.7)")
    except Exception:
        missing.append("yt-dlp")
    if not missing:
        return []
    log(f"[deps] Thiếu/cũ: {', '.join(missing)} — đang pip install -r requirements.txt ...")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r",
             os.path.join(_ROOT, "requirements.txt")],
            capture_output=True, text=True, timeout=1800, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            log("[deps] Cài xong — transcript đủ đồ chạy")
        else:
            log(f"[deps] pip lỗi (tool vẫn chạy, thiếu gói nào method đó tự bỏ qua): "
                f"{(r.stderr or r.stdout).strip()[:200]}")
    except Exception as exc:  # noqa: BLE001
        log(f"[deps] Không cài được ({exc}) — tool vẫn chạy với fallback")
    return missing


# ── Dọn rác định kỳ (vận hành dài ngày, tool bật 24/7 không tắt) ─────────────
_LAST_GC_DAY = ""


def cleanup_garbage(log=print, force: bool = False) -> None:
    """Chạy tối đa 1 lần/ngày (gọi thoải mái, tự bỏ qua nếu hôm nay đã dọn).
    - output/runs: xóa run cũ >7 ngày (final.txt đã sang voice, Sheet đã ghi)
    - output/logs: xóa log cũ >30 ngày
    - %TEMP%/DrissionPage*: profile browser tạm >1 ngày
    - ~/.claude/projects/*content*: transcript phiên `claude --print` >14 ngày
      (mỗi call 1 file — 60+ call/ngày, không dọn sẽ phình hàng GB)
    """
    global _LAST_GC_DAY
    import glob
    import time as _time
    today = datetime.now().strftime("%Y%m%d")
    if not force and _LAST_GC_DAY == today:
        return
    _LAST_GC_DAY = today
    now = _time.time()

    def _rm_old(pattern: str, days: float, kind: str) -> None:
        n = 0
        for p in glob.glob(pattern):
            try:
                if now - os.path.getmtime(p) > days * 86400:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    n += 1
            except Exception:
                pass
        if n:
            log(f"[gc] Xóa {n} {kind} cũ")

    _rm_old(os.path.join(RUNS_DIR, "*"), 7, "run")
    _rm_old(os.path.join(_ROOT, "output", "logs", "*.log"), 30, "log")
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if tmp:
        _rm_old(os.path.join(tmp, "DrissionPage", "*"), 1, "browser profile tạm")
        _rm_old(os.path.join(tmp, "DrissionPage*"), 1, "browser profile tạm")
    proj = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    repo_key = os.path.basename(_ROOT).lower()
    for d in glob.glob(os.path.join(proj, "*")):
        if repo_key in os.path.basename(d).lower():
            _rm_old(os.path.join(d, "*.jsonl"), 14, "claude session")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            return fm, parts[2].lstrip("\n")
    return {}, text


def load_prompt(name: str) -> str:
    return _read(os.path.join(PROMPTS_DIR, name))


# ── Kênh ─────────────────────────────────────────────────────────────────────
def _channel_path(channel_id: str, cfg: dict) -> tuple[str, str]:
    style, lang_num = channel_id.split("-")
    lang_code = cfg["languages"][lang_num]
    lang_dir = os.path.join(TOPICS_DIR, cfg["active_topic"], lang_code)
    return os.path.join(lang_dir, f"{style}.md"), lang_code


def channel_exists(channel_id: str, cfg: dict) -> bool:
    try:
        path, _ = _channel_path(channel_id, cfg)
    except (KeyError, ValueError):
        return False
    return os.path.exists(path)


def load_channel(channel_id: str, cfg: dict) -> dict:
    path, lang_code = _channel_path(channel_id, cfg)
    fm, body = _parse_frontmatter(_read(path))
    return {
        "id": channel_id,
        "lang_code": lang_code,
        "language": fm.get("language", ""),
        "channel": body.strip(),
        "thumb_case": fm.get("thumb_case", "upper"),
        "title_thumb_mode": fm.get("title_thumb", "restyled"),
        "target_minutes": fm.get("target_minutes", 0),
    }


# cpm mặc định (đo từ TXT+MP3 thật bên tam-ly). Để TRONG code vì cơ chế Update bỏ qua
# thư mục config/ (giữ active_topic + creds mỗi máy) — nếu để trong config.yaml thì VM khác
# không bao giờ nhận được bảng này. config.yaml chỉ là override tùy chọn.
DEFAULT_CHARS_PER_MIN = {
    "es": 973, "vi": 832, "en": 920, "fr": 1048, "de": 895,
    "pt": 935, "ja": 341, "ko": 445, "it": 875, "tr": 766,
}


def target_chars_for(chan: dict, cfg: dict) -> int:
    """Số ký tự mục tiêu = target_minutes × cpm[ngôn ngữ]. 0 nếu kênh không khai báo."""
    try:
        minutes = float(chan.get("target_minutes", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return 0
    lang = chan.get("lang_code", "")
    # Ưu tiên config.yaml (nếu máy có), thiếu thì dùng bảng mặc định trong code
    cpm = (cfg.get("chars_per_min") or {}).get(lang) or DEFAULT_CHARS_PER_MIN.get(lang, 0)
    return round(minutes * cpm) if cpm else 0


# ── Tiêu đề + text thumb ─────────────────────────────────────────────────────
def _parse_title_thumb(text: str) -> tuple[str, str]:
    title, thumb = "", ""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.upper().startswith("TITLE:"):
            title = s.split(":", 1)[1].strip()
        elif s.upper().startswith("THUMB:"):
            thumb = s.split(":", 1)[1].strip()
    return title, thumb


def generate_title_thumb(api, cfg, chan, competitor_title, transcript, log) -> dict:
    log("[title/thumb] Tạo tiêu đề + text thumb...")
    resp = api.call(
        stage="check",
        system="You create video titles and thumbnail text. Return EXACTLY 2 lines: TITLE: and THUMB:",
        user_message=render(load_prompt("title_thumb.md"), {
            "COMPETITOR_TITLE": competitor_title,
            "TRANSCRIPT_SAMPLE": transcript[:600],
            "LANGUAGE": chan["language"],
            "CHANNEL": chan["channel"],
            "MODE": chan.get("title_thumb_mode", "restyled"),
            "CASING": casing_instruction(chan.get("thumb_case", "upper")),
        }),
        model=cfg["models"]["check"],
        temperature=0.7,
        max_tokens=300,
    )
    title, thumb = _parse_title_thumb(resp.text)
    title = trim_title(title, 100)
    thumb = apply_thumb_case(thumb, chan.get("thumb_case", "upper"))
    log(f"[title/thumb] TITLE: {title[:60]} | THUMB: {thumb[:40]}")
    return {"title": title, "thumb": thumb}


# ── Pipeline: viết + check/fix ────────────────────────────────────────────────
def write_oneshot(api, cfg, chan, transcript, title, log) -> str:
    log("[write] Viết script...")
    resp = api.call(
        stage="write",
        system="You write viral voiceover scripts. Output the voiceover lines only — no stage directions, no bracketed annotations.",
        user_message=render(load_prompt("write_oneshot.md"), {
            "LANGUAGE": chan["language"],
            "CHANNEL": chan["channel"],
            "COMPETITOR_TRANSCRIPT": transcript,
            "TITLE": title,
        }),
        model=cfg["models"]["write"],
        temperature=0.8,
        max_tokens=16000,
    )
    text = clean_voice_text(resp.text, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"])
    log(f"      {count_chars(text):,} ký tự")
    return text


def check_fix_oneshot(api, cfg, chan, transcript, draft, log) -> str:
    log("[check] Đánh giá và sửa...")
    resp = api.call(
        stage="check",
        system="You write viral voiceover scripts. Output the corrected voiceover lines only — no stage directions, no bracketed annotations.",
        user_message=render(load_prompt("check_fix.md"), {
            "LANGUAGE": chan["language"],
            "CHANNEL": chan["channel"],
            "COMPETITOR_TRANSCRIPT": transcript,
            "DRAFT": draft,
        }),
        model=cfg["models"]["check"],
        temperature=0.5,
        max_tokens=16000,
    )
    text = clean_voice_text(resp.text, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"])
    log(f"      {count_chars(text):,} ký tự")
    return text


# ── Adapt + Review (chỉ chạy khi kênh có target_minutes) ─────────────────────
def adapt_oneshot(api, cfg, chan, draft, target_chars, log) -> str:
    # LLM không đếm được ký tự khi viết, lại VƯỢT/HỤT số khai với tỉ lệ dao động. Nên vòng
    # phản hồi: lần đầu khai ĐÚNG target; đo thực tế; THỪA thì giảm số khai, THIẾU thì tăng —
    # có GIẢM CHẤN để khỏi dao động (full tỉ lệ từng nhảy 17k→5k). Cuối cùng giữ bản GẦN
    # target nhất. Không số "đoán" cứng → hợp mọi nguồn.
    #
    # Lượt 1-3: nén từ BẢN GỐC (đủ chất liệu, tránh tam sao thất bản).
    # Lượt 4-5 (chỉ khi 1-3 đều trượt ±25%): đổi chiến thuật — bản tốt nhất đang DÀI hơn
    # target thì RÚT GỌN từ chính nó (cắt 5k→3k dễ hơn nhiều nén 18k→3k); đang NGẮN hơn
    # thì thử lại từ bản gốc với số khai đã chỉnh.
    tmpl = load_prompt("adapt.md")
    lo, hi = target_chars * 0.75, target_chars * 1.25        # trong ±25% là đạt, dừng luôn
    aim = target_chars                                        # lần đầu: khai đúng target (1×)
    best, best_gap = draft, abs(count_chars(draft) - target_chars)
    for attempt in range(1, 6):
        src = draft
        if attempt >= 4 and best is not draft and count_chars(best) > hi:
            src = best                    # cắt tỉa từ bản gần nhất phía TRÊN target
            aim = target_chars
        resp = api.call(
            stage="check",
            system="You refine viral voiceover scripts. Output the script only — no commentary.",
            user_message=render(tmpl, {
                "LANGUAGE": chan["language"],
                "CHANNEL": chan["channel"],
                "CHARS": aim,
                "DRAFT": src,
            }),
            model=cfg["models"]["check"],
            temperature=0.6,
            max_tokens=16000,
        )
        result = clean_voice_text(resp.text, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"])
        n = count_chars(result)
        log(f"[adapt {attempt}] khai {aim:,}{' (rút từ bản gần nhất)' if src is not draft else ''} "
            f"→ {n:,} ký tự (target {target_chars:,})")
        if abs(n - target_chars) < best_gap:
            best, best_gap = result, abs(n - target_chars)
        if lo <= n <= hi:
            return result
        # chỉnh số khai về phía cần, GIẢM CHẤN bằng mũ 0.6 (vì output siêu tuyến tính theo aim)
        factor = (target_chars / max(1, n)) ** 0.6
        aim = int(max(target_chars * 0.3, min(target_chars * 1.5, aim * factor)))
    return best


def review_oneshot(api, cfg, chan, draft, target_chars, log) -> str:
    log("[review] Trau chuốt + giữ ký tự...")
    resp = api.call(
        stage="check",
        system="You polish viral voiceover scripts. Output the script only — no commentary.",
        user_message=render(load_prompt("review.md"), {
            "LANGUAGE": chan["language"],
            "CHANNEL": chan["channel"],
            "CHARS": target_chars,
            "DRAFT": draft,
        }),
        model=cfg["models"]["check"],
        temperature=0.5,
        max_tokens=16000,
    )
    text = clean_voice_text(resp.text, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"])
    log(f"      {count_chars(text):,} ký tự")
    return text


# ── Mô tả / SEO ───────────────────────────────────────────────────────────────
def _parse_seo_package(text: str) -> dict:
    result = {"seo": "", "hashtags": "", "seo_kw": ""}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("DESCRIPTION:"):
            current = "seo"; buf = [s[12:].strip()] if s[12:].strip() else []
        elif s.upper().startswith("HASHTAGS:"):
            if current: result[current] = "\n".join(buf).strip()
            current = "hashtags"; buf = [s[9:].strip()] if s[9:].strip() else []
        elif s.upper().startswith("KEYWORDS:"):
            if current: result[current] = "\n".join(buf).strip()
            current = "seo_kw"; buf = [s[9:].strip()] if s[9:].strip() else []
        elif current is not None:
            buf.append(line)
    if current:
        result[current] = "\n".join(buf).strip()
    return result


def generate_seo_package(api, cfg, chan, title, thumb, channel_keywords, script, log) -> dict:
    log("[seo] Tạo mô tả + hashtag + từ khóa SEO...")
    opening = "\n".join(script.splitlines()[:40]).strip()
    resp = api.call(
        stage="check",
        system="You write YouTube SEO content. Output exactly 3 labeled sections: DESCRIPTION, HASHTAGS, KEYWORDS.",
        user_message=render(load_prompt("seo.md"), {
            "LANGUAGE": chan["language"],
            "TITLE": title,
            "THUMB": thumb,
            "CHANNEL_KEYWORDS": channel_keywords or "(none)",
            "SCRIPT_OPENING": opening,
        }),
        model=cfg["models"]["check"],
        temperature=0.6,
        max_tokens=800,
    )
    pkg = _parse_seo_package(resp.text)
    log(f"[seo] description {len(pkg['seo'])} chars | hashtags: {pkg['hashtags'][:60]} | kw: {pkg['seo_kw'][:60]}")
    return pkg


_LANG_NAMES = {
    "es": "Spanish", "vi": "Vietnamese", "en": "English", "fr": "French",
    "de": "German", "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
    "it": "Italian", "tr": "Turkish",
}


def _load_channel_for_seo(channel_id: str, cfg: dict) -> dict:
    """Load channel; nếu không có file kênh thì dùng ngôn ngữ từ mã T1..T10."""
    try:
        ch = load_channel(channel_id, cfg)
        ch["_inferred"] = False
        return ch
    except Exception:
        pass
    try:
        parts = channel_id.split("-")
        lang_num = parts[-1]  # lấy phần cuối ("T2" từ "KA2-T2")
        lang_code = cfg["languages"].get(lang_num, "en")
        lang_name = _LANG_NAMES.get(lang_code, lang_code)
        return {"id": channel_id, "lang_code": lang_code,
                "language": lang_name, "channel": "", "thumb_case": "upper",
                "_inferred": True}
    except Exception:
        return {"id": channel_id, "lang_code": "en",
                "language": "English", "channel": "", "thumb_case": "upper",
                "_inferred": True}


# ── Backfill SEO cho rows đã có title/thumb ───────────────────────────────────
def backfill_seo_job(job: dict, cfg: dict, api, log=print) -> dict:
    """Tạo SEO cho 1 row. Channel phải có file cấu hình (đã lọc bởi channel_exists)."""
    ma      = job["ma"]
    channel = job["channel"]
    title   = job.get("title", "")
    thumb   = job.get("thumb", "")
    keywords = job.get("keywords", "")

    # Cùng logic với run_job: load_channel() → ngôn ngữ từ TLx.md
    chan = load_channel(channel, cfg)

    # Cố đọc script từ voice folder để làm context tốt hơn
    script = ""
    voice_dir = (
        os.environ.get("CONTENT_VOICE_DIR")
        or cfg.get("output", {}).get("voice_dir", "")
    )
    if voice_dir:
        script_path = os.path.join(voice_dir, f"{ma}.txt")
        try:
            script = _read(script_path)
        except Exception:
            pass

    log(f"[{ma}] SEO backfill — {title[:60]} [{chan['language']}]"
        + (" (có script)" if script else ""))
    pkg = generate_seo_package(api, cfg, chan, title, thumb, keywords, script, log)
    return {"ok": True, "ma": ma, **pkg}


# ── Job chính ─────────────────────────────────────────────────────────────────
def run_job(job: dict, cfg: dict, api, log=print, on_title_thumb=None) -> dict:
    ma = job["ma"]
    channel = job["channel"]
    title = job.get("title", "")
    link = job["link"]

    chan = load_channel(channel, cfg)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, f"{ma}_{channel}_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    log(f"=== JOB {ma} · {channel} · '{title[:50]}' ===")

    comp = youtube.get_transcript(link, run_dir, log=log)
    transcript = comp["transcript"]

    tt = generate_title_thumb(api, cfg, chan, comp.get("title_original", ""), transcript, log)
    if not title:
        title = tt["title"]
    thumb = tt["thumb"]
    with open(os.path.join(run_dir, "title_thumb.txt"), "w", encoding="utf-8") as f:
        f.write(f"TITLE: {title}\nTHUMB: {thumb}\n")
    if on_title_thumb:
        on_title_thumb(ma, title, thumb)

    draft = write_oneshot(api, cfg, chan, transcript, title, log)
    target_chars = target_chars_for(chan, cfg)
    if target_chars:
        # Kênh có target độ dài: write → adapt (ép ký tự) → review (giữ ký tự)
        draft = adapt_oneshot(api, cfg, chan, draft, target_chars, log)
        final = review_oneshot(api, cfg, chan, draft, target_chars, log)
    else:
        # Kênh không khai target: giữ nguyên luồng cũ (write → check_fix)
        final = check_fix_oneshot(api, cfg, chan, transcript, draft, log)
    final = clean_voice_text(final, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"])

    seo_pkg = generate_seo_package(
        api, cfg, chan, title, thumb,
        channel_keywords=job.get("keywords", ""),
        script=final, log=log,
    )

    final_path = os.path.join(run_dir, "final.txt")
    with open(final_path, "w", encoding="utf-8") as f:
        f.write(final)

    final_chars = count_chars(final)
    log(f"=== XONG {ma}: {final_chars:,} ký tự ===")

    ensure_drives(cfg, log=log)
    voice = copy_to_voice(final_path, ma, cfg, log=log)
    return {
        "ok": True,
        "ma": ma,
        "channel": channel,
        "title": title,
        "thumb": thumb,
        "script": final,
        "seo": seo_pkg["seo"],
        "hashtags": seo_pkg["hashtags"],
        "seo_kw": seo_pkg["seo_kw"],
        "final_path": final_path,
        "run_dir": run_dir,
        "chars": final_chars,
        "voice_path": voice.get("path", ""),
        "voice_copy": voice,
    }
