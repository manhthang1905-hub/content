"""
run.py — Điểm chạy duy nhất của CONTENT.

Cách dùng:
  # Test 1 link trực tiếp (KHÔNG ghi Sheet trừ khi thêm --write-sheet)
  python run.py --link "https://youtu.be/XXXX" --channel TL1-T1 --title "..."

  # Chạy theo Google Sheet (đọc job chưa viết → viết → ghi Sheet)
  python run.py --queue [--limit N]
  python run.py --ma TL1-0001            # chạy đúng 1 job theo mã trên Sheet
"""
from __future__ import annotations

import argparse
import os
import sys

sys.dont_write_bytecode = True  # không sinh __pycache__/*.pyc cho gọn
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "core"))


# ── Tee: mọi thứ in ra console đồng thời lưu output/logs/run_YYYYMMDD.log ────
class _Tee:
    """Bọc stdout/stderr: vừa in console vừa ghi file log để chẩn đoán về sau."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass

    def reconfigure(self, **kwargs):  # youtube.py gọi reconfigure lúc import
        pass


def _setup_file_log() -> None:
    from datetime import datetime
    log_dir = os.path.join(_ROOT, "output", "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"run_{datetime.now():%Y%m%d}.log")
    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n=== run.py start {datetime.now():%Y-%m-%d %H:%M:%S} · argv: {' '.join(sys.argv[1:])} ===\n")
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)


_setup_file_log()


def _load_env() -> None:
    """Nạp .env (KEY=VALUE) vào os.environ trước khi import api."""
    path = os.path.join(_ROOT, "config", ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


_load_env()

import yaml  # noqa: E402

import api as api_mod  # noqa: E402
import pipeline  # noqa: E402
import sheets  # noqa: E402


def load_config() -> dict:
    with open(os.path.join(_ROOT, "config", "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_api(cfg: dict):
    return api_mod.make_client(cfg, log_fn=lambda m: print(m, flush=True))


def run_one(job: dict, cfg: dict, api, write_sheet: bool) -> dict:
    def on_title_thumb(ma: str, title: str, thumb: str) -> None:
        if write_sheet:
            sheets.write_title_thumb(cfg["sheet"], ma, title, thumb, log=lambda m: print(m, flush=True))

    try:
        result = pipeline.run_job(job, cfg, api, log=lambda m: print(m, flush=True), on_title_thumb=on_title_thumb)
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI] {job.get('ma','?')}: {exc}", flush=True)
        return {"ok": False, "ma": job.get("ma"), "error": str(exc)}

    if write_sheet and result.get("ok"):
        sheets.write_result(cfg["sheet"], result["ma"],
                            seo=result.get("seo", ""),
                            hashtags=result.get("hashtags", ""),
                            seo_kw=result.get("seo_kw", ""),
                            log=lambda m: print(m, flush=True))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="CONTENT — viết lại kịch bản bám đối thủ")
    ap.add_argument("--link", help="Link đối thủ (chế độ test trực tiếp)")
    ap.add_argument("--channel", help="Mã kênh, vd TL1-T1")
    ap.add_argument("--title", default="", help="Tiêu đề video mới (tùy chọn)")
    ap.add_argument("--ma", help="Chạy đúng 1 job theo mã trên Sheet")
    ap.add_argument("--queue", action="store_true", help="Chạy mọi job chưa viết trên Sheet")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số job khi --queue")
    ap.add_argument("--write-sheet", action="store_true",
                    help="Ghi kết quả lên Sheet (chế độ --link mặc định KHÔNG ghi)")
    ap.add_argument("--backfill-seo", action="store_true",
                    help="Tạo lại SEO/hashtag/keywords cho rows chưa đăng (AV trống hoặc EDIT XONG)")
    ap.add_argument("--creds-file", help="Override creds JSON (vd: config/creds_mt.json)")
    ap.add_argument("--sheet-name", help="Override tên spreadsheet (vd: MT)")
    ap.add_argument("--topic", help="Override active topic (vd: success, tai-chinh)")
    args = ap.parse_args()

    cfg = load_config()
    pipeline.cleanup_garbage(log=lambda m: print(m, flush=True))
    # Override config từ CLI flags (dùng cho multi-sheet backfill)
    if args.creds_file:
        cfg["sheet"]["creds_file"] = os.path.join(_ROOT, args.creds_file) if not os.path.isabs(args.creds_file) else args.creds_file
    if args.sheet_name:
        cfg["sheet"]["spreadsheet_name"] = args.sheet_name
    if args.topic:
        cfg["active_topic"] = args.topic
    api = make_api(cfg)

    # ── Chế độ test trực tiếp 1 link ──
    if args.link:
        if not args.channel:
            ap.error("--link cần kèm --channel")
        job = {"ma": args.ma or "TEST", "channel": args.channel,
               "title": args.title, "link": args.link}
        res = run_one(job, cfg, api, write_sheet=args.write_sheet)
        if res.get("ok"):
            print(f"\n→ Kết quả: {res['final_path']}")
        sys.exit(0 if res.get("ok") else 1)

    # ── Chế độ Sheet ──
    if args.queue or args.ma:
        print("[Sheet] Đọc job chưa viết...", flush=True)
        pending = sheets.get_pending(cfg["sheet"], log=lambda m: print(m, flush=True))
        if args.ma:
            pending = [j for j in pending if j["ma"] == args.ma]
        # Chỉ chạy job có kênh hợp lệ trong cây topic active
        skipped = [j for j in pending if not pipeline.channel_exists(j["channel"], cfg)]
        pending = [j for j in pending if pipeline.channel_exists(j["channel"], cfg)]
        if skipped:
            print(f"[Sheet] Bỏ qua {len(skipped)} job không có cấu hình kênh trong topic '{cfg['active_topic']}'", flush=True)
        if args.limit and args.limit > 0:
            pending = pending[: args.limit]
        print(f"[Sheet] {len(pending)} job sẽ chạy", flush=True)
        ok = 0
        for job in pending:
            res = run_one(job, cfg, api, write_sheet=True)
            ok += 1 if res.get("ok") else 0
        print(f"\n→ Hoàn tất: {ok}/{len(pending)} job thành công")
        sys.exit(0)

    # ── Backfill SEO ──
    if args.backfill_seo:
        print("[SEO] Đọc rows cần backfill...", flush=True)
        pending = sheets.get_seo_backfill_pending(cfg["sheet"], log=lambda m: print(m, flush=True))
        # Chỉ xử lý kênh có file cấu hình — giống logic --queue
        skipped = [j for j in pending if not pipeline.channel_exists(j["channel"], cfg)]
        pending = [j for j in pending if pipeline.channel_exists(j["channel"], cfg)]
        if skipped:
            print(f"[SEO] Bỏ qua {len(skipped)} rows không có file kênh trong topic '{cfg['active_topic']}'", flush=True)
        if args.limit and args.limit > 0:
            pending = pending[: args.limit]
        print(f"[SEO] {len(pending)} rows sẽ xử lý", flush=True)
        ok = 0
        for job in pending:
            try:
                result = pipeline.backfill_seo_job(job, cfg, api, log=lambda m: print(m, flush=True))
                if result.get("ok"):
                    sheets.write_result(cfg["sheet"], result["ma"],
                                        seo=result.get("seo", ""),
                                        hashtags=result.get("hashtags", ""),
                                        seo_kw=result.get("seo_kw", ""),
                                        log=lambda m: print(m, flush=True))
                    ok += 1
            except Exception as exc:
                print(f"[SEO] Lỗi {job['ma']}: {exc}", flush=True)
        print(f"\n→ Hoàn tất SEO backfill: {ok}/{len(pending)} thành công")
        sys.exit(0)

    ap.error("Cần --link (test) hoặc --queue / --ma / --backfill-seo (chạy theo Sheet)")


if __name__ == "__main__":
    main()
