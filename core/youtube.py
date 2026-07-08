"""
fetch_video_data.py — Lấy metadata + transcript từ YouTube video
với 4 phương án cascade, đảm bảo luôn lấy được nội dung.

Usage:
  python fetch_video_data.py <url_or_video_id> [--force] [--method N]

Phương án cascade:
  1. youtube-transcript-api     (nhanh, free, cần có sub)
  2. yt-dlp subtitle download   (free, dễ bị 429)
  3. DrissionPage browser       (browser thật, rất tin cậy)
  4. yt-dlp audio + Whisper API (fallback cuối, cần OPENAI_API_KEY)

Output: ./input_cache/{video_id}.json
"""

import sys, os, re, json, html, time, urllib.request, threading
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_WHISPER_LOCK = threading.Lock()  # chỉ 1 Whisper job/lúc trên RAM thấp

CONTENT_DIR = os.environ.get("CONTENT_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # core/ → CONTENT/
# Nơi lưu transcript đối thủ. Mặc định = output/runs; fetch.py trỏ vào đúng thư mục
# run của từng job để transcript nằm gọn cùng kịch bản của job đó.
CACHE_DIR = os.path.join(CONTENT_DIR, "output", "runs")

# Load API keys
_api_keys_file = os.path.join(CONTENT_DIR, 'api_keys.json')
_api_keys = {}
if os.path.exists(_api_keys_file):
    with open(_api_keys_file, 'r', encoding='utf-8') as _f:
        _api_keys = json.load(_f)

YT_API_KEY    = _api_keys.get('youtube_api_key', '') or os.environ.get('YOUTUBE_API_KEY', '')
OPENAI_API_KEY = _api_keys.get('openai_api_key', '') or os.environ.get('OPENAI_API_KEY', '')
OPENAI_API_BASE = _api_keys.get('openai_api_base', '') or 'https://api.openai.com/v1'

# Fallback: đọc OPENAI_API_KEY từ 11lab_vm config nếu chưa có
if not OPENAI_API_KEY:
    _11lab_cfg = os.path.join(os.path.dirname(CONTENT_DIR), 'voice', '11lab_vm', 'config', 'config.json')
    if os.path.exists(_11lab_cfg):
        try:
            with open(_11lab_cfg, 'r', encoding='utf-8') as _f:
                OPENAI_API_KEY = json.load(_f).get('OPENAI_API_KEY', '')
        except Exception:
            pass

# Optional cookies file to bypass YouTube IP restrictions on cloud servers.
# Tìm theo thứ tự ưu tiên; file đầu tiên tìm thấy sẽ được dùng.
_COOKIES_CANDIDATES = [
    os.path.join(CONTENT_DIR, 'config', 'youtube.com_cookies.txt'),
    os.path.join(CONTENT_DIR, 'config', 'youtube_cookies.txt'),
    os.path.join(CONTENT_DIR, 'youtube_cookies.txt'),
]
YOUTUBE_COOKIES = next((p for p in _COOKIES_CANDIDATES if os.path.exists(p)), None)
if YOUTUBE_COOKIES:
    print(f"[cookies] Using {YOUTUBE_COOKIES}", file=sys.stderr)

# Proxy riêng cho YouTube khi IP máy bị chặn endpoint phụ đề (per-machine,
# đặt trong config/.env). Nhiều proxy cách nhau dấu phẩy, thử theo thứ tự:
#   YT_PROXY=socks5://127.0.0.1:10001,socks5://127.0.0.1:40000   (4G, WARP)
# Chỉ áp cho method 1+2 (transcript/subtitle); method 4 tải audio nặng vẫn đi trực tiếp.
YT_PROXIES = [p.strip() for p in os.environ.get('YT_PROXY', '').split(',') if p.strip()]
if YT_PROXIES:
    print(f"[proxy] YouTube transcript qua {' → '.join(YT_PROXIES)}", file=sys.stderr)

# Không cấu hình YT_PROXY mà bị chặn → TỰ DÒ các proxy quen của dàn máy, cái nào
# sống thì dùng ngay trong phiên (không lưu cứng nên không bao giờ dính proxy chết):
# 4G local + gateway (máy chủ 4G 192.168.88.254), 4G qua LAN (các máy khác), WARP local.
_PROXY_CANDIDATES = [
    'socks5://127.0.0.1:10001',        # 4G SOCKS local (máy chủ 4G)
    'socks5://127.0.0.1:5000',         # 4G gateway local
    'socks5://192.168.88.254:10002',   # 4G relay qua LAN (mọi máy trong dàn)
    'socks5://192.168.88.254:5000',    # 4G gateway qua LAN
    'socks5://127.0.0.1:40000',        # Cloudflare WARP proxy mode
]
_PROXY_AUTODETECT_TS = 0.0


def _autodetect_proxies(reason: str = '') -> bool:
    """Bị chặn mà chưa có proxy nào chạy được → thử từng ứng viên (test nhẹ bằng
    robots.txt của YouTube), cái sống thì thêm vào YT_PROXIES của phiên này.
    Tối đa 1 lần/30 phút (4G/WARP có thể sống lại giữa chừng — tool bật 24/7).
    Trả về True nếu tìm được ít nhất 1 proxy mới."""
    global _PROXY_AUTODETECT_TS
    if time.time() - _PROXY_AUTODETECT_TS < 1800:
        return False
    _PROXY_AUTODETECT_TS = time.time()
    import requests
    found = []
    for p in _PROXY_CANDIDATES:
        if p in YT_PROXIES:
            continue
        try:
            r = requests.get('https://www.youtube.com/robots.txt',
                             proxies={'http': p, 'https': p}, timeout=8)
            if r.ok:
                found.append(p)
        except Exception:
            continue
    if found:
        YT_PROXIES.extend(found)
        log(f"[proxy] Tự dò thấy proxy sống ({reason}): {' → '.join(found)}")
        return True
    log(f"[proxy] Tự dò không thấy proxy nào sống ({reason}) — dùng browser/whisper")
    return False

# Detect local ffmpeg (check common locations)
def _find_ffmpeg() -> str:
    candidates = [
        os.path.join(CONTENT_DIR, 'ffmpeg', 'bin', 'ffmpeg.exe'),   # Windows local
        os.path.join(CONTENT_DIR, 'ffmpeg', 'ffmpeg.exe'),
        os.path.join(CONTENT_DIR, 'ffmpeg', 'bin', 'ffmpeg'),        # Linux/Mac local
        # 11lab_vm ffmpeg (shared on same machine)
        os.path.join(os.path.dirname(CONTENT_DIR), 'voice', '11lab_vm', 'ffmpeg', 'bin', 'ffmpeg.exe'),
        'ffmpeg',  # system PATH
    ]
    for c in candidates:
        try:
            import subprocess
            r = subprocess.run([c, '-version'], capture_output=True, timeout=3,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                if not os.path.isabs(c):
                    # 'ffmpeg' tren PATH → resolve tuyet doi, vi yt-dlp can
                    # ffmpeg_location = dirname (dirname('ffmpeg') rong → loi)
                    import shutil as _shutil
                    c = _shutil.which(c) or c
                return c
        except Exception:
            continue
    return ''

FFMPEG_PATH = _find_ffmpeg()
if FFMPEG_PATH:
    import subprocess as _sp
    _ver = _sp.run([FFMPEG_PATH, '-version'], capture_output=True).stdout.decode()[:60]
    print(f"[ffmpeg] Found: {FFMPEG_PATH} — {_ver.splitlines()[0] if _ver else '?'}",
          file=__import__('sys').stderr)
else:
    print("[ffmpeg] Not found — audio will be downloaded as native m4a (no compression)",
          file=__import__('sys').stderr)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def extract_video_id(s: str) -> str:
    s = s.strip()
    if re.match(r'^[A-Za-z0-9_-]{11}$', s):
        return s
    for pat in [r'[?&]v=([A-Za-z0-9_-]{11})',
                r'youtu\.be/([A-Za-z0-9_-]{11})',
                r'/shorts/([A-Za-z0-9_-]{11})']:
        m = re.search(pat, s)
        if m:
            return m.group(1)
    raise ValueError(f"Cannot extract video ID from: {s}")


def clean_text(raw: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def log(msg): print(f"  {msg}", file=sys.stderr)


# ─────────────────────────────────────────────
# Method 1: youtube-transcript-api
# ─────────────────────────────────────────────

def _make_yt_session(proxy: str = ''):
    """Tạo requests.Session với cookies và/hoặc proxy nếu cấu hình."""
    if not YOUTUBE_COOKIES and not proxy:
        return None
    try:
        import requests
        session = requests.Session()
        if YOUTUBE_COOKIES:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(YOUTUBE_COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar
        if proxy:
            session.proxies = {'http': proxy, 'https': proxy}
        return session
    except Exception as e:
        log(f"[cookies] Không load được session: {e}")
        return None


def method1_transcript_api(video_id: str) -> tuple:
    """Thử lần lượt: IP máy (khi không có proxy) hoặc từng proxy trong YT_PROXIES.
    Cả dàn thất bại → TỰ DÒ proxy quen của hệ thống rồi thử thêm 1 vòng (tự fix,
    máy mới không cần cấu hình gì)."""
    last_err = None
    tried = list(YT_PROXIES or [''])
    for proxy in tried:
        try:
            return _method1_once(video_id, proxy)
        except Exception as e:
            last_err = e
            via = proxy or 'IP máy'
            log(f"  method1 qua {via} lỗi: {type(e).__name__}")
    if _autodetect_proxies('method1 thất bại toàn bộ'):
        for proxy in [p for p in YT_PROXIES if p not in tried]:
            try:
                return _method1_once(video_id, proxy)
            except Exception as e:
                last_err = e
                log(f"  method1 qua {proxy} lỗi: {type(e).__name__}")
    raise last_err


def _method1_once(video_id: str, proxy: str = '') -> tuple:
    from youtube_transcript_api import YouTubeTranscriptApi
    prefer = ['es', 'es-419', 'es-MX', 'es-US', 'en', 'en-US']

    # Try v1.x API first
    try:
        session = _make_yt_session(proxy)
        api = YouTubeTranscriptApi(http_client=session) if session else YouTubeTranscriptApi()
        tlist = api.list(video_id)
        fetched = None
        lang_used = None

        for lang in prefer:
            try:
                t = tlist.find_transcript([lang])
                raw = t.fetch()
                fetched = ' '.join(
                    seg.text if hasattr(seg, 'text') else seg.get('text', '')
                    for seg in raw)
                lang_used = f"{lang} (v1)"
                break
            except Exception:
                continue

        if not fetched:
            all_t = list(tlist)
            if all_t:
                raw = all_t[0].fetch()
                fetched = ' '.join(
                    seg.text if hasattr(seg, 'text') else seg.get('text', '')
                    for seg in raw)
                lang_used = f"{getattr(all_t[0], 'language_code', '?')} (v1-any)"

        if fetched:
            return clean_text(fetched), lang_used
        raise RuntimeError("No transcript found")

    except AttributeError:
        # v0.x fallback
        tlist = YouTubeTranscriptApi.list_transcripts(video_id)
        for lang in prefer:
            try:
                raw = tlist.find_transcript([lang]).fetch()
                text = ' '.join(seg.get('text','') for seg in raw)
                return clean_text(text), f"{lang} (v0)"
            except Exception:
                continue
        raise RuntimeError("No transcript in v0 either")


# ─────────────────────────────────────────────
# Method 2: yt-dlp subtitle download
# ─────────────────────────────────────────────

def method2_ytdlp_sub(video_id: str, url: str) -> tuple:
    import yt_dlp, tempfile, glob

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'writesubtitles': True, 'writeautomaticsub': True,
            'subtitleslangs': ['es', 'es-419', 'en', 'en-US'],
            'subtitlesformat': 'vtt',
            'outtmpl': os.path.join(tmp, '%(id)s.%(ext)s'),
            'sleep_interval': 3, 'max_sleep_interval': 8,
        }
        if YOUTUBE_COOKIES:
            opts['cookiefile'] = YOUTUBE_COOKIES
            # Node.js + EJS giải n-challenge; Chrome impersonation bypass CDN 429
            opts['js_runtimes'] = {'node': {}}
            opts['remote_components'] = ['ejs:github']
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts['impersonate'] = ImpersonateTarget('chrome')
            except Exception:
                pass
        attempts = max(3, len(YT_PROXIES))
        for attempt in range(attempts):
            if YT_PROXIES:
                opts['proxy'] = YT_PROXIES[attempt % len(YT_PROXIES)]  # xoay proxy mỗi lượt
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                break
            except Exception as e:
                if '429' in str(e) and attempt < attempts - 1:
                    wait = (attempt + 1) * 8
                    log(f"429 rate limit — waiting {wait}s (attempt {attempt+1}/{attempts})")
                    time.sleep(wait)
                else:
                    raise

        vtt_files = glob.glob(os.path.join(tmp, f'{video_id}*.vtt'))
        if not vtt_files:
            raise FileNotFoundError("No subtitle files downloaded")

        def lang_score(f):
            if '.es.' in f or '.es-' in f: return 0
            if '.en.' in f: return 1
            return 2
        vtt_files.sort(key=lang_score)
        best = vtt_files[0]

        m = re.search(r'\.([a-z]{2}(?:-\w+)?)\.vtt$', best)
        lang = m.group(1) if m else 'unknown'

        with open(best, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()

        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line == 'WEBVTT': continue
            if re.match(r'^[\d:.,\s\-<>]+$', line): continue
            if line.startswith(('NOTE', 'STYLE', 'REGION')): continue
            line = re.sub(r'<[^>]+>', '', line)
            if line.strip():
                lines.append(line.strip())

        return clean_text(' '.join(lines)), f"{lang} (yt-dlp-vtt)"


# ─────────────────────────────────────────────
# Method 3: DrissionPage browser scraping
# ─────────────────────────────────────────────

def method3_browser(video_id: str, url: str) -> tuple:
    """
    Dùng DrissionPage mở YouTube, click 'Show transcript', scrape text.
    """
    from DrissionPage import ChromiumPage, ChromiumOptions

    opts = ChromiumOptions()
    opts.headless(True)
    opts.set_argument('--no-sandbox')
    opts.set_argument('--disable-dev-shm-usage')
    opts.set_argument('--mute-audio')

    page = ChromiumPage(addr_or_opts=opts)
    try:
        log("Browser: opening YouTube...")
        page.get(url)
        time.sleep(4)

        # Dismiss consent / cookie banners if present
        # DrissionPage can selector phai co prefix 'css:'; click by_js vi element
        # co the chua nam trong viewport headless ("no location or size")
        for sel in ['css:button[aria-label*="Accept"]', 'css:button[aria-label*="Reject"]',
                    'css:#yDmH0d button.VfPpkd-LgbsSe']:
            try:
                btn = page.ele(sel, timeout=2)
                if btn:
                    btn.click(by_js=True)
                    time.sleep(1)
                    break
            except Exception:
                pass

        # Layout moi: nut "Show transcript" nam trong phan mo ta video
        # (ytd-video-description-transcript-section-renderer), khong con trong menu "..."
        log("Browser: opening transcript from description...")
        clicked_transcript = False
        try:
            exp = page.ele('css:#expand', timeout=3)
            if exp:
                exp.click(by_js=True)
                time.sleep(2)
        except Exception:
            pass
        try:
            btn = page.ele('css:ytd-video-description-transcript-section-renderer button', timeout=5)
            if btn:
                btn.click(by_js=True)
                clicked_transcript = True
        except Exception:
            pass

        if not clicked_transcript:
            # Fallback layout cu: menu "..." → "Show transcript"
            log("Browser: fallback via '...' menu...")
            for sel in [
                'css:ytd-watch-metadata ytd-menu-renderer button',
                'css:ytd-menu-renderer button[aria-label]',
            ]:
                try:
                    btn = page.ele(sel, timeout=3)
                    if btn:
                        btn.click(by_js=True)
                        time.sleep(2)
                        break
                except Exception:
                    continue
            try:
                items = page.eles('css:ytd-menu-service-item-renderer')
                for item in items:
                    text = item.text.lower()
                    if 'transcript' in text or 'transcripci' in text:
                        item.click(by_js=True)
                        clicked_transcript = True
                        break
            except Exception:
                pass

        if not clicked_transcript:
            raise RuntimeError("Could not find 'Show transcript' button")

        # Scrape transcript segments — panel load lazy, poll toi 30s
        log("Browser: scraping transcript...")
        segments = []
        for _ in range(15):
            time.sleep(2)
            for sel in [
                'css:ytd-transcript-segment-renderer .segment-text',
                'css:ytd-transcript-body-renderer .segment-text',
                'css:ytd-transcript-segment-renderer',
            ]:
                try:
                    els = page.eles(sel, timeout=1)
                    if els:
                        segments = [e.text.strip() for e in els if e.text.strip()]
                        if segments:
                            break
                except Exception:
                    continue
            if segments:
                break

        if not segments:
            raise RuntimeError("No transcript segments found in browser")

        text = clean_text(' '.join(segments))
        log(f"Browser: got {len(text.split())} words")
        return text, 'es/en (browser-scraped)'

    finally:
        try:
            page.quit()
        except Exception:
            pass


# ─────────────────────────────────────────────
def _faster_whisper_transcribe(audio_file: str, lang_hint: str) -> str:
    """Transcribe bằng faster-whisper: GPU (int8_float16) trước, lỗi thì CPU (int8).
    DLL cuBLAS/cuDNN lấy từ pip wheels nvidia-cublas-cu12 / nvidia-cudnn-cu12."""
    import site, glob as _glob
    for _sp in site.getsitepackages():
        for _d in _glob.glob(os.path.join(_sp, 'nvidia', '*', 'bin')):
            try:
                os.add_dll_directory(_d)
                os.environ['PATH'] = _d + os.pathsep + os.environ['PATH']
            except Exception:
                pass
    from faster_whisper import WhisperModel

    last_err = None
    for device, compute in (('cuda', 'int8_float16'), ('cpu', 'int8')):
        try:
            log(f"  dùng faster-whisper small ({device}/{compute})...")
            model = WhisperModel('small', device=device, compute_type=compute)
            segments, _info = model.transcribe(
                audio_file, language=lang_hint or None, vad_filter=True, beam_size=1)
            text = clean_text(' '.join(s.text.strip() for s in segments))
            if not text.strip():
                raise RuntimeError('faster-whisper trả về text rỗng')
            return text
        except Exception as e:
            last_err = e
            log(f"  faster-whisper {device} lỗi: {type(e).__name__}: {str(e)[:150]}")
    raise RuntimeError(f"faster-whisper thất bại cả cuda lẫn cpu: {last_err}")


# Method 4: yt-dlp audio + Whisper API (guaranteed fallback)
# ─────────────────────────────────────────────

def method4_whisper(video_id: str, url: str) -> tuple:
    """
    Download audio via yt-dlp (no ffmpeg needed — tải m4a native),
    rồi gửi lên OpenAI Whisper API để transcribe.
    
    Đây là phương án đảm bảo nhất: hoạt động với MỌI video dù không có sub.
    Cần: OPENAI_API_KEY trong api_keys.json
    Không cần: ffmpeg
    Giới hạn: video < ~25 phút (file < 25MB)
    """
    import yt_dlp, tempfile, glob, shutil

    tmp_dir = tempfile.mkdtemp()
    try:
        # ── Step A: Download audio ──
        # Nếu có ffmpeg: nén thành mp3 64kbps (~4MB/8min, nhanh hơn upload)
        # Không có ffmpeg: tải m4a native (~8MB/8min, vẫn OK với Whisper)
        log("Whisper [A]: downloading audio...")

        if FFMPEG_PATH:
            log(f"  ffmpeg available → sẽ nén mp3 64kbps")
            opts = {
                'quiet': True, 'no_warnings': True,
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': os.path.join(tmp_dir, f'{video_id}.%(ext)s'),
                'ffmpeg_location': os.path.dirname(FFMPEG_PATH),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '64',  # 64kbps — đủ cho STT, file nhỏ
                }],
            }
            target_ext = '.mp3'
        else:
            log(f"  no ffmpeg → tải m4a native")
            opts = {
                'quiet': True, 'no_warnings': True,
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
                'outtmpl': os.path.join(tmp_dir, f'{video_id}.%(ext)s'),
            }
            target_ext = None

        # Giong method 2: cookies + node giai n-challenge + Chrome impersonation,
        # thieu thi tai audio dinh 403 chap chon
        if YOUTUBE_COOKIES:
            opts['cookiefile'] = YOUTUBE_COOKIES
        opts['js_runtimes'] = {'node': {}}
        opts['remote_components'] = ['ejs:github']
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts['impersonate'] = ImpersonateTarget('chrome')
        except Exception:
            pass

        last_dl_err = None
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                last_dl_err = None
                break
            except Exception as e:
                last_dl_err = e
                if attempt < 2:
                    wait = (attempt + 1) * 10
                    log(f"  audio download lỗi ({str(e)[:80]}) — retry sau {wait}s ({attempt+1}/3)")
                    time.sleep(wait)
        if last_dl_err:
            raise last_dl_err

        # Find the downloaded file
        audio_files = glob.glob(os.path.join(tmp_dir, f'{video_id}.*'))
        if not audio_files:
            raise FileNotFoundError("Không tìm thấy file audio sau khi tải")

        audio_file = audio_files[0]
        ext = os.path.splitext(audio_file)[1].lower()
        file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        log(f"Whisper [A]: tải xong {os.path.basename(audio_file)} ({file_size_mb:.1f} MB)")

        # ── Step B: Detect language ──
        lang_hint = 'es'
        if hasattr(method4_whisper, '_last_audio_lang'):
            raw = method4_whisper._last_audio_lang or 'es'
            # Whisper chỉ nhận ISO 639-1 (2 ký tự) — strip country code
            lang_hint = raw.split('-')[0].split('_')[0] or 'es'

        # ── Step C: Transcribe — local whisper preferred (no API key needed) ──
        log(f"Whisper [B]: transcribing ({file_size_mb:.1f} MB, lang={lang_hint})...")
        local_err = None  # giữ lỗi gốc của whisper local để báo ra (không nuốt)
        with _WHISPER_LOCK:  # serialise — chỉ 1 job dùng Whisper cùng lúc (RAM/VRAM)
            try:
                text = _faster_whisper_transcribe(audio_file, lang_hint)
                word_count = len(text.split())
                log(f"Whisper [B]: local SUCCESS — {word_count} words")
                return text, f'{lang_hint} (whisper-local-small)'
            except ImportError as _e:
                local_err = f"import faster_whisper lỗi: {type(_e).__name__}: {_e}"
                log(f"  {local_err} — fallback to API...")
            except Exception as _e:
                local_err = f"whisper local lỗi: {type(_e).__name__}: {_e}"
                log(f"  {local_err} — fallback to API...")

        # ── Fallback: OpenAI Whisper API (limit 25MB) ──
        if file_size_mb > 24.5:
            raise RuntimeError(
                f"Whisper local thất bại [{local_err}] và file audio quá lớn cho API "
                f"({file_size_mb:.1f} MB > 25 MB)"
            )
        api_key = OPENAI_API_KEY
        if not api_key:
            # Lộ lỗi gốc của whisper local thay vì câu chung chung "pip install"
            raise RuntimeError(
                f"Whisper local thất bại [{local_err}] và không có OPENAI_API_KEY để fallback API"
                if local_err else
                "Cần OPENAI_API_KEY hoặc pip install openai-whisper"
            )
        log(f"  dùng Whisper API ({OPENAI_API_BASE})...")

        # Determine MIME type
        mime_map = {
            '.m4a': 'audio/mp4',
            '.webm': 'audio/webm',
            '.mp3': 'audio/mpeg',
            '.ogg': 'audio/ogg',
            '.wav': 'audio/wav',
            '.flac': 'audio/flac',
        }
        mime_type = mime_map.get(ext, 'audio/mp4')
        filename = f'audio{ext}'

        with open(audio_file, 'rb') as f:
            audio_data = f.read()

        # Build multipart/form-data manually
        boundary = 'WhisperUploadBoundary42abc'
        CRLF = b'\r\n'

        def field(name, value):
            return (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n'
            ).encode()

        body = (
            field('model', 'whisper-1') +
            field('language', lang_hint) +
            field('response_format', 'text') +
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f'Content-Type: {mime_type}\r\n\r\n'
            ).encode() +
            audio_data +
            CRLF +
            f'--{boundary}--\r\n'.encode()
        )

        req = urllib.request.Request(
            f'{OPENAI_API_BASE}/audio/transcriptions',
            data=body,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            },
            method='POST',
        )

        with urllib.request.urlopen(req, timeout=180) as resp:
            # response_format=text returns plain text directly
            raw_response = resp.read().decode('utf-8', errors='replace')

        # response_format=text → raw_response is the transcript text
        text = raw_response.strip()
        if not text:
            raise RuntimeError("Whisper API trả về kết quả rỗng")

        word_count = len(text.split())
        log(f"Whisper [B]: SUCCESS — {word_count} words")
        return clean_text(text), f'{lang_hint} (whisper-api-m4a)'

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)



# ─────────────────────────────────────────────
# Metadata via yt-dlp
# ─────────────────────────────────────────────

def get_metadata(url: str) -> dict:
    import yt_dlp
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        'title':       info.get('title', ''),
        'description': (info.get('description', '') or '')[:2000],
        'channel':     info.get('channel', ''),
        'duration':    info.get('duration', 0),
        'view_count':  info.get('view_count', 0),
        'tags':        (info.get('tags', []) or [])[:20],
    }


# ─────────────────────────────────────────────
# YouTube Data API v3 helpers
# ─────────────────────────────────────────────

def yt_api_get_metadata(video_id: str) -> dict:
    """Get video metadata via YouTube Data API v3 (faster, more accurate)."""
    if not YT_API_KEY:
        return {}
    url = (f'https://www.googleapis.com/youtube/v3/videos'
           f'?part=snippet,contentDetails,statistics'
           f'&id={video_id}&key={YT_API_KEY}')
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        if not data.get('items'):
            return {}
        item = data['items'][0]
        snip = item['snippet']
        stats = item.get('statistics', {})
        return {
            'title':        snip.get('title', ''),
            'description':  snip.get('description', '')[:2000],
            'channel':      snip.get('channelTitle', ''),
            'tags':         snip.get('tags', [])[:20],
            'default_lang': snip.get('defaultLanguage', ''),
            'audio_lang':   snip.get('defaultAudioLanguage', ''),
            'view_count':   int(stats.get('viewCount', 0)),
            'like_count':   int(stats.get('likeCount', 0)),
        }
    except Exception as e:
        log(f'YT API metadata error: {e}')
        return {}


def yt_api_list_captions(video_id: str) -> list:
    """
    List available caption tracks via YouTube Data API v3.
    Returns list of dicts: [{language, trackKind, name}, ...]
    NOTE: Can list tracks but NOT download content with API key alone.
    This is used as a pre-check to know which methods to try first.
    """
    if not YT_API_KEY:
        return []
    url = (f'https://www.googleapis.com/youtube/v3/captions'
           f'?part=snippet&videoId={video_id}&key={YT_API_KEY}')
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        tracks = []
        for item in data.get('items', []):
            s = item['snippet']
            tracks.append({
                'language':  s['language'],
                'trackKind': s['trackKind'],   # 'standard', 'asr' (auto), 'forced'
                'name':      s.get('name', ''),
            })
        return tracks
    except Exception as e:
        log(f'YT API captions list error: {e}')
        return []


def has_spanish_captions(tracks: list) -> bool:
    return any(t['language'].startswith('es') for t in tracks)


def has_english_captions(tracks: list) -> bool:
    return any(t['language'].startswith('en') for t in tracks)


# ─────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────

METHODS = [
    (1, "youtube-transcript-api", method1_transcript_api),
    (2, "yt-dlp subtitle",        method2_ytdlp_sub),
    (3, "DrissionPage browser",   method3_browser),
    (4, "Whisper API",            method4_whisper),
]


def fetch_video_data(url_or_id: str, force: bool = False,
                     only_method: int = None) -> dict:
    video_id = extract_video_id(url_or_id)
    url = f"https://www.youtube.com/watch?v={video_id}"
    cache_file = os.path.join(CACHE_DIR, f"{video_id}.json")

    if os.path.exists(cache_file) and not force:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Only return cached if transcript was successfully fetched
        if data.get('transcript'):
            log(f"[CACHE] {cache_file}")
            return data
        log("[CACHE] Cached file has no transcript — re-fetching...")

    result = {
        'video_id': video_id, 'url': url,
        'title_original': '', 'description': '', 'channel': '',
        'duration_sec': 0, 'view_count': 0, 'tags': [],
        'transcript': '', 'transcript_lang': '',
        'transcript_method': '', 'transcript_word_count': 0,
        'fetch_errors': [], 'methods_tried': [],
    }

    # ── Step 1: Metadata — try YT API first, fallback to yt-dlp ──
    print(f"\n[FETCH] {url}", file=sys.stderr)
    print(f"[1/2] Getting metadata...", file=sys.stderr)

    # Pre-check captions with YT API (fast, uses API key)
    caption_tracks = []
    if YT_API_KEY:
        log("YT API: checking caption tracks...")
        caption_tracks = yt_api_list_captions(video_id)
        if caption_tracks:
            langs = [t['language'] for t in caption_tracks]
            kinds = [t['trackKind'] for t in caption_tracks]
            log(f"YT API: found {len(caption_tracks)} caption track(s): {langs} ({kinds})")
            result['caption_tracks'] = caption_tracks
            result['has_spanish_sub'] = has_spanish_captions(caption_tracks)
            result['has_english_sub'] = has_english_captions(caption_tracks)
        else:
            log("YT API: no caption tracks found (or private)")
            result['caption_tracks'] = []
            result['has_spanish_sub'] = False
            result['has_english_sub'] = False

    # Get metadata
    try:
        # Try YT API first (faster)
        meta = yt_api_get_metadata(video_id) if YT_API_KEY else {}
        if not meta.get('title'):
            # Fallback to yt-dlp
            log("yt-dlp metadata fallback...")
            import yt_dlp
            opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            meta = {
                'title':       info.get('title', ''),
                'description': (info.get('description', '') or '')[:2000],
                'channel':     info.get('channelTitle', '') or info.get('channel', ''),
                'tags':        (info.get('tags', []) or [])[:20],
                'view_count':  info.get('view_count', 0),
                'audio_lang':  info.get('language', ''),
            }
        result.update({
            'title_original': meta.get('title', ''),
            'description':    meta.get('description', ''),
            'channel':        meta.get('channel', ''),
            'view_count':     meta.get('view_count', 0),
            'tags':           meta.get('tags', []),
            'audio_lang':     meta.get('audio_lang', ''),
        })
        log(f"Title: {result['title_original'][:80]}")
        log(f"Channel: {result['channel']} | Views: {result['view_count']:,}")
        # Inform method4 of audio language for accurate Whisper transcription
        method4_whisper._last_audio_lang = result.get('audio_lang', '') or 'es'
    except Exception as e:
        result['fetch_errors'].append(f"Metadata: {e}")
        log(f"WARNING: Metadata failed: {e}")

    # ── Step 2: Transcript — cascade through methods ──
    print(f"[2/2] Getting transcript (cascade)...", file=sys.stderr)

    for num, name, fn in METHODS:
        if only_method and num != only_method:
            continue

        log(f"Trying Method {num}: {name}...")
        result['methods_tried'].append(num)

        try:
            if num in (1,):
                text, lang = fn(video_id)
            else:
                text, lang = fn(video_id, url)

            if text and len(text.split()) >= 50:
                result['transcript'] = text
                result['transcript_lang'] = lang
                result['transcript_method'] = name
                result['transcript_word_count'] = len(text.split())
                log(f"SUCCESS: {result['transcript_word_count']} words [{lang}]")
                break
            else:
                raise RuntimeError(f"Transcript too short: {len(text.split())} words")

        except Exception as e:
            err = f"Method {num} ({name}): {e}"
            result['fetch_errors'].append(err)
            log(f"FAILED: {e}")
            # Brief pause before next method
            if num < 4:
                time.sleep(2)

    # ── Save ──
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    status = "OK" if result['transcript'] else "NO_TRANSCRIPT"
    print(f"[{status}] Saved → {cache_file}", file=sys.stderr)
    return result


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python fetch_video_data.py <url_or_id> [--force] [--method N]")
        print("  --force     Re-fetch even if cached")
        print("  --method N  Use only method N (1=api, 2=ytdlp, 3=browser, 4=whisper)")
        sys.exit(1)

    url_input = sys.argv[1]
    force = '--force' in sys.argv
    only = None
    if '--method' in sys.argv:
        idx = sys.argv.index('--method')
        only = int(sys.argv[idx + 1])

    data = fetch_video_data(url_input, force=force, only_method=only)

    # Print summary
    print("\n" + "=" * 60)
    print(f"VIDEO      : {data['video_id']}")
    print(f"TITLE      : {data['title_original'][:80]}")
    print(f"CHANNEL    : {data['channel']} | {data['view_count']:,} views")
    print(f"TRANSCRIPT : {data['transcript_word_count']} words"
          f" [{data['transcript_lang']}] via {data['transcript_method']}")
    if data['fetch_errors']:
        print(f"ERRORS ({len(data['fetch_errors'])}):")
        for e in data['fetch_errors']:
            print(f"  - {e[:120]}")
    print("=" * 60)

    if data['transcript']:
        print(f"\nPREVIEW:\n{data['transcript'][:400]}...")

    print()
    print(json.dumps({
        "status": "ok" if data['transcript'] else "no_transcript",
        "video_id": data['video_id'],
        "title": data['title_original'],
        "words": data['transcript_word_count'],
        "method": data['transcript_method'],
        "cache": cache_file if 'cache_file' in dir() else
                 os.path.join(CACHE_DIR, data['video_id'] + '.json'),
    }, ensure_ascii=False))



def get_transcript(link: str, out_dir: str, force: bool = False, log=print) -> dict:
    """Lấy transcript và lưu vào out_dir (thư mục run của job)."""
    global CACHE_DIR
    CACHE_DIR = out_dir
    import os as _os
    _os.makedirs(out_dir, exist_ok=True)
    log(f"[fetch] Lấy transcript đối thủ: {link}")
    data = fetch_video_data(link, force=force)
    if not data.get("transcript"):
        errs = " | ".join(data.get("fetch_errors", [])) or "không rõ"
        raise RuntimeError(f"Không lấy được transcript từ {link}. Lỗi: {errs}")
    log(
        f"[fetch] OK — {len(data.get('transcript',''))} ký tự "
        f"[{data.get('transcript_lang','?')}] · tiêu đề gốc: {data.get('title_original','')[:60]}"
    )
    return data
