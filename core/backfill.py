from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent  # core/ → CONTENT/
sys.path.insert(0, str(ROOT / "core"))

import yaml  # noqa: E402
import sheets  # noqa: E402


def load_config() -> dict:
    with open(ROOT / "config" / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_title_thumb(path: Path) -> tuple[str, str]:
    title = ""
    thumb = ""
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
        elif line.upper().startswith("THUMB:"):
            thumb = line.split(":", 1)[1].strip()
    return title, thumb


def run_key(run_dir: Path) -> tuple[str, str]:
    m = re.match(r"(TL\d+-\d+)_(.+?)_\d{8}_\d{6}$", run_dir.name)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def latest_complete_runs() -> dict[str, Path]:
    runs: dict[str, Path] = {}
    for run_dir in (ROOT / "output" / "runs").iterdir():
        if not run_dir.is_dir():
            continue
        ma, _channel = run_key(run_dir)
        if not ma:
            continue
        if not (run_dir / "final.txt").exists():
            continue
        current = runs.get(ma)
        if current is None or run_dir.name > current.name:
            runs[ma] = run_dir
    return runs


def copy_voice(ma: str, final_path: Path, voice_dir: Path) -> bool:
    voice_dir.mkdir(parents=True, exist_ok=True)
    dst = voice_dir / f"{ma}.txt"
    shutil.copy2(str(final_path), str(dst))
    return dst.exists() and dst.stat().st_size > 0


def main() -> int:
    cfg = load_config()
    voice_dir = Path(
        os.environ.get("CONTENT_VOICE_DIR")
        or os.environ.get("TAMLY_VOICE_DIR")
        or cfg.get("output", {}).get("voice_dir")
        or r"C:\Users\Administrator\Desktop\voice\voice"
    )
    runs = latest_complete_runs()
    nguon_ok = 0
    voice_ok = 0
    skipped_title = 0
    errors: list[dict] = []

    for ma, run_dir in sorted(runs.items()):
        title_path = run_dir / "title_thumb.txt"
        if title_path.exists():
            title, thumb = parse_title_thumb(title_path)
            if title or thumb:
                try:
                    res = sheets.write_title_thumb(cfg["sheet"], ma, title, thumb, log=print)
                    if res.get("status") == "ok":
                        nguon_ok += 1
                    else:
                        errors.append({"ma": ma, "step": "nguon", "message": res})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"ma": ma, "step": "nguon", "message": str(exc)})
            else:
                skipped_title += 1
        else:
            skipped_title += 1

        try:
            if copy_voice(ma, run_dir / "final.txt", voice_dir):
                voice_ok += 1
                print(f"[voice] {ma} -> {voice_dir / (ma + '.txt')}")
        except Exception as exc:  # noqa: BLE001
            errors.append({"ma": ma, "step": "voice", "message": str(exc)})

    summary = {
        "runs": len(runs),
        "nguon_updated": nguon_ok,
        "voice_copied": voice_ok,
        "missing_title_thumb": skipped_title,
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
