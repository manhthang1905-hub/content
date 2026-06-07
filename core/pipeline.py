"""
pipeline.py — Luồng viết content (trái tim của tool).

Luồng 3 bước (học TAMLY, làm sạch):
  ① analyze — đọc đối thủ → dàn ý có phân tích từng phần
  ② write   — viết TỪNG phần theo mục tiêu; sau mỗi phần check, đạt thì chốt
  ③ ghép    — nối các phần + dọn định dạng cho ElevenLabs

Prompt nằm ở prompts/*.md (gốc, sửa tự do). Biến của kênh chèn vào lúc render.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import yaml

import checks
import fetch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ → CONTENT/
PROMPTS_DIR = os.path.join(_ROOT, "prompts")
TOPICS_DIR = os.path.join(_ROOT, "topics")
RUNS_DIR = os.path.join(_ROOT, "output", "runs")


# ── Tiện ích ────────────────────────────────────────────────────────────────
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render(template: str, variables: dict) -> str:
    """Thay <<KEY>> trong template bằng giá trị. An toàn với JSON trong prompt."""
    out = template
    for key, val in variables.items():
        out = out.replace(f"<<{key}>>", str(val))
    return out


def copy_to_voice(final_path: str, ma: str, cfg: dict, log=print) -> dict:
    voice_dir = os.environ.get("CONTENT_VOICE_DIR") or os.environ.get("TAMLY_VOICE_DIR") or cfg.get("output", {}).get("voice_dir") or r"C:\Users\Administrator\Desktop\voice\voice"
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


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tách frontmatter YAML (giữa hai dòng ---) khỏi phần thân .md."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            return fm, parts[2].lstrip("\n")
    return {}, text


def load_prompt(name: str) -> str:
    """3 prompt dùng chung cho mọi kênh."""
    return _read(os.path.join(PROMPTS_DIR, name))


def _channel_paths(channel_id: str, cfg: dict) -> tuple[str, str, str, str]:
    """Mã kênh 'TLx-Ty' → (đường dẫn file kênh, thư mục ngôn ngữ, thư mục topic, mã ngôn ngữ)."""
    style, lang_num = channel_id.split("-")           # "TL1-T2" → "TL1", "T2"
    lang_code = cfg["languages"][lang_num]            # "T2" → "vi"
    topic_dir = os.path.join(TOPICS_DIR, cfg["active_topic"])
    lang_dir = os.path.join(topic_dir, lang_code)
    return os.path.join(lang_dir, f"{style}.md"), lang_dir, topic_dir, lang_code


def channel_exists(channel_id: str, cfg: dict) -> bool:
    try:
        voice_path, _, _, _ = _channel_paths(channel_id, cfg)
    except (KeyError, ValueError):
        return False
    return os.path.exists(voice_path)


def load_channel(channel_id: str, cfg: dict) -> dict:
    """Đi theo cây: topics/{topic}/{ngôn ngữ}/{kênh}.md + insight.md + topic.md."""
    voice_path, lang_dir, topic_dir, lang_code = _channel_paths(channel_id, cfg)

    # Lớp ngôn ngữ = insight.md (frontmatter: language, thumb_case; thân: insight khán giả)
    fm, insight_body = _parse_frontmatter(_read(os.path.join(lang_dir, "insight.md")))

    # File kênh = TLx.md (frontmatter: title_thumb mode…; thân: văn phong)
    vfm, voice_body = _parse_frontmatter(_read(voice_path))

    topic_path = os.path.join(topic_dir, "topic.md")
    topic_note = _read(topic_path) if os.path.exists(topic_path) else ""

    return {
        "id": channel_id,
        "lang_code": lang_code,
        "language": fm.get("language", ""),
        "audience": insight_body.strip(),       # insight khán giả → nhồi vào prompt
        "thumb_case": fm.get("thumb_case", "upper"),
        "_voice": voice_body.strip(),
        "title_thumb_mode": vfm.get("title_thumb", "restyled"),
        "duration_minutes": int(vfm.get("duration_minutes", 0) or 0),
        "_topic_guide": topic_note,
    }


def duration_budget(chan: dict, cfg: dict) -> dict:
    minutes = int(chan.get("duration_minutes") or 0)
    lang_code = chan.get("lang_code", "")
    rates = cfg.get("duration_char_rates", {}) or {}
    chars_per_minute = int(rates.get(lang_code) or 1000)
    target = max(1, minutes * chars_per_minute)
    tolerance = int(cfg.get("duration_tolerance_pct", 25) or 25)
    return {
        "duration_minutes": minutes,
        "lang_code": lang_code,
        "chars_per_minute": chars_per_minute,
        "target_chars": target,
        "min_chars": int(target * (1 - tolerance / 100)),
        "max_chars": int(target * (1 + tolerance / 100)),
        "tolerance_pct": tolerance,
    }


def allocate_part_targets(parts: list[dict], budget: dict) -> list[dict]:
    if not parts:
        return parts
    target_total = int(budget.get("target_chars") or 0)
    tolerance = int(budget.get("tolerance_pct") or 25)
    lengths = [max(1, checks.count_chars(p.get("competitor_excerpt", ""))) for p in parts]
    length_sum = sum(lengths) or len(parts)
    allocated = [max(1, int(target_total * length / length_sum)) for length in lengths]
    drift = target_total - sum(allocated)
    if allocated:
        allocated[-1] += drift
    for part, target in zip(parts, allocated):
        target = max(1, int(target))
        part["target_chars"] = target
        part["min_chars"] = max(1, int(target * (1 - tolerance / 100)))
        part["max_chars"] = max(part["min_chars"], int(target * (1 + tolerance / 100)))
    return parts


def _score_10(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 10 and score <= 100:
        score = score / 10
    return max(0.0, min(10.0, score))


def _candidate_rank(cand: dict, target: int) -> tuple:
    length = cand.get("length", {})
    length_ok = bool(length.get("ok"))
    passed = bool(cand.get("verdict", {}).get("pass"))
    chars = int(cand.get("chars") or 0)
    distance = abs(chars - target) if target else 0
    return (length_ok, passed, cand.get("score", 0), -distance)


CANDIDATE_DIRECTIONS = [
    "Bản này ưu tiên bám sát đoạn gốc nhất: cùng mạch, cùng cảm xúc, cùng cách dẫn, chỉ đổi câu chữ và bản địa hóa tự nhiên.",
    "Bản này ưu tiên nghe tự nhiên và gần khán giả kênh nhất: đời thường, có cảm xúc, không dịch máy, không xa lạ.",
    "Bản này ưu tiên gọn và sắc nhất: mỗi ý chỉ nói một lần, cắt diễn giải vòng, giữ đúng khoảng ký tự yêu cầu.",
]


COMPRESS_DIRECTION = "Bản này bắt buộc nén lại còn tối đa {max_chars} ký tự và gần {target_chars} ký tự: giữ các ý chính của đoạn gốc, bỏ câu giải thích lặp, bỏ ví dụ phụ, mỗi nhịp chỉ nói một lần."


def _candidate_direction(attempt: int) -> str:
    if attempt <= len(CANDIDATE_DIRECTIONS):
        return CANDIDATE_DIRECTIONS[attempt - 1]
    return CANDIDATE_DIRECTIONS[-1]


# ── Tiêu đề + text thumb ────────────────────────────────────────────────────
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
    """Tạo tiêu đề + text thumb mới cho kênh (faithful: chuyển ngữ sát đối thủ;
    restyled: viết lại theo văn phong). Thumb viết hoa giữ dấu, tiêu đề ≤100 ký tự."""
    log("[title/thumb] Tạo tiêu đề + text thumb...")
    variables = {
        "COMPETITOR_TITLE": competitor_title,
        "TRANSCRIPT_SAMPLE": transcript[:600],
        "LANGUAGE": chan["language"],
        "AUDIENCE": chan["audience"],
        "MODE": chan.get("title_thumb_mode", "restyled"),
        "VOICE": chan["_voice"],
        "CASING": checks.casing_instruction(chan.get("thumb_case", "upper")),
    }
    resp = api.call(
        stage="check",
        system="Bạn tạo tiêu đề + text thumb. Trả về ĐÚNG 2 dòng: TITLE: và THUMB:.",
        user_message=render(load_prompt("title_thumb.md"), variables),
        model=cfg["models"]["check"],
        temperature=0.7,
        max_tokens=300,
    )
    title, thumb = _parse_title_thumb(resp.text)
    title = checks.trim_title(title, 100)
    thumb = checks.apply_thumb_case(thumb, chan.get("thumb_case", "upper"))
    log(f"[title/thumb] TITLE: {title[:60]} | THUMB: {thumb[:40]}")
    return {"title": title, "thumb": thumb, "verdict": {}}


def evaluate_against_competitor(api, cfg, chan, comp, title, thumb, script, log) -> dict:
    """Final check toàn bài bằng prompt check.md, không dùng judge riêng."""
    log("[quality] Final check toàn bài theo góc nhìn khán giả kênh...")
    budget = chan.get("length_budget", {})
    verdict = api.call_json(
        stage="check",
        system="Bạn là khán giả phổ thông khó tính. Trả về đúng JSON.",
        user_message=render(load_prompt("check.md"), {
            "CHECK_SCOPE": f"Toàn bài sau khi ghép. Title: {title}. Thumb: {thumb}.",
            "ORIGINAL_TEXT": comp.get("transcript", ""),
            "NEW_TEXT": script,
            "LANGUAGE": chan["language"],
            "AUDIENCE": chan["audience"],
            "VOICE": chan["_voice"],
            "TARGET_CHARS": budget.get("target_chars", ""),
            "MIN_CHARS": budget.get("min_chars", ""),
            "MAX_CHARS": budget.get("max_chars", ""),
        }),
        model=cfg["models"]["check"],
        max_tokens=1200,
    )
    score = _score_10(verdict.get("score", 0))
    verdict["score"] = score
    verdict["ready_for_voice"] = bool(verdict.get("pass")) and score >= cfg["pipeline"]["pass_score"]
    verdict["content_score"] = score
    log(f"[quality] CONTENT {score}/10 · "
        f"{'sẵn sàng voice' if verdict.get('ready_for_voice') else 'cần xem lại'}")
    return verdict


# ── Bước 1: phân tích đối thủ → dàn ý ───────────────────────────────────────
def analyze_competitor(api, cfg, chan, title, transcript, log) -> dict:
    prompt = render(load_prompt("analyze.md"), {
        "TITLE": title,
        "LANGUAGE": chan["language"],
        "AUDIENCE": chan["audience"],
        "TOPIC_GUIDE": chan.get("_topic_guide", ""),
        "MAX_PARTS": cfg["pipeline"]["max_parts"],
        "COMPETITOR_TRANSCRIPT": transcript,
    })
    log("[1/3] Tách kịch bản đối thủ thành các khúc...")
    data = api.call_json(
        stage="analyze",
        system="Bạn là biên tập viên content giỏi. Trả về đúng JSON theo yêu cầu.",
        user_message=prompt,
        model=cfg["models"]["analyze"],
    )
    parts = data.get("parts", [])
    if not parts:
        raise RuntimeError("Bước phân tích không trả về khúc nào (parts rỗng).")
    log(f"      {len(parts)} khúc — {data.get('why_viral','')[:80]}")
    return data


# ── Bước 2: viết 1 khúc, bám sát đối thủ ─────────────────────────────────────
def _write_candidate(api, cfg, chan, part, idx, total, prev_context, log, write_tpl, check_tpl, direction, attempt) -> dict:
    pass_score = cfg["pipeline"]["pass_score"]
    name = part.get("name", f"Khúc {idx}")
    excerpt = part.get("competitor_excerpt", "")
    wvars = {
        "PART_INDEX": idx, "TOTAL_PARTS": total,
        "PART_NAME": name,
        "PART_ROLE": part.get("role", ""),
        "PART_EXCERPT": excerpt,
        "PART_KEEP": part.get("keep", ""),
        "PART_TECHNIQUE": part.get("technique", ""),
        "VOICE": chan["_voice"],
        "LANGUAGE": chan["language"],
        "AUDIENCE": chan["audience"],
        "PART_TARGET_CHARS": part.get("target_chars", ""),
        "PART_MIN_CHARS": part.get("min_chars", ""),
        "PART_MAX_CHARS": part.get("max_chars", ""),
        "PREVIOUS_CONTEXT": prev_context or "(đây là khúc đầu tiên)",
    }
    prompt = render(write_tpl, wvars)
    prompt += f"\n\nƯu tiên riêng cho bản viết này: {direction}"

    resp = api.call(
        stage="write",
        system="Bạn là người viết kịch bản voiceover triệu view. Chỉ xuất lời thoại.",
        user_message=prompt,
        model=cfg["models"]["write"],
        temperature=0.8,
    )
    text = checks.clean_voice_text(
        resp.text,
        blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"],
    )
    n = checks.count_chars(text)

    cvars = {
        "CHECK_SCOPE": f"Một khúc: {name}. Chỉ so khúc này với đoạn gốc tương ứng.",
        "ORIGINAL_TEXT": excerpt,
        "NEW_TEXT": text,
        "LANGUAGE": chan["language"],
        "AUDIENCE": chan["audience"],
        "VOICE": chan["_voice"],
        "TARGET_CHARS": part.get("target_chars", ""),
        "MIN_CHARS": part.get("min_chars", ""),
        "MAX_CHARS": part.get("max_chars", ""),
    }
    verdict = api.call_json(
        stage="check",
        system="Bạn là khán giả phổ thông khó tính. Trả về đúng JSON.",
        user_message=render(check_tpl, cvars),
        model=cfg["models"]["check"],
    )
    score = _score_10(verdict.get("score", 0))
    length = checks.length_status(text, int(part.get("target_chars") or n or 1), int(chan.get("length_budget", {}).get("tolerance_pct", 25)))
    length_ok = length["ok"]
    passed = verdict.get("pass", False) and score >= pass_score
    length_note = f"{n}/{length['target']} ký tự (mục tiêu {length['min']}-{length['max']})"
    log(f"      Khúc {idx}/{total} '{name}' — lần {attempt}: "
        f"{length_note}, điểm {score}/10 {'✓ ĐẠT' if passed and length_ok else '✗ chưa'}")

    return {"text": text, "chars": n, "length": length, "score": score, "verdict": verdict, "attempt": attempt}


def write_part(api, cfg, chan, part, idx, total, prev_context, log) -> dict:
    write_tpl = load_prompt("write.md")
    check_tpl = load_prompt("check.md")
    pass_score = cfg["pipeline"]["pass_score"]
    max_rw = cfg["pipeline"]["max_rewrites_per_part"]

    name = part.get("name", f"Khúc {idx}")
    excerpt = part.get("competitor_excerpt", "")

    best = None
    for attempt in range(1, max_rw + 2):  # viết vài bản độc lập rồi chọn bản tốt nhất
        direction = _candidate_direction(attempt)
        cand = _write_candidate(api, cfg, chan, part, idx, total, prev_context, log, write_tpl, check_tpl, direction, attempt)
        if best is None or _candidate_rank(cand, cand["length"]["target"]) > _candidate_rank(best, best["length"]["target"]):
            best = cand

    if best and not best["length"].get("ok"):
        attempt = max_rw + 2
        direction = COMPRESS_DIRECTION.format(
            target_chars=part.get("target_chars", ""),
            max_chars=part.get("max_chars", ""),
        )
        cand = _write_candidate(api, cfg, chan, part, idx, total, prev_context, log, write_tpl, check_tpl, direction, attempt)
        if _candidate_rank(cand, cand["length"]["target"]) > _candidate_rank(best, best["length"]["target"]):
            best = cand

    log(f"      Khúc {idx} chọn bản tốt nhất (điểm {best['score']}/10, {best['chars']}/{best['length']['target']} ký tự)")
    return best


# ── Luồng đầy đủ cho 1 job ──────────────────────────────────────────────────
def run_job(job: dict, cfg: dict, api, log=print, on_title_thumb=None) -> dict:
    ma = job["ma"]
    channel = job["channel"]
    title = job.get("title", "")
    link = job["link"]

    chan = load_channel(channel, cfg)
    chan["length_budget"] = duration_budget(chan, cfg)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, f"{ma}_{channel}_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    log(f"=== JOB {ma} · {channel} · '{title[:50]}' ===")
    log(f"    run_dir: {run_dir}")

    # Lấy transcript đối thủ — lưu luôn vào thư mục run của job (gọn, tự chứa)
    comp = fetch.get_transcript(link, run_dir, log=log)
    transcript = comp["transcript"]

    # Tạo tiêu đề + text thumb mới cho kênh (từ tiêu đề đối thủ đã viral)
    tt = generate_title_thumb(api, cfg, chan, comp.get("title_original", ""), transcript, log)
    if not title:
        title = tt["title"]              # tiêu đề mới làm tiêu đề video luôn
    thumb = tt["thumb"]
    with open(os.path.join(run_dir, "title_thumb.txt"), "w", encoding="utf-8") as f:
        f.write(f"TITLE: {title}\nTHUMB: {thumb}\n")
    if on_title_thumb:
        on_title_thumb(ma, title, thumb)

    # ① Tách đối thủ thành các khúc (kèm excerpt nguyên văn)
    analysis = analyze_competitor(api, cfg, chan, title, transcript, log)
    parts = allocate_part_targets(analysis["parts"], chan["length_budget"])
    analysis["parts"] = parts
    analysis["length_budget"] = chan["length_budget"]
    with open(os.path.join(run_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    # ② Viết lại từng khúc, bám sát đối thủ
    total = len(parts)
    written: list[dict] = []
    assembled = ""
    ctx_chars = cfg["pipeline"]["context_chars_per_part"]
    log("[2/3] Viết lại từng khúc (bám sát đối thủ)...")
    for i, part in enumerate(parts, start=1):
        prev_context = assembled[-ctx_chars:] if assembled else ""
        result = write_part(api, cfg, chan, part, i, total, prev_context, log)
        written.append({"part": part, **result})
        assembled = (assembled + "\n\n" + result["text"]).strip()
        with open(os.path.join(run_dir, f"part_{i:02d}.txt"), "w", encoding="utf-8") as f:
            f.write(result["text"])

    # Lưu verdict của check (minh bạch — soi check chấm gì)
    review = [{
        "id": w["part"].get("id", i + 1),
        "name": w["part"].get("name", ""),
        "score": w["score"],
        "attempts": w["attempt"],
        "chars": w["chars"],
        "target_chars": w["part"].get("target_chars"),
        "min_chars": w["part"].get("min_chars"),
        "max_chars": w["part"].get("max_chars"),
        "length_ok": w.get("length", {}).get("ok"),
        "pass": w["verdict"].get("pass"),
        "fix": w["verdict"].get("fix", ""),
    } for i, w in enumerate(written)]
    with open(os.path.join(run_dir, "review.json"), "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)

    # ③ Ghép + dọn định dạng
    log("[3/3] Ghép & dọn định dạng cho ElevenLabs...")
    final = checks.clean_voice_text(
        assembled, blank_line_between_paragraphs=cfg["output"]["blank_line_between_paragraphs"]
    )
    final_path = os.path.join(run_dir, "final.txt")
    with open(final_path, "w", encoding="utf-8") as f:
        f.write(final)

    budget = chan["length_budget"]
    final_chars = checks.count_chars(final)
    length = checks.length_status(final, budget["target_chars"], budget["tolerance_pct"])
    avg_score = round(sum(w["score"] for w in written) / max(1, len(written)), 1)
    quality = evaluate_against_competitor(api, cfg, chan, comp, title, thumb, final, log)
    with open(os.path.join(run_dir, "final_check.json"), "w", encoding="utf-8") as f:
        json.dump(quality, f, ensure_ascii=False, indent=2)
    log(f"=== XONG {ma}: {final_chars:,}/{budget['target_chars']:,} ký tự "
        f"(mục tiêu {budget['duration_minutes']} phút · khoảng {budget['min_chars']:,}-{budget['max_chars']:,} · "
        f"tỉ lệ {length['ratio']}x) · điểm TB {avg_score}/10 · quality {quality.get('content_score', 0)}/10 ===")

    voice = copy_to_voice(final_path, ma, cfg, log=log)

    return {
        "ok": True,
        "ma": ma,
        "channel": channel,
        "title": title,
        "thumb": thumb,
        "script": final,
        "final_path": final_path,
        "run_dir": run_dir,
        "chars": final_chars,
        "target_chars": budget["target_chars"],
        "min_chars": budget["min_chars"],
        "max_chars": budget["max_chars"],
        "length_ok": length["ok"],
        "duration_minutes": budget["duration_minutes"],
        "length_ratio": length["ratio"],
        "avg_score": avg_score,
        "quality_score": quality.get("content_score", 0),
        "ready_for_voice": quality.get("ready_for_voice", False),
        "voice_path": voice.get("path", ""),
        "voice_copy": voice,
        "quality": quality,
        "parts": written,
        "analysis": analysis,
    }
