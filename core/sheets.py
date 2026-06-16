"""
sheets.py — Đọc job cần viết và ghi kết quả lên Google Sheet.

Sạch hơn bản TAMLY: mọi chỉ số cột, tên spreadsheet/worksheet đều lấy từ
config.yaml (mục `sheet:`), không hard-code rải rác. Auth bằng creds.json
(service account).
"""
from __future__ import annotations

import os
import re
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ → CONTENT/
CREDS_FILE = os.path.join(_ROOT, "config", "creds.json")

_SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Retry cho lỗi Sheets tạm thời (mạng, 429, 5xx) ──────────────────────────
_TRANSIENT = (
    "remotedisconnected", "remote end closed connection", "timeout", "timed out",
    "temporarily unavailable", "connection aborted", "connection reset",
    "connectionerror", "ssl", "429", "quota", "rate limit", "500", "502", "503", "504",
)


def _is_transient(exc: Exception) -> bool:
    text = repr(exc).lower()
    return any(m in text for m in _TRANSIENT)


def _retry(label: str, func, attempts: int = 8, base_delay: float = 2.0, log=print):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= attempts or not _is_transient(exc):
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 45.0)
            log(f"[sheets] {label} lỗi tạm thời ({attempt}/{attempts}): {exc}; thử lại sau {delay:.0f}s")
            time.sleep(delay)
    raise last  # type: ignore[misc]


# ── Kết nối ─────────────────────────────────────────────────────────────────
def _open(sheet_cfg: dict, log=print):
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, _SCOPE)
    client = _retry("authorize", lambda: gspread.authorize(creds), log=log)
    name = sheet_cfg["spreadsheet_name"]
    return _retry(f"open {name}", lambda: client.open(name), log=log)


def _cell(row: list, idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


# ── Đọc job cần viết ────────────────────────────────────────────────────────
def get_pending(sheet_cfg: dict, log=print) -> list[dict]:
    """
    Trả về danh sách job chưa có content (cột content trống) và đã có channel
    config. Title/thumb/link lấy từ INPUT trước, thiếu thì tra sang NGUON.
    """
    c = sheet_cfg["columns"]
    ss = _open(sheet_cfg, log=log)
    inp = _retry("open INPUT", lambda: ss.worksheet(sheet_cfg["input_sheet"]), log=log)
    rows = _retry("read INPUT", lambda: inp.get_all_values(), log=log)

    # NGUON lookup: ma -> {title, thumb, link, hook}
    nguon: dict[str, dict] = {}
    try:
        nsheet = _retry("open NGUON", lambda: ss.worksheet(sheet_cfg["nguon_sheet"]), log=log)
        ndata = _retry("read NGUON", lambda: nsheet.get_all_values(), log=log)
        nc = sheet_cfg["nguon_columns"]
        for row in ndata[1:]:
            ma = _cell(row, nc["ma"])
            if ma:
                nguon[ma] = {
                    "title": _cell(row, nc["title"]),
                    "thumb": _cell(row, nc["thumb"]),
                    "link": _cell(row, nc["link"]),
                    "hook": _cell(row, nc.get("hook", -1)) if nc.get("hook", -1) >= 0 else "",
                    "keywords": _cell(row, nc["keywords"]) if nc.get("keywords") is not None else "",
                }
    except Exception as exc:  # noqa: BLE001
        log(f"[sheets] Không đọc được NGUON (bỏ qua): {exc}")

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        ma = _cell(row, c["ma"])
        channel = _cell(row, c["channel"])
        seo = _cell(row, c["seo"])
        if not ma or not channel or seo:
            continue
        # Tính hợp lệ của kênh (có file cấu hình trong cây topics/) do run.py lọc

        title = _cell(row, c["title"])
        thumb = _cell(row, c["thumb"])
        link = _cell(row, c["link"])
        info = nguon.get(ma, {})
        title = title or info.get("title", "")
        thumb = thumb or info.get("thumb", "")
        link = link or info.get("link", "")

        pending.append({
            "row": i, "ma": ma, "channel": channel,
            "title": title, "thumb": thumb, "link": link,
            "hook": info.get("hook", ""),
            "keywords": info.get("keywords", ""),
        })
    return pending


# ── Ghi kết quả ─────────────────────────────────────────────────────────────
def _write_title_thumb_to_nguon(ss, sheet_cfg: dict, ma: str, title: str = "", thumb: str = "", log=print) -> dict:
    if not title and not thumb:
        return {"status": "skipped", "ma": ma}

    nc = sheet_cfg["nguon_columns"]
    nsheet = _retry("open NGUON", lambda: ss.worksheet(sheet_cfg["nguon_sheet"]), log=log)
    ndata = _retry("read NGUON for write", lambda: nsheet.get_all_values(), log=log)

    for i, row in enumerate(ndata[1:], start=2):
        if _cell(row, nc["ma"]) == ma:
            if title:
                _retry(f"write {ma} NGUON title", lambda: nsheet.update_cell(i, nc["title"] + 1, title), log=log)
            if thumb:
                _retry(f"write {ma} NGUON thumb", lambda: nsheet.update_cell(i, nc["thumb"] + 1, thumb), log=log)
            log(f"[sheets] Đã ghi title/thumb {ma} vào NGUON dòng {i}")
            return {"status": "ok", "ma": ma, "row": i}

    log(f"[sheets] Không tìm thấy dòng MA={ma} trên NGUON để ghi title/thumb")
    return {"status": "error", "ma": ma, "message": "MA not found on NGUON"}


def write_title_thumb(sheet_cfg: dict, ma: str, title: str = "", thumb: str = "", log=print) -> dict:
    ss = _open(sheet_cfg, log=log)
    return _write_title_thumb_to_nguon(ss, sheet_cfg, ma, title, thumb, log=log)


def write_result(sheet_cfg: dict, ma: str, seo: str = "",
                 hashtags: str = "", seo_kw: str = "", log=print) -> dict:
    """Ghi seo description + hashtags + seo keywords vào INPUT theo MA."""
    c = sheet_cfg["columns"]
    ss = _open(sheet_cfg, log=log)
    inp = _retry("open INPUT", lambda: ss.worksheet(sheet_cfg["input_sheet"]), log=log)
    rows = _retry("read INPUT", lambda: inp.get_all_values(), log=log)

    for i, row in enumerate(rows[1:], start=2):
        if _cell(row, c["ma"]) == ma:
            if seo:
                _retry(f"write {ma} seo", lambda: inp.update_cell(i, c["seo"] + 1, seo), log=log)
            if hashtags:
                _retry(f"write {ma} hashtags", lambda: inp.update_cell(i, c["hashtags"] + 1, hashtags), log=log)
            if seo_kw:
                _retry(f"write {ma} seo_kw", lambda: inp.update_cell(i, c["seo_kw"] + 1, seo_kw), log=log)
            log(f"[sheets] Đã ghi SEO {ma} vào INPUT dòng {i}")
            return {"status": "ok", "ma": ma, "row": i}

    log(f"[sheets] Không tìm thấy dòng MA={ma} trên INPUT")
    return {"status": "error", "ma": ma, "message": "MA not found"}


# giữ alias để không break code cũ
def write_content(sheet_cfg, ma, content="", seo="", title="", thumb="", log=print):
    return write_result(sheet_cfg, ma, seo=seo, log=log)


def parse_video_id(link: str) -> str:
    m = (re.search(r"[?&]v=([A-Za-z0-9_-]{11})", link)
         or re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", link)
         or re.search(r"/shorts/([A-Za-z0-9_-]{11})", link))
    return m.group(1) if m else ""
