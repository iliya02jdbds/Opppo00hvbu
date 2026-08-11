# -*- coding: utf-8 -*-
"""Rubika downloader bot — revised build.

New in this revision versus pddo.py
-------------------------------------
* Bare @username (or clean plain username) detection:
  the bot fetches the account's latest N posts via instagrapi and
  delivers each one automatically.  Private accounts that the logged-in
  session follows are supported.
* Carousel / album posts (media_type == 8) now handled in both the
  URL-download path (dl_ig) and the username-browse path
  (dl_ig_username) — all resources in the album are delivered.
* BOT_TOKEN read from environment first; hardcoded string is the fallback
  so existing deploys keep working without changes.
* bot.get_name() (undocumented/absent in rubka) replaced with a safe
  attribute lookup on the Message object.
* Persian-facing strings updated to mention the @username feature.
* MAX_IG_POSTS env var to cap how many posts a username lookup returns
  (default 3).

Architecture overview
----------------------
  handle_text()
    ├─ URL detected         → dl_ytdlp() or dl_ig()
    └─ @username / username → dl_ig_username()

  dl_ig()          – single IG URL; instagrapi primary, yt-dlp fallback
  dl_ig_username() – profile browse; instagrapi only (yt-dlp can't browse)
  dl_ytdlp()       – everything else + IG fallback

All three share StatusHandle for in-place progress editing and the
global CACHE dict for repeat-request deduplication.
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import yt_dlp
from rubka import Robot
from rubka.context import Message
from rubka.button import InlineBuilder

# instagrapi is optional; the bot still works (via yt-dlp) without it.
try:
    from instagrapi import Client as InstaClient
    from instagrapi.types import Media  # for type hints only
    HAS_INSTA = True
except ImportError:  # pragma: no cover
    HAS_INSTA = False


# --------------------------------------------------------------------------- #
# Configuration — environment variables; BOT_TOKEN falls back to hard-coded
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "CBFJCA0CGJYHRSVMVIXCHVNXRWASMKEKVIXCRORAAGJSJAVOBRJFHTUPCATYTNCI",
)
ADMIN_IDS: set[str] = {
    x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
CACHE_DIR = DOWNLOAD_DIR / "cache"
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "49"))
MAX_CACHE_ENTRIES = int(os.environ.get("MAX_CACHE_ENTRIES", "50"))
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "720").strip()
CONCURRENT_FRAGMENTS = max(4, int(os.environ.get("CONCURRENT_FRAGMENTS", "16")))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "cookies.txt"))

# If COOKIES_CONTENT_B64 is set and the cookie file doesn't already exist
# (e.g. first boot on a fresh Railway volume), decode it and write it out.
# On subsequent restarts the file already exists on the volume, so this
# is skipped and the persisted cookies are used as-is.
_cookies_b64 = os.environ.get("COOKIES_CONTENT_B64", "").strip()
if _cookies_b64 and not COOKIES_FILE.exists():
    import base64
    try:
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_bytes(base64.b64decode(_cookies_b64))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("bot").error("Failed to write cookies file: %s", exc)

IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "").strip()
MAX_IG_POSTS = int(os.environ.get("MAX_IG_POSTS", "3"))   # posts returned per username lookup

INFO_TIMEOUT = int(os.environ.get("INFO_TIMEOUT", "45"))
DL_TIMEOUT = int(os.environ.get("DL_TIMEOUT", "600"))

HAS_ARIA2 = shutil.which("aria2c") is not None

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "WARNING").upper(),
)
for _noisy in ("httpx", "httpcore", "yt_dlp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")


# --------------------------------------------------------------------------- #
# Persian user-facing messages  (code, comments, and docstrings stay English)
# --------------------------------------------------------------------------- #
MSG_SEND_LINK = (
    "🔗 یک لینک بفرست یا نام کاربری اینستاگرام را با @ بفرست "
    "تا برایت دانلود کنم."
)
MSG_FETCHING = "🔎 در حال دریافت اطلاعات…"
MSG_UPLOADING = "📤 در حال ارسال فایل…"
MSG_FAILED = "❌ دانلود ناموفق، دوباره تلاش کنید."
MSG_TIMEOUT = "⏱ زمان دانلود به پایان رسید، دوباره تلاش کنید."
MSG_NO_OUTPUT = "❌ فایلی برای ارسال یافت نشد، دوباره تلاش کنید."
MSG_DISABLED = "⛔ ربات موقتاً غیرفعال است."
MSG_TOO_BIG = "❌ حجم فایل {sz:.0f}MB است و از محدودهٔ {limit}MB بیشتر است."
MSG_DOWNGRADE = "ℹ️ کیفیت {req}p موجود نیست؛ به {chosen}p تغییر یافت."
MSG_IG_INVALID = "❌ لینک اینستاگرام نامعتبر است."
MSG_IG_UNSUPPORTED = "❌ این نوع پست اینستاگرام پشتیبانی نمی‌شود."
MSG_IG_NOT_FOUND = "❌ کاربر اینستاگرام @{username} یافت نشد."
MSG_IG_NO_POSTS = "❌ هیچ پستی برای @{username} یافت نشد."
MSG_IG_NO_SESSION = (
    "❌ برای دریافت پست‌های کاربر، ربات باید به اینستاگرام وارد شده باشد."
)
MSG_IG_FETCHING_USER = "🔎 در حال دریافت آخرین پست‌های @{username}…"
MSG_IG_SENDING = "📤 در حال ارسال {n} پست از @{username}…"


# --------------------------------------------------------------------------- #
# Regular expressions and runtime state
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://\S+", re.I)
INSTA_RE = re.compile(
    r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I
)
# Matches a clean Instagram handle: 1-30 alphanumeric / dot / underscore chars.
# Requires the WHOLE text to be the handle (no spaces, no extra tokens).
INSTA_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_.]{3,30})$")

# Single-word strings that are almost certainly not Instagram usernames.
_COMMON_WORDS = {
    "start", "help", "stop", "hi", "hello", "hey", "ok", "okay",
    "yes", "no", "سلام", "ممنون", "خوب", "بله", "نه", "مرسی",
    "باشه", "چطور", "چی", "چه",
}

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
SKIP_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".tmp"}

STATS: dict[str, Any] = {
    "users": set(),
    "downloads": 0,
    "errors": 0,
    "ig_downloads": 0,
    "started": time.time(),
}

CACHE: dict[str, Path] = {}
BACKGROUND_TASKS: set[asyncio.Task] = set()
BOT_ENABLED = True
ig_client: Optional["InstaClient"] = None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required (set env var or check hard-coded fallback).")

bot = Robot(token=BOT_TOKEN)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spawn(coro) -> asyncio.Task:
    """Schedule a coroutine as a tracked background task."""
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


def progress_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 10)
    return "▓" * filled + "░" * (10 - filled)


def _probe_height(path: Path) -> Optional[int]:
    """Return the video height reported by ffprobe, or None on any failure."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "csv=p=0",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
        digits = "".join(ch for ch in (out.splitlines()[0] if out else "") if ch.isdigit())
        return int(digits) if digits else None
    except Exception as exc:  # noqa: BLE001
        log.warning("ffprobe failed for %s: %s", path.name, exc)
        return None


def is_ig_username(text: str) -> Optional[str]:
    """Return the clean Instagram username if *text* looks like a bare handle.

    Returns None if text contains a URL, multiple words, is a common word,
    or does not match the Instagram handle character set.
    """
    text = text.strip()
    if not text or " " in text or URL_RE.search(text):
        return None
    if text.lower() in _COMMON_WORDS:
        return None
    m = INSTA_HANDLE_RE.match(text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Instagram client — lazy init, every call guarded
# --------------------------------------------------------------------------- #
def init_instagram() -> None:
    global ig_client
    if not (HAS_INSTA and IG_USERNAME and IG_PASSWORD):
        return
    try:
        client = InstaClient()
        session_path = Path("ig_session.json")
        if session_path.exists():
            try:
                client.load_settings(session_path)
                client.login(IG_USERNAME, IG_PASSWORD)
                ig_client = client
                log.warning("Instagram session restored from ig_session.json")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Saved IG session invalid, fresh login: %s", exc)
        client.login(IG_USERNAME, IG_PASSWORD)
        try:
            client.dump_settings(session_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist IG session: %s", exc)
        ig_client = client
        log.warning("Instagram logged in as %s", IG_USERNAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Instagram login failed: %s", exc)
        ig_client = None


# --------------------------------------------------------------------------- #
# Inline keyboard builders
# --------------------------------------------------------------------------- #
def main_panel(uid: str) -> dict:
    builder = InlineBuilder().row(
        InlineBuilder().button_simple("help", "راهنما"),
        InlineBuilder().button_simple("status", "وضعیت"),
    )
    if uid in ADMIN_IDS:
        builder = builder.row(InlineBuilder().button_simple("admin", "مدیریت"))
    return builder.build()


def admin_panel() -> dict:
    return (
        InlineBuilder()
        .row(
            InlineBuilder().button_simple("a_stats", "آمار"),
            InlineBuilder().button_simple("a_toggle", "روشن/خاموش"),
        )
        .row(InlineBuilder().button_simple("home", "بازگشت"))
        .build()
    )


def back_panel() -> dict:
    return InlineBuilder().row(
        InlineBuilder().button_simple("home", "بازگشت")
    ).build()


# --------------------------------------------------------------------------- #
# StatusHandle — a single "working…" message edited in-place
# --------------------------------------------------------------------------- #
@dataclass
class StatusHandle:
    chat_id: str
    message_id: str
    _last_text: str = ""
    _last_edit: float = 0.0
    _deleted: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def edit(
        self, text: str, *, force: bool = False, interval: float = 1.2
    ) -> None:
        if self._deleted or text == self._last_text:
            return
        now = time.time()
        if not force and (now - self._last_edit) < interval:
            return
        self._last_text = text
        self._last_edit = now
        try:
            await bot.edit_message_text(self.chat_id, self.message_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("edit_message_text failed: %s", exc)

    async def delete(self) -> None:
        if self._deleted:
            return
        self._deleted = True
        try:
            await bot.delete_message(self.chat_id, self.message_id)
        except Exception:  # noqa: BLE001
            pass


async def _new_status(chat_id: str, text: str) -> StatusHandle:
    """Send the initial status bubble and return a handle to it."""
    sent = await bot.send_message(chat_id, text)
    # rubka returns {"data": {"message_id": "..."}, ...}
    message_id = sent["data"]["message_id"]
    handle = StatusHandle(chat_id=chat_id, message_id=message_id)
    handle._last_text = text
    return handle


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
@bot.on_message(commands=["start"])
async def cmd_start(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    STATS["users"].add(uid)
    engine = "aria2c" if HAS_ARIA2 else "yt-dlp"
    ig_note = " · اینستاگرام: متصل" if ig_client else ""
    # sender_name is present on most rubka Message objects; fall back gracefully.
    name = getattr(message, "sender_name", None) or "دوست من"
    await message.reply_inline(
        f"سلام {name} 👋\n\n"
        f"من ربات دانلودر هستم · موتور: {engine}{ig_note}\n\n"
        f"• یک لینک ویدیو بفرست (یوتیوب، تیک‌تاک، توییتر، اینستاگرام و …)\n"
        f"• یا نام کاربری اینستاگرام را با @ بفرست تا آخرین پست‌هایش را بگیری.\n\n"
        f"فایل با کیفیت تا {DEFAULT_QUALITY}p خودکار ارسال می‌شه.",
        main_panel(uid),
    )


@bot.on_message(commands=["help"])
async def cmd_help(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    await message.reply_inline(
        f"📌 راهنمای ربات\n\n"
        f"① لینک ویدیو بفرست ← فایل تا {DEFAULT_QUALITY}p خودکار ارسال می‌شه.\n"
        f"② نام کاربری اینستاگرام با @ بفرست ← آخرین {MAX_IG_POSTS} پست ارسال می‌شه.\n\n"
        f"پلتفرم‌های پشتیبانی‌شده: یوتیوب، تیک‌تاک، توییتر/X، اینستاگرام و هر "
        f"سایتی که yt-dlp پشتیبانی می‌کند.",
        main_panel(uid),
    )


# --------------------------------------------------------------------------- #
# Text handler — URL detection → download; @username → IG profile browse
# --------------------------------------------------------------------------- #
def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(")>].,!؟") if m else None


@bot.on_message()
async def handle_text(bot: Robot, message: Message) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # commands handled by their own decorators

    uid = message.sender_id
    chat_id = message.chat_id
    STATS["users"].add(uid)

    if not BOT_ENABLED and uid not in ADMIN_IDS:
        await message.reply(MSG_DISABLED)
        return

    # ── Path A: a real URL ──────────────────────────────────────────────────
    url = extract_url(text)
    if url:
        status = await _new_status(chat_id, MSG_FETCHING)
        if INSTA_RE.search(url):
            _spawn(dl_ig(status, url, chat_id))
        else:
            _spawn(dl_ytdlp(status, url, DEFAULT_QUALITY, chat_id))
        return

    # ── Path B: bare Instagram username (@handle or plain handle) ───────────
    ig_user = is_ig_username(text)
    if ig_user:
        status = await _new_status(chat_id, MSG_FETCHING)
        _spawn(dl_ig_username(status, ig_user, chat_id))
        return

    # ── Fallback ─────────────────────────────────────────────────────────────
    await message.reply_inline(MSG_SEND_LINK, main_panel(uid))


# --------------------------------------------------------------------------- #
# Format probing and quality resolution
# --------------------------------------------------------------------------- #
def _probe_formats(url: str) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _available_heights(info: dict) -> list[int]:
    return sorted(
        {int(f["height"]) for f in info.get("formats", []) if f.get("height")}
    )


def _choose_height(requested: int, available: list[int]) -> Optional[int]:
    if not available:
        return None
    at_or_below = [h for h in available if h <= requested]
    return max(at_or_below) if at_or_below else min(available)


async def _resolve_format(
    loop: asyncio.AbstractEventLoop,
    url: str,
    quality: str,
    is_instagram: bool,
    status: StatusHandle,
) -> str:
    if is_instagram:
        return "best"
    if quality == "audio":
        return "bestaudio/best"
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b"

    requested = int(quality)
    target = requested
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _probe_formats, url),
            timeout=INFO_TIMEOUT,
        )
        chosen = _choose_height(requested, _available_heights(info))
        if chosen:
            target = chosen
            if chosen != requested:
                await status.edit(
                    MSG_DOWNGRADE.format(req=requested, chosen=chosen), force=True
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("Format probe failed for %s: %s", url, exc)

    return (
        f"bv*[height<={target}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={target}]+ba/"
        f"b[height<={target}]/b"
    )


# --------------------------------------------------------------------------- #
# In-process download cache
# --------------------------------------------------------------------------- #
def _cache_key(url: str, quality: str, is_instagram: bool) -> str:
    return f"{url}|{'best' if is_instagram else quality}"


def _store_in_cache(path: Path, key: str) -> Path:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        dest = CACHE_DIR / f"{digest}{path.suffix}"
        shutil.move(str(path), str(dest))
        CACHE[key] = dest
        _evict_cache()
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("Cache store failed: %s", exc)
        return path


def _evict_cache() -> None:
    while len(CACHE) > MAX_CACHE_ENTRIES:
        old_key, old_path = next(iter(CACHE.items()))
        CACHE.pop(old_key, None)
        try:
            old_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Upload — send a file to Rubika; returns True on success
# --------------------------------------------------------------------------- #
async def _send_media(
    status: StatusHandle,
    path: Path,
    chat_id: str,
    quality: str,
) -> bool:
    size_mb = path.stat().st_size / 1_048_576
    if size_mb > MAX_FILE_MB:
        await status.edit(
            MSG_TOO_BIG.format(sz=size_mb, limit=MAX_FILE_MB), force=True
        )
        return False

    ext = path.suffix.lower()
    if quality == "audio" or ext in AUDIO_EXTS:
        caption = f"🎵 {size_mb:.1f} MB"
    else:
        h = _probe_height(path)
        caption = f"{size_mb:.1f} MB" + (f" · {h}p" if h else "")

    await status.edit(MSG_UPLOADING, force=True)
    try:
        if quality == "audio" or ext in AUDIO_EXTS:
            await bot.send_music(chat_id, path=str(path), text=caption)
        else:
            # rubka has no dedicated send_video; send_document delivers video files fine.
            # Best-effort: try undocumented file_type kwarg; fall back on TypeError.
            try:
                await bot.send_document(
                    chat_id, path=str(path), text=caption, file_type="Video"
                )
            except TypeError:
                await bot.send_document(chat_id, path=str(path), text=caption)
    except Exception as exc:  # noqa: BLE001
        log.error("Upload failed for %s: %s", path.name, exc)
        await status.edit(MSG_FAILED, force=True)
        return False
    return True


# --------------------------------------------------------------------------- #
# yt-dlp download pipeline (with aria2c acceleration and cache)
# --------------------------------------------------------------------------- #
def _build_ytdlp_opts(
    url: str, folder: Path, fmt: str, quality: str, hook
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": str(folder / "%(title).60s.%(ext)s"),
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 15,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "max_filesize": MAX_FILE_MB * 1_048_576,
        "progress_hooks": [hook],
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if HAS_ARIA2:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": [
                "-x16", "-s16", "-k1M",
                "--max-connection-per-server=16",
                "--min-split-size=1M",
            ]
        }
    if quality == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    return opts


def _run_ytdlp(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


async def _download_with_fallback(
    loop: asyncio.AbstractEventLoop, url: str, opts: dict
) -> dict:
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _run_ytdlp, url, opts),
            timeout=DL_TIMEOUT,
        )
    except yt_dlp.utils.DownloadError as exc:
        if "requested format is not available" in str(exc).lower():
            log.warning("Format unavailable, retrying with best: %s", url)
            retry = dict(opts, format="best")
            retry.pop("postprocessors", None)
            return await asyncio.wait_for(
                loop.run_in_executor(None, _run_ytdlp, url, retry),
                timeout=DL_TIMEOUT,
            )
        raise


async def dl_ytdlp(
    status: StatusHandle, url: str, quality: str, chat_id: str
) -> None:
    """Download via yt-dlp and deliver to Rubika."""
    loop = asyncio.get_running_loop()
    is_instagram = bool(INSTA_RE.search(url))
    key = _cache_key(url, quality, is_instagram)

    cached = CACHE.get(key)
    if cached and cached.exists():
        try:
            if await _send_media(status, cached, chat_id, quality):
                STATS["downloads"] += 1
                await status.delete()
                return
        except Exception:  # noqa: BLE001
            log.warning("Cached upload failed, re-downloading: %s", url)
            CACHE.pop(key, None)

    await status.edit(MSG_FETCHING, force=True)
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    last_edit: dict[str, float] = {"t": 0.0}

    def hook(data: dict) -> None:
        # Runs in a thread — marshal edits onto the event loop.
        if data.get("status") != "downloading":
            return
        now = time.time()
        if now - last_edit["t"] < 1.5:
            return
        last_edit["t"] = now
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes", 0)
        pct = (done / total * 100) if total else 0
        speed = (data.get("speed") or 0) / 1_048_576
        eta = data.get("eta")
        eta_txt = f" · {eta}s" if eta else ""
        text = (
            f"⬇️ در حال دانلود {progress_bar(pct)} {pct:.0f}%\n"
            f"{speed:.1f} MB/s{eta_txt}"
        )
        try:
            loop.call_soon_threadsafe(
                lambda: _spawn(status.edit(text, interval=0))
            )
        except RuntimeError:
            pass

    try:
        fmt = await _resolve_format(loop, url, quality, is_instagram, status)
        opts = _build_ytdlp_opts(url, folder, fmt, quality, hook)
        await _download_with_fallback(loop, url, opts)

        files = [
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() not in SKIP_EXTS
        ]
        if not files:
            await status.edit(MSG_NO_OUTPUT, force=True)
            STATS["errors"] += 1
            return
        path = max(files, key=lambda f: f.stat().st_size)

        if path.stat().st_size > MAX_FILE_MB * 1_048_576:
            size_mb = path.stat().st_size / 1_048_576
            await status.edit(
                MSG_TOO_BIG.format(sz=size_mb, limit=MAX_FILE_MB), force=True
            )
            STATS["errors"] += 1
            return

        cached_path = _store_in_cache(path, key)
        if await _send_media(status, cached_path, chat_id, quality):
            STATS["downloads"] += 1
            if is_instagram:
                STATS["ig_downloads"] += 1
            await status.delete()

    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await status.edit(MSG_TIMEOUT, force=True)
    except yt_dlp.utils.DownloadError as exc:
        STATS["errors"] += 1
        log.warning("yt-dlp download error: %s", exc)
        await status.edit(MSG_FAILED, force=True)
    except Exception:  # noqa: BLE001
        STATS["errors"] += 1
        log.error("Unexpected error for %s", url, exc_info=True)
        await status.edit(MSG_FAILED, force=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Instagram URL download (instagrapi primary → yt-dlp fallback)
# --------------------------------------------------------------------------- #
async def _send_ig_media_list(
    paths: List[Path],
    chat_id: str,
    status: StatusHandle,
) -> int:
    """Upload a list of already-downloaded IG media files.  Returns send count."""
    sent = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        size_mb = p.stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            log.warning("IG media %s too large (%.1f MB), skipping", p.name, size_mb)
            continue
        ext = p.suffix.lower()
        try:
            if ext in AUDIO_EXTS:
                await bot.send_music(chat_id, path=str(p))
            elif ext in {".jpg", ".jpeg", ".png", ".webp"}:
                await bot.send_image(chat_id, path=str(p))
            else:
                try:
                    await bot.send_document(
                        chat_id, path=str(p),
                        text=f"{size_mb:.1f} MB", file_type="Video"
                    )
                except TypeError:
                    await bot.send_document(
                        chat_id, path=str(p), text=f"{size_mb:.1f} MB"
                    )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.error("Send failed for %s: %s", p.name, exc)
        finally:
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return sent


async def dl_ig(status: StatusHandle, url: str, chat_id: str) -> None:
    """Download a single Instagram post URL: video, photo, or album."""
    if not ig_client:
        await dl_ytdlp(status, url, "best", chat_id)
        return

    await status.edit(MSG_FETCHING, force=True)
    loop = asyncio.get_running_loop()
    downloaded_paths: List[Path] = []

    try:
        # Extract shortcode — handles /p/, /reel/, /tv/, and /stories/ paths.
        match = re.search(
            r"/(p|reel|tv|stories)/([A-Za-z0-9_-]+)", url
        )
        if not match:
            await status.edit(MSG_IG_INVALID, force=True)
            return
        shortcode = match.group(2)

        def fetch_info():
            return ig_client.media_info(ig_client.media_pk_from_code(shortcode))

        post = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_info), timeout=INFO_TIMEOUT
        )

        await status.edit(MSG_UPLOADING, force=True)

        if post.media_type == 2:  # single video
            def dl_vid():
                return ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))
            p = await asyncio.wait_for(
                loop.run_in_executor(None, dl_vid), timeout=DL_TIMEOUT
            )
            downloaded_paths = [Path(p)]

        elif post.media_type == 1:  # single photo
            def dl_photo():
                return ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))
            p = await asyncio.wait_for(
                loop.run_in_executor(None, dl_photo), timeout=INFO_TIMEOUT
            )
            downloaded_paths = [Path(p)]

        elif post.media_type == 8:  # album / carousel — download ALL resources
            resources = post.resources or []
            for res in resources:
                try:
                    if res.media_type == 2:
                        def dl_res_vid(pk=res.pk):
                            return ig_client.video_download(
                                pk, folder=str(DOWNLOAD_DIR)
                            )
                        rp = await asyncio.wait_for(
                            loop.run_in_executor(None, dl_res_vid),
                            timeout=DL_TIMEOUT,
                        )
                    else:
                        def dl_res_img(pk=res.pk):
                            return ig_client.photo_download(
                                pk, folder=str(DOWNLOAD_DIR)
                            )
                        rp = await asyncio.wait_for(
                            loop.run_in_executor(None, dl_res_img),
                            timeout=INFO_TIMEOUT,
                        )
                    downloaded_paths.append(Path(rp))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Album resource %s failed: %s", res.pk, exc)

        else:
            await status.edit(MSG_IG_UNSUPPORTED, force=True)
            return

        sent = await _send_ig_media_list(downloaded_paths, chat_id, status)
        if sent:
            STATS["downloads"] += sent
            STATS["ig_downloads"] += sent
            await status.delete()
        else:
            await status.edit(MSG_NO_OUTPUT, force=True)
            STATS["errors"] += 1

    except Exception as exc:  # noqa: BLE001 — always fall back to yt-dlp
        STATS["errors"] += 1
        log.warning("instagrapi URL fetch failed (%s), falling back to yt-dlp", exc)
        # Clean up any partial downloads before handing off.
        for p in downloaded_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        await dl_ytdlp(status, url, "best", chat_id)


# --------------------------------------------------------------------------- #
# Instagram username → latest N posts
# --------------------------------------------------------------------------- #
async def dl_ig_username(
    status: StatusHandle, username: str, chat_id: str
) -> None:
    """Fetch the latest MAX_IG_POSTS posts from an Instagram profile.

    Uses the logged-in instagrapi session, so private accounts that the
    login account follows are accessible.  There is no yt-dlp fallback for
    profile browsing (yt-dlp does not support listing a user's feed).
    """
    if not ig_client:
        await status.edit(MSG_IG_NO_SESSION, force=True)
        STATS["errors"] += 1
        return

    await status.edit(
        MSG_IG_FETCHING_USER.format(username=username), force=True
    )
    loop = asyncio.get_running_loop()

    try:
        def fetch_user_medias():
            user_id = ig_client.user_id_from_username(username)
            return ig_client.user_medias(user_id, amount=MAX_IG_POSTS)

        medias = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_user_medias),
            timeout=INFO_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        err_str = str(exc).lower()
        if "not found" in err_str or "usernamenotfound" in err_str:
            await status.edit(
                MSG_IG_NOT_FOUND.format(username=username), force=True
            )
        else:
            log.warning("User lookup @%s failed: %s", username, exc)
            await status.edit(
                MSG_IG_NOT_FOUND.format(username=username), force=True
            )
        STATS["errors"] += 1
        return

    if not medias:
        await status.edit(
            MSG_IG_NO_POSTS.format(username=username), force=True
        )
        STATS["errors"] += 1
        return

    await status.edit(
        MSG_IG_SENDING.format(n=len(medias), username=username), force=True
    )
    total_sent = 0

    for media in medias:
        paths_this_post: List[Path] = []
        try:
            if media.media_type == 2:  # video
                def dl_v(pk=media.pk):
                    return ig_client.video_download(
                        pk, folder=str(DOWNLOAD_DIR)
                    )
                p = await asyncio.wait_for(
                    loop.run_in_executor(None, dl_v), timeout=DL_TIMEOUT
                )
                paths_this_post = [Path(p)]

            elif media.media_type == 1:  # photo
                def dl_p(pk=media.pk):
                    return ig_client.photo_download(
                        pk, folder=str(DOWNLOAD_DIR)
                    )
                p = await asyncio.wait_for(
                    loop.run_in_executor(None, dl_p), timeout=INFO_TIMEOUT
                )
                paths_this_post = [Path(p)]

            elif media.media_type == 8:  # album / carousel
                resources = media.resources or []
                for res in resources:
                    try:
                        if res.media_type == 2:
                            def dl_rv(pk=res.pk):
                                return ig_client.video_download(
                                    pk, folder=str(DOWNLOAD_DIR)
                                )
                            rp = await asyncio.wait_for(
                                loop.run_in_executor(None, dl_rv),
                                timeout=DL_TIMEOUT,
                            )
                        else:
                            def dl_ri(pk=res.pk):
                                return ig_client.photo_download(
                                    pk, folder=str(DOWNLOAD_DIR)
                                )
                            rp = await asyncio.wait_for(
                                loop.run_in_executor(None, dl_ri),
                                timeout=INFO_TIMEOUT,
                            )
                        paths_this_post.append(Path(rp))
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "Album resource %s in post %s failed: %s",
                            res.pk, media.pk, exc,
                        )

            # Skip unknown types silently.
            sent = await _send_ig_media_list(
                paths_this_post, chat_id, status
            )
            total_sent += sent

        except asyncio.TimeoutError:
            log.warning("Timeout downloading media %s from @%s", media.pk, username)
            for p in paths_this_post:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Error on media %s from @%s: %s", media.pk, username, exc)
            for p in paths_this_post:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

    if total_sent:
        STATS["downloads"] += total_sent
        STATS["ig_downloads"] += total_sent
        await status.delete()
    else:
        await status.edit(MSG_NO_OUTPUT, force=True)
        STATS["errors"] += 1


# --------------------------------------------------------------------------- #
# Callback handler (admin panel + navigation)
# --------------------------------------------------------------------------- #
def _extract_button_id(message: Message) -> str:
    """Extract the pressed button's id from aux_data.

    rubka returns aux_data as either a dict or an object depending on build;
    handle both, and fall back to raw_data for diagnosability.
    """
    aux = getattr(message, "aux_data", None)
    if aux is None:
        return ""
    if isinstance(aux, dict):
        bid = aux.get("button_id") or aux.get("id") or ""
    else:
        bid = getattr(aux, "button_id", "") or getattr(aux, "id", "") or ""
    if not bid:
        raw = getattr(message, "raw_data", None) or {}
        bid = (
            raw.get("aux_data", {}).get("button_id", "")
            if isinstance(raw.get("aux_data"), dict)
            else ""
        )
    return bid


@bot.on_callback()
async def callbacks(bot: Robot, message: Message) -> None:
    global BOT_ENABLED
    data = _extract_button_id(message)
    log.warning(
        "callback: button_id=%r  raw_aux_data=%r",
        data,
        getattr(message, "aux_data", None),
    )
    uid = message.sender_id
    chat_id = message.chat_id
    edit_msg_id = getattr(message, "message_id", None) or (
        (getattr(message, "raw_data", {}) or {}).get("message_id")
    )

    async def safe_edit(text: str, markup: Optional[dict] = None) -> None:
        try:
            await bot.edit_message_text(chat_id, edit_msg_id, text)
            if markup is not None:
                await bot.edit_inline_keypad(chat_id, edit_msg_id, markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("safe_edit failed: %s", exc)

    if data == "home":
        await safe_edit("🏠 خانه", main_panel(uid))
    elif data == "help":
        await safe_edit(
            f"کافیست لینک ویدیو بفرستی یا نام کاربری اینستاگرام را با @ بفرستی.\n"
            f"فایل با کیفیت تا {DEFAULT_QUALITY}p خودکار ارسال می‌شه.",
            back_panel(),
        )
    elif data == "status":
        engine = "aria2c" if HAS_ARIA2 else "yt-dlp"
        ig_note = "متصل" if ig_client else "خاموش"
        up = int(time.time() - STATS["started"])
        await safe_edit(
            f"🟢 آنلاین · موتور: {engine} · اینستاگرام: {ig_note}\n"
            f"دانلودها: {STATS['downloads']} · خطاها: {STATS['errors']}\n"
            f"مدت فعالیت: {up // 3600}س {(up % 3600) // 60}د",
            back_panel(),
        )
    elif data == "admin" and uid in ADMIN_IDS:
        await safe_edit("⚙️ پنل مدیریت", admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time() - STATS["started"])
        await safe_edit(
            f"👥 کاربران: {len(STATS['users'])}\n"
            f"⬇️ دانلودها: {STATS['downloads']} (اینستاگرام: {STATS['ig_downloads']})\n"
            f"❌ خطاها: {STATS['errors']}\n"
            f"⏱ مدت فعالیت: {up // 3600}س {(up % 3600) // 60}д",
            admin_panel(),
        )
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await safe_edit(
            "✅ ربات روشن شد" if BOT_ENABLED else "⛔ ربات خاموش شد",
            admin_panel(),
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    init_instagram()
    log.warning(
        "Rubika bot starting — engine: %s | IG: %s",
        "aria2c" if HAS_ARIA2 else "yt-dlp",
        "active" if ig_client else "off",
    )
    bot.run()
