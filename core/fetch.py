"""
fetch.py — Lấy kịch bản (transcript) đối thủ từ 1 link YouTube.

Đây là lớp mỏng bọc quanh tools/fetch_video_data.py (đã rất chắc, có 4 phương án
cascade: youtube-transcript-api → yt-dlp → browser → Whisper). Pipeline chỉ cần
gọi get_transcript(link) và nhận lại transcript + tiêu đề gốc.
"""
from __future__ import annotations

import os
import sys

# youtube.py nằm cùng thư mục core/ (đã có trên sys.path)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import youtube  # noqa: E402
from youtube import fetch_video_data, extract_video_id  # noqa: E402


def get_video_id(link: str) -> str:
    """Rút video_id từ 1 link/ID YouTube."""
    return extract_video_id(link)


def get_transcript(link: str, out_dir: str, force: bool = False, log=print) -> dict:
    """
    Lấy transcript đối thủ và lưu LUÔN vào out_dir (thư mục run của job) — để
    transcript nằm gọn cùng kịch bản của job đó. Trả về dict { video_id, title_original,
    transcript, transcript_lang, transcript_word_count, ... }.
    """
    youtube.CACHE_DIR = out_dir                 # transcript {video_id}.json lưu vào run dir
    os.makedirs(out_dir, exist_ok=True)
    log(f"[fetch] Lấy transcript đối thủ: {link}")
    data = fetch_video_data(link, force=force)
    if not data.get("transcript"):
        errs = " | ".join(data.get("fetch_errors", [])[:3]) or "không rõ"
        raise RuntimeError(f"Không lấy được transcript từ {link}. Lỗi: {errs}")
    log(
        f"[fetch] OK — {data['transcript_word_count']} từ "
        f"[{data.get('transcript_lang','?')}] · tiêu đề gốc: {data.get('title_original','')[:60]}"
    )
    return data
