"""
checks.py — Dọn định dạng đầu ra cho giọng đọc ElevenLabs + đo độ dài.

Đây là phần "ngọn" (deterministic), nhẹ: bỏ rác markdown, đảm bảo mỗi câu một
dòng, không dính chữ, có nhịp nghỉ giữa đoạn. Không phán xét chất lượng — việc
đó là của prompt (gốc).
"""
from __future__ import annotations

import re

# Dấu câu kết thúc câu (đa ngôn ngữ: Latin + CJK)
_SENTENCE_END = ".!?…。！？"


def clean_voice_text(text: str, blank_line_between_paragraphs: bool = True) -> str:
    """Chuẩn hóa text để giọng đọc tự nhiên, không dính chữ, không markdown."""
    if not text:
        return ""

    # 1) Bỏ rác markdown / nhãn
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)   # **đậm** / *nghiêng*
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # ## tiêu đề
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)  # gạch đầu dòng
    text = text.replace("—", ", ").replace(" – ", ", ")   # em/en dash → nghỉ ngắn

    # 2) Tách từng dòng, bỏ khoảng trắng thừa
    lines = [ln.strip() for ln in text.split("\n")]

    # 3) Đảm bảo không dính chữ: thêm khoảng trắng sau dấu câu nếu bị dính
    fixed = []
    for ln in lines:
        ln = re.sub(rf"([{re.escape(_SENTENCE_END)}])([A-Za-zÀ-ÿ¿¡])", r"\1 \2", ln)
        ln = re.sub(r"[ \t]{2,}", " ", ln)
        fixed.append(ln)

    # 4) Gộp tối đa 1 dòng trống liên tiếp (giữ nhịp nghỉ giữa đoạn)
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

    # Bỏ dòng trống ở đầu/cuối
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()

    return "\n".join(out)


def count_chars(text: str) -> int:
    """Đếm ký tự (bỏ khoảng trắng đầu/cuối)."""
    return len((text or "").strip())


def length_status(text: str, target: int, tolerance_pct: int = 20) -> dict:
    """So độ dài thực với mục tiêu. Trả về dict {chars, target, ok, ratio}."""
    n = count_chars(text)
    lo = int(target * (1 - tolerance_pct / 100))
    hi = int(target * (1 + tolerance_pct / 100))
    return {
        "chars": n,
        "target": target,
        "min": lo,
        "max": hi,
        "ok": lo <= n <= hi,
        "ratio": round(n / target, 2) if target else 0,
    }


# ── Tiêu đề + text thumb ────────────────────────────────────────────────────
def apply_thumb_case(text: str, style: str = "upper") -> str:
    """Viết hoa text thumb GIỮ DẤU (str.upper của Python là Unicode-aware, không
    ASCII-fold: "đã trải" → "ĐÃ TRẢI", "não é" → "NÃO É"). Theo từng kiểu ngôn ngữ."""
    text = (text or "").strip()
    if style == "preserve":          # Nhật/Hàn: không có chữ hoa, giữ nguyên
        return text
    if style == "turkish_upper":     # Thổ: i→İ, ı→I rồi viết hoa, giữ ç ğ ö ş ü
        return text.translate(str.maketrans({"i": "İ", "ı": "I"})).upper()
    return text.upper()              # Latin (es, vi, en, fr, de, pt, it): viết hoa giữ dấu


def casing_instruction(style: str) -> str:
    """Chỉ dẫn casing cho prompt — nói rõ GIỮ DẤU để model không strip về ASCII."""
    s = (style or "upper").strip().lower()
    if s == "preserve":
        return ("giữ nguyên dạng chữ tự nhiên với đầy đủ dấu (ngôn ngữ này không dùng "
                "chữ in hoa toàn bộ).")
    if s == "turkish_upper":
        return ("VIẾT HOA theo quy tắc tiếng Thổ (i→İ, ı→I) và GIỮ mọi ký tự Thổ "
                "(ç ğ ö ş ü). Không bao giờ bỏ dấu hay đổi về ASCII.")
    return ("VIẾT HOA toàn bộ nhưng GIỮ NGUYÊN mọi dấu của ngôn ngữ "
            "(không bao giờ bỏ dấu, không đổi về ASCII trơn).")


def trim_title(title: str, max_chars: int = 100) -> str:
    """Cắt tiêu đề về ≤ max_chars ở ranh giới từ."""
    t = (title or "").strip().strip('"').strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars].rsplit(" ", 1)[0].strip()
    return cut or t[:max_chars].strip()
