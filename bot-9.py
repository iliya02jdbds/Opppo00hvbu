# -*- coding: utf-8 -*-
"""Rubika Instagram Downloader Bot — v15 (بدون دکمه شیشه‌ای)

همه منوها و پنل ادمین با دستور متنی کار می‌کنند.
هیچ Inline Button استفاده نشده.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yt_dlp
from rubka import Robot
from rubka.context import Message

try:
    from instagrapi import Client as InstaClient
    HAS_INSTA = True
except ImportError:
    HAS_INSTA = False


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "CBFJCA0CGJYHRSVMVIXCHVNXRWASMKEKVIXCRORAAGJSJAVOBRJFHTUPCATYTNCI",
)
ADMIN_IDS: Set[str] = {
    x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
CACHE_DIR = DOWNLOAD_DIR / "cache"
USERS_FILE = Path(os.environ.get("USERS_FILE", "users.json"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "49"))
MAX_CACHE_ENTRIES = int(os.environ.get("MAX_CACHE_ENTRIES", "50"))
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "720").strip()
CONCURRENT_FRAGMENTS = max(4, int(os.environ.get("CONCURRENT_FRAGMENTS", "16")))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "cookies.txt"))

_cookies_b64 = os.environ.get("COOKIES_CONTENT_B64", "").strip()
if _cookies_b64 and not COOKIES_FILE.exists():
    import base64
    try:
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_bytes(base64.b64decode(_cookies_b64))
    except Exception as exc:
        logging.getLogger("bot").error("Failed to write cookies file: %s", exc)

IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "").strip()
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "").strip()
MAX_IG_POSTS = int(os.environ.get("MAX_IG_POSTS", "100"))
MAX_SELECT = 10
POSTS_PER_PAGE = 10

INFO_TIMEOUT = int(os.environ.get("INFO_TIMEOUT", "60"))
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
# Messages
# --------------------------------------------------------------------------- #
MSG_SEND_LINK = (
    "🔗 یک لینک بفرست یا نام کاربری اینستاگرام را با @ بفرست.\n\n"
    "دستورات:\n"
    "/help - راهنما\n"
    "/status - وضعیت ربات\n"
    "/admin - پنل مدیریت (فقط ادمین)"
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
MSG_IG_NO_SESSION = "❌ برای دریافت پست‌های کاربر، ربات باید به اینستاگرام وارد شده باشد."
MSG_IG_FETCHING_USER = "🔎 در حال دریافت آخرین پست‌های @{username}…\n(تا ۱۰۰ پست)"
MSG_SELECT_PROMPT = (
    "📋 لیست پست‌های @{username}\n"
    "صفحه {page}/{total_pages} — پست‌های {start} تا {end}\n\n"
    "{list}\n\n"
    "🔢 شماره پست‌هایی که می‌خوای رو بفرست (حداکثر {max_select} تا)\n"
    "مثال: 1 5 12 68\n\n"
    "دستورات:\n"
    "بعدی - صفحه بعد\n"
    "قبلی - صفحه قبل\n"
    "لغو - انصراف"
)
MSG_INVALID_SELECTION = "❌ شماره‌ها نامعتبر است. فقط عدد بنویس (مثل: 1 5 12)"
MSG_TOO_MANY_SELECTED = f"❌ حداکثر {MAX_SELECT} پست می‌تونی همزمان انتخاب کنی."
MSG_NO_VALID_POSTS = "❌ هیچ پست معتبری انتخاب نشد."
MSG_SENDING_SELECTED = "📤 در حال ارسال {n} پست انتخاب‌شده از @{username}…"


# --------------------------------------------------------------------------- #
# Regex & State
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://\S+", re.I)
INSTA_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I)
INSTA_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_.]{3,30})$")
NUMBERS_RE = re.compile(r"(\d+)")

_COMMON_WORDS = {
    "start", "help", "stop", "hi", "hello", "hey", "ok", "okay",
    "yes", "no", "سلام", "ممنون", "خوب", "بله", "نه", "مرسی",
    "باشه", "چطور", "چی", "چه", "بعدی", "قبلی", "صفحه", "لغو",
}

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
SKIP_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".tmp"}

STATS: Dict[str, Any] = {
    "users": set(),
    "downloads": 0,
    "errors": 0,
    "ig_downloads": 0,
    "started": time.time(),
}

USER_STATE: Dict[str, Dict[str, Any]] = {}
CACHE: Dict[str, Path] = {}
BACKGROUND_TASKS: Set[asyncio.Task] = set()
BOT_ENABLED = True
ig_client: Optional["InstaClient"] = None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required.")

bot = Robot(token=BOT_TOKEN)


# --------------------------------------------------------------------------- #
# Persistent users
# --------------------------------------------------------------------------- #
def load_users() -> Set[str]:
    if USERS_FILE.exists():
        try:
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            return set(data.get("users", []))
        except Exception:
            pass
    return set()


def save_users(users: Set[str]) -> None:
    try:
        USERS_FILE.write_text(
            json.dumps({"users": list(users)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Failed to save users: %s", exc)


STATS["users"] = load_users()


def add_user(uid: str) -> None:
    if uid not in STATS["users"]:
        STATS["users"].add(uid)
        save_users(STATS["users"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


def progress_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 10)
    return "▓" * filled + "░" * (10 - filled)


def _probe_height(path: Path) -> Optional[int]:
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
    except Exception as exc:
        log.warning("ffprobe failed for %s: %s", path.name, exc)
        return None


def is_ig_username(text: str) -> Optional[str]:
    text = text.strip()
    if not text or " " in text or URL_RE.search(text):
        return None
    if text.lower() in _COMMON_WORDS:
        return None
    m = INSTA_HANDLE_RE.match(text)
    return m.group(1) if m else None


def media_type_fa(media_type: int) -> str:
    if media_type == 1:
        return "📷 عکس"
    if media_type == 2:
        return "🎬 ویدیو"
    if media_type == 8:
        return "📁 آلبوم"
    return "❓"


def format_post_line(idx: int, media) -> str:
    t = media_type_fa(getattr(media, "media_type", 0))
    caption = (getattr(media, "caption_text", None) or "")[:40].replace("\n", " ")
    if caption:
        caption = f" — {caption}…"
    return f"{idx}. {t}{caption}"


# --------------------------------------------------------------------------- #
# Instagram client
# --------------------------------------------------------------------------- #
IG_SESSION_FILE = Path(os.environ.get("IG_SESSION_FILE", "ig_session.json"))

_ig_session_b64 = os.environ.get("IG_SESSION_CONTENT_B64", "").strip()
if _ig_session_b64 and not IG_SESSION_FILE.exists():
    import base64 as _base64
    try:
        IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        IG_SESSION_FILE.write_bytes(_base64.b64decode(_ig_session_b64))
    except Exception as exc:
        logging.getLogger("bot").error("Failed to write IG session file: %s", exc)

_ig_challenge_event = threading.Event()
_ig_challenge_code = ""
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def _notify_admins(text: str) -> None:
    if _main_loop is None or not ADMIN_IDS:
        log.warning("Could not notify admins: %s", text)
        return
    for admin_id in ADMIN_IDS:
        try:
            asyncio.run_coroutine_threadsafe(
                bot.send_message(admin_id, text), _main_loop
            )
        except Exception as exc:
            log.warning("Failed to notify admin %s: %s", admin_id, exc)


def _ig_challenge_code_handler(username: str, choice) -> str:
    _ig_challenge_event.clear()
    global _ig_challenge_code
    _ig_challenge_code = ""
    _notify_admins(
        f"⚠️ اینستاگرام برای ورود اکانت {username} کد تایید می‌خواد.\n"
        f"کد رو اینطوری بفرست:\n/code 123456"
    )
    log.warning("Waiting for IG verification code for %s...", username)
    if not _ig_challenge_event.wait(timeout=300):
        return ""
    return _ig_challenge_code


def _parse_sessionid_from_cookies(cookies_path: Path) -> str:
    from urllib.parse import unquote
    try:
        for line in cookies_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#HttpOnly_"):
                stripped = stripped[len("#HttpOnly_"):]
            elif stripped.startswith("#") or not stripped:
                continue
            parts = stripped.split("\t")
            if len(parts) >= 7 and parts[5] == "sessionid":
                return unquote(parts[6])
    except Exception as exc:
        log.warning("Could not parse sessionid: %s", exc)
    return ""


def init_instagram(force: bool = False) -> bool:
    """تلاش برای ورود به اینستاگرام.
    اول sessionid، اگر نشد یوزرنیم+پسورد.
    اگر force=True باشد حتی اگر ig_client موجود باشد دوباره تلاش می‌کند.
    برمی‌گرداند True اگر موفق شود.
    """
    from urllib.parse import unquote
    global ig_client
    if not HAS_INSTA:
        return False

    if ig_client is not None and not force:
        return True

    # --- روش ۱: sessionid ---
    session_id = unquote(IG_SESSION_ID) if IG_SESSION_ID else ""
    if not session_id and COOKIES_FILE.exists():
        session_id = _parse_sessionid_from_cookies(COOKIES_FILE)

    if session_id:
        log.warning("Attempting Instagram login via sessionid...")
        try:
            client = InstaClient()
            client.login_by_sessionid(session_id)
            try:
                IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                client.dump_settings(IG_SESSION_FILE)
            except Exception:
                pass
            ig_client = client
            log.warning("✅ Instagram logged in via sessionid")
            _notify_admins("✅ ورود اینستاگرام با sessionid موفق بود.")
            return True
        except Exception as exc:
            log.error("❌ sessionid login failed: %s", exc)
            _notify_admins(f"❌ sessionid کار نکرد: {exc}")

    # --- روش ۲: یوزرنیم + پسورد ---
    if not (IG_USERNAME and IG_PASSWORD):
        log.warning("No IG_USERNAME/IG_PASSWORD set, cannot fallback login")
        return False

    try:
        client = InstaClient()
        client.challenge_code_handler = _ig_challenge_code_handler

        # اول سعی کن سشن ذخیره‌شده را لود کنی
        if IG_SESSION_FILE.exists():
            try:
                client.load_settings(IG_SESSION_FILE)
                client.login(IG_USERNAME, IG_PASSWORD)
                ig_client = client
                log.warning("✅ Instagram session restored with username/password")
                _notify_admins("✅ سشن اینستاگرام با یوزرنیم/پسورد بازیابی شد.")
                return True
            except Exception as exc:
                log.warning("Saved session invalid, doing fresh login: %s", exc)

        # لاگین تازه
        client.login(IG_USERNAME, IG_PASSWORD)
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception:
            pass
        ig_client = client
        log.warning("✅ Instagram logged in as %s", IG_USERNAME)
        _notify_admins(f"✅ ورود اینستاگرام با اکانت {IG_USERNAME} موفق بود.")
        return True
    except Exception as exc:
        log.error("Instagram login failed: %s", exc)
        ig_client = None
        _notify_admins(f"❌ ورود اینستاگرام ناموفق بود: {exc}")
        return False


def ensure_ig_client() -> bool:
    """اگر ig_client مرده باشد، دوباره تلاش برای لاگین می‌کند."""
    global ig_client
    if ig_client is not None:
        try:
            # تست سبک — اگر خطا بدهد یعنی سشن مرده
            ig_client.get_timeline_feed()
            return True
        except Exception:
            log.warning("ig_client seems dead, trying to re-login...")
            ig_client = None

    return init_instagram(force=True)


# --------------------------------------------------------------------------- #
# StatusHandle
# --------------------------------------------------------------------------- #
@dataclass
class StatusHandle:
    chat_id: str
    message_id: str
    _last_text: str = ""
    _last_edit: float = 0.0
    _deleted: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def edit(self, text: str, *, force: bool = False, interval: float = 1.2) -> None:
        if self._deleted or text == self._last_text:
            return
        now = time.time()
        if not force and (now - self._last_edit) < interval:
            return
        self._last_text = text
        self._last_edit = now
        try:
            await bot.edit_message_text(self.chat_id, self.message_id, text)
        except Exception as exc:
            log.warning("edit_message_text failed: %s", exc)

    async def delete(self) -> None:
        if self._deleted:
            return
        self._deleted = True
        try:
            await bot.delete_message(self.chat_id, self.message_id)
        except Exception:
            pass


async def _new_status(chat_id: str, text: str) -> StatusHandle:
    sent = await bot.send_message(chat_id, text)
    message_id = sent["data"]["message_id"]
    handle = StatusHandle(chat_id=chat_id, message_id=message_id)
    handle._last_text = text
    return handle


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@bot.on_message(commands=["start"])
async def cmd_start(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    add_user(uid)
    engine = "aria2c" if HAS_ARIA2 else "yt-dlp"
    ig_note = " · اینستاگرام: متصل" if ig_client else ""
    name = getattr(message, "sender_name", None) or "دوست من"
    await message.reply(
        f"سلام {name} 👋\n\n"
        f"من ربات دانلودر هستم · موتور: {engine}{ig_note}\n\n"
        f"• لینک ویدیو بفرست\n"
        f"• یا @username اینستاگرام بفرست تا پست‌ها رو انتخاب کنی\n\n"
        f"دستورات:\n"
        f"/help - راهنما\n"
        f"/status - وضعیت\n"
        f"/admin - پنل مدیریت (ادمین)"
    )


@bot.on_message(commands=["myid"])
async def cmd_myid(bot: Robot, message: Message) -> None:
    await message.reply(f"🆔 آیدی شما: {message.sender_id}")


@bot.on_message(commands=["help"])
async def cmd_help(bot: Robot, message: Message) -> None:
    await message.reply(
        f"📌 راهنما\n\n"
        f"① لینک ویدیو بفرست ← فایل تا {DEFAULT_QUALITY}p ارسال می‌شه\n"
        f"② @username بفرست ← لیست تا ۱۰۰ پست نمایش داده می‌شه\n"
        f"   بعد شماره پست‌ها رو بنویس (حداکثر {MAX_SELECT} تا)\n\n"
        f"مثال انتخاب: 1 5 12 68\n\n"
        f"دستورات ادمین:\n"
        f"/admin - پنل مدیریت\n"
        f"/stats - آمار\n"
        f"/toggle - روشن/خاموش\n"
        f"/users - لیست کاربران\n"
        f"/clearusers - حذف همه کاربران\n"
        f"/broadcast متن - پیام همگانی"
    )


@bot.on_message(commands=["status"])
async def cmd_status(bot: Robot, message: Message) -> None:
    engine = "aria2c" if HAS_ARIA2 else "yt-dlp"
    ig_note = "متصل" if ig_client else "خاموش"
    up = int(time.time() - STATS["started"])
    await message.reply(
        f"🟢 آنلاین · موتور: {engine} · اینستاگرام: {ig_note}\n"
        f"دانلودها: {STATS['downloads']} · خطاها: {STATS['errors']}\n"
        f"مدت فعالیت: {up // 3600}س {(up % 3600) // 60}د"
    )


@bot.on_message(commands=["admin"])
async def cmd_admin(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    if uid not in ADMIN_IDS:
        await message.reply("⛔ شما ادمین نیستید.")
        return
    await message.reply(
        "⚙️ پنل مدیریت\n\n"
        "دستورات موجود:\n"
        "/stats - آمار کامل\n"
        "/toggle - روشن یا خاموش کردن ربات\n"
        "/users - لیست کاربران\n"
        "/clearusers - حذف همه کاربران\n"
        "/broadcast متن پیام - ارسال پیام همگانی\n"
        "/igstatus - وضعیت اینستاگرام\n"
        "/login - ورود با sessionid\n"
        "/loginpass - ورود با یوزرنیم + پسورد\n"
        "/loginemail - ورود با چالش ایمیل\n"
        "/code 123456 - دادن کد تایید\n"
        "/checkig - چک واقعی بودن لاگین"
    )


@bot.on_message(commands=["stats"])
async def cmd_stats(bot: Robot, message: Message) -> None:
    if message.sender_id not in ADMIN_IDS:
        return
    up = int(time.time() - STATS["started"])
    await message.reply(
        f"👥 کاربران: {len(STATS['users'])}\n"
        f"⬇️ دانلودها: {STATS['downloads']} (اینستاگرام: {STATS['ig_downloads']})\n"
        f"❌ خطاها: {STATS['errors']}\n"
        f"⏱ مدت فعالیت: {up // 3600}س {(up % 3600) // 60}د\n"
        f"وضعیت ربات: {'🟢 روشن' if BOT_ENABLED else '🔴 خاموش'}"
    )


@bot.on_message(commands=["toggle"])
async def cmd_toggle(bot: Robot, message: Message) -> None:
    global BOT_ENABLED
    if message.sender_id not in ADMIN_IDS:
        return
    BOT_ENABLED = not BOT_ENABLED
    await message.reply("✅ ربات روشن شد" if BOT_ENABLED else "⛔ ربات خاموش شد")


@bot.on_message(commands=["users"])
async def cmd_users(bot: Robot, message: Message) -> None:
    if message.sender_id not in ADMIN_IDS:
        return
    users = list(STATS["users"])
    if not users:
        await message.reply("هیچ کاربری ثبت نشده.")
        return
    sample = users[:40]
    text = f"👥 لیست کاربران ({len(users)} نفر):\n\n" + "\n".join(sample)
    if len(users) > 40:
        text += f"\n\n... و {len(users) - 40} نفر دیگر"
    await message.reply(text)


@bot.on_message(commands=["clearusers"])
async def cmd_clearusers(bot: Robot, message: Message) -> None:
    if message.sender_id not in ADMIN_IDS:
        return
    STATS["users"].clear()
    save_users(STATS["users"])
    await message.reply("✅ همه کاربران حذف شدند.")


@bot.on_message(commands=["broadcast"])
async def cmd_broadcast(bot: Robot, message: Message) -> None:
    if message.sender_id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("مثال:\n/broadcast سلام دوستان، ربات آپدیت شد.")
        return

    text = parts[1].strip()
    users = list(STATS["users"])
    if not users:
        await message.reply("هیچ کاربری برای ارسال وجود ندارد.")
        return

    await message.reply(f"📤 شروع ارسال به {len(users)} کاربر...")
    success = 0
    fail = 0
    for u in users:
        try:
            await bot.send_message(u, text)
            success += 1
            await asyncio.sleep(0.15)
        except Exception:
            fail += 1
    await message.reply(f"✅ ارسال تمام شد.\nموفق: {success}\nناموفق: {fail}")


@bot.on_message(commands=["igstatus"])
async def cmd_igstatus(bot: Robot, message: Message) -> None:
    from urllib.parse import unquote
    if message.sender_id not in ADMIN_IDS:
        return
    sid_env = unquote(IG_SESSION_ID) if IG_SESSION_ID else ""
    sid_cookie = _parse_sessionid_from_cookies(COOKIES_FILE) if COOKIES_FILE.exists() else ""
    lines = [
        f"HAS_INSTA: {HAS_INSTA}",
        f"ig_client: {'✅ متصل' if ig_client else '❌ None'}",
        f"IG_SESSION_ID: {'✅ ' + sid_env[:25] + '...' if sid_env else '❌ خالی'}",
        f"COOKIES_FILE: {'✅' if COOKIES_FILE.exists() else '❌'}",
        f"sessionid در کوکی: {'✅ ' + sid_cookie[:25] + '...' if sid_cookie else '❌'}",
        f"IG_SESSION_FILE: {'✅' if IG_SESSION_FILE.exists() else '❌'}",
        f"کاربران: {len(STATS['users'])}",
    ]
    await message.reply("\n".join(lines))


@bot.on_message(commands=["login"])
async def cmd_login(bot: Robot, message: Message) -> None:
    """تلاش ورود با sessionid (روش قدیمی)"""
    global _main_loop, ig_client
    if message.sender_id not in ADMIN_IDS:
        return
    if not HAS_INSTA:
        await message.reply("❌ instagrapi نصب نیست.")
        return
    from urllib.parse import unquote
    sid = unquote(IG_SESSION_ID) if IG_SESSION_ID else _parse_sessionid_from_cookies(COOKIES_FILE)
    if not sid:
        await message.reply("❌ sessionid پیدا نشد. از /loginpass استفاده کن.")
        return

    await message.reply(f"🔄 تلاش login با sessionid:\n{sid[:30]}...")
    _main_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()

    if ig_client:
        await message.reply("✅ ig_client قبلاً متصله.")
        return

    def _try_login():
        from instagrapi import Client as _C
        c = _C()
        c.login_by_sessionid(sid)
        return c

    try:
        client = await asyncio.wait_for(loop.run_in_executor(None, _try_login), timeout=30)
        ig_client = client
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception:
            pass
        await message.reply("✅ ورود موفق با sessionid!")
    except Exception as exc:
        await message.reply(f"❌ شکست sessionid:\n{exc}\n\nاز /loginpass استفاده کن.")


@bot.on_message(commands=["loginpass"])
async def cmd_loginpass(bot: Robot, message: Message) -> None:
    """ورود با یوزرنیم + پسورد و پشتیبانی از کد ایمیل"""
    global _main_loop, ig_client
    if message.sender_id not in ADMIN_IDS:
        return
    if not HAS_INSTA:
        await message.reply("❌ instagrapi نصب نیست.")
        return
    if not (IG_USERNAME and IG_PASSWORD):
        await message.reply(
            "❌ یوزرنیم یا پسورد ست نشده.\n\n"
            "این دو تا رو ست کن:\n"
            "INSTAGRAM_USERNAME=یوزرنیم\n"
            "INSTAGRAM_PASSWORD=پسورد"
        )
        return

    await message.reply(
        f"🔄 در حال ورود با اکانت: {IG_USERNAME}\n\n"
        f"اگر اینستاگرام کد ایمیل خواست، کد رو اینطوری بفرست:\n"
        f"/code 123456\n\n"
        f"حداکثر ۲ دقیقه فرصت داری."
    )
    _main_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()

    def _try_login_pass():
        from instagrapi import Client as _C
        from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired, BadPassword, LoginRequired

        c = _C()
        c.challenge_code_handler = _ig_challenge_code_handler

        # پاک کردن سشن قبلی
        if IG_SESSION_FILE.exists():
            try:
                IG_SESSION_FILE.unlink()
            except Exception:
                pass

        try:
            c.login(IG_USERNAME, IG_PASSWORD)
            return c
        except ChallengeRequired as e:
            # چالش عادی — از handler استفاده می‌شود
            raise e
        except Exception as e:
            err = str(e).lower()
            if "email" in err or "send you an email" in err or "checkpoint" in err:
                # حالت خاص: اینستاگرام می‌خواهد از طریق ایمیل وارد شویم
                _notify_admins(
                    "⚠️ اینستاگرام درخواست ورود با ایمیل داده.\n"
                    "کد ایمیل رو سریع با دستور زیر بفرست:\n/code 123456"
                )
            raise e

    try:
        client = await asyncio.wait_for(
            loop.run_in_executor(None, _try_login_pass), timeout=150
        )
        ig_client = client
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception:
            pass
        await message.reply(f"✅ ورود موفق با اکانت {IG_USERNAME}!")
    except Exception as exc:
        err_msg = str(exc)
        if "email" in err_msg.lower() or "send you an email" in err_msg.lower():
            await message.reply(
                "⚠️ اینستاگرام گفت می‌تونه برات ایمیل بفرسته.\n\n"
                "۱. برو ایمیلت رو چک کن و کد رو بردار\n"
                "۲. سریع این دستور رو بزن:\n"
                "/code 123456\n\n"
                "بعد دوباره /loginpass بزن."
            )
        else:
            await message.reply(f"❌ ورود با پسورد شکست خورد:\n{err_msg}")


@bot.on_message(commands=["code"])
async def cmd_code(bot: Robot, message: Message) -> None:
    """دادن کد تایید اینستاگرام"""
    global _ig_challenge_code
    if message.sender_id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await message.reply("کد رو اینطوری بفرست:\n/code 123456")
        return
    _ig_challenge_code = code
    _ig_challenge_event.set()
    await message.reply("✅ کد ثبت شد، در حال تلاش برای ورود…")


@bot.on_message(commands=["checkig"])
async def cmd_checkig(bot: Robot, message: Message) -> None:
    """چک کردن واقعی بودن لاگین اینستاگرام"""
    if message.sender_id not in ADMIN_IDS:
        return
    if not HAS_INSTA:
        await message.reply("❌ instagrapi نصب نیست.")
        return
    if ig_client is None:
        await message.reply("❌ ig_client برابر None است. هنوز لاگین نشده.")
        return

    await message.reply("🔄 در حال بررسی اتصال واقعی به اینستاگرام...")
    loop = asyncio.get_running_loop()

    def _check():
        results = []
        try:
            account = ig_client.account_info()
            results.append(f"✅ account_info: {account.username} (pk={account.pk})")
        except Exception as e:
            results.append(f"❌ account_info: {e}")
        try:
            ig_client.get_timeline_feed()
            results.append("✅ timeline_feed: دریافت شد")
        except Exception as e:
            results.append(f"❌ timeline_feed: {e}")
        try:
            user_id = ig_client.user_id_from_username("instagram")
            results.append(f"✅ user_id_from_username: {user_id}")
        except Exception as e:
            results.append(f"❌ user_id_from_username: {e}")
        return results

    try:
        results = await asyncio.wait_for(loop.run_in_executor(None, _check), timeout=30)
        text = "🔍 نتیجه بررسی اتصال اینستاگرام:\n\n" + "\n".join(results)
        await message.reply(text)
    except Exception as exc:
        await message.reply(f"❌ خطا در بررسی:\n{exc}")


@bot.on_message(commands=["loginemail"])
async def cmd_loginemail(bot: Robot, message: Message) -> None:
    """ورود با چالش ایمیل — کد رو با /code بفرست"""
    global _main_loop, ig_client, _ig_challenge_code
    if message.sender_id not in ADMIN_IDS:
        return
    if not HAS_INSTA:
        await message.reply("❌ instagrapi نصب نیست.")
        return
    if not (IG_USERNAME and IG_PASSWORD):
        await message.reply("❌ یوزرنیم یا پسورد ست نشده.")
        return

    await message.reply(
        f"🔄 شروع ورود با چالش ایمیل برای {IG_USERNAME}\n\n"
        f"۱. صبر کن تا اینستاگرام کد بفرسته\n"
        f"۲. کد رو از ایمیلت بردار\n"
        f"۳. سریع بفرست:\n/code 123456\n\n"
        f"حداکثر ۳ دقیقه فرصت داری."
    )

    _main_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()
    _ig_challenge_event.clear()
    _ig_challenge_code = ""

    def _try_email_login():
        from instagrapi import Client as _C
        from instagrapi.exceptions import ChallengeRequired

        c = _C()
        c.challenge_code_handler = _ig_challenge_code_handler

        # پاک کردن سشن قبلی
        if IG_SESSION_FILE.exists():
            try:
                IG_SESSION_FILE.unlink()
            except Exception:
                pass

        try:
            c.login(IG_USERNAME, IG_PASSWORD)
            return c
        except ChallengeRequired:
            # منتظر کد می‌مانیم (handler آن را می‌گیرد)
            # بعد از دریافت کد، دوباره تلاش می‌کنیم
            if not _ig_challenge_event.wait(timeout=180):
                raise Exception("زمان انتظار برای کد ایمیل تمام شد")
            # کد دریافت شده، دوباره لاگین
            c2 = _C()
            c2.challenge_code_handler = lambda u, ch: _ig_challenge_code
            c2.login(IG_USERNAME, IG_PASSWORD)
            return c2
        except Exception as e:
            err = str(e)
            if "email" in err.lower() or "send you an email" in err.lower() or "checkpoint" in err.lower():
                _notify_admins(
                    "⚠️ اینستاگرام درخواست کد ایمیل کرده.\n"
                    "کد رو سریع بفرست:\n/code 123456"
                )
                if not _ig_challenge_event.wait(timeout=180):
                    raise Exception("زمان انتظار برای کد ایمیل تمام شد")
                # تلاش مجدد با کد
                c3 = _C()
                c3.challenge_code_handler = lambda u, ch: _ig_challenge_code
                c3.login(IG_USERNAME, IG_PASSWORD)
                return c3
            raise e

    try:
        client = await asyncio.wait_for(
            loop.run_in_executor(None, _try_email_login), timeout=200
        )
        ig_client = client
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception:
            pass
        await message.reply(f"✅ ورود با ایمیل موفق شد! اکانت: {IG_USERNAME}")
    except Exception as exc:
        await message.reply(
            f"❌ ورود با ایمیل شکست خورد:\n{exc}\n\n"
            f"اگر کد رو فرستادی و بازم خطا داد، یعنی اینستاگرام از IP سرور قبول نمی‌کنه."
        )


@bot.on_message(commands=["setcode"])
async def cmd_setcode(bot: Robot, message: Message) -> None:
    """مستقیم کد تایید رو ثبت کن"""
    global _ig_challenge_code
    if message.sender_id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await message.reply("مثال:\n/setcode 123456")
        return
    _ig_challenge_code = code
    _ig_challenge_event.set()
    await message.reply(f"✅ کد «{code}» ثبت شد.\nحالا دوباره /loginemail بزن.")


# --------------------------------------------------------------------------- #
# Text handler
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
        return

    uid = message.sender_id
    chat_id = message.chat_id
    add_user(uid)

    if not BOT_ENABLED and uid not in ADMIN_IDS:
        await message.reply(MSG_DISABLED)
        return

    # حالت انتخاب پست
    state = USER_STATE.get(uid)
    if state and state.get("mode") == "select_posts":
        await handle_post_selection(message, uid, chat_id, text, state)
        return

    # لینک
    url = extract_url(text)
    if url:
        status = await _new_status(chat_id, MSG_FETCHING)
        if INSTA_RE.search(url):
            _spawn(dl_ig(status, url, chat_id))
        else:
            _spawn(dl_ytdlp(status, url, DEFAULT_QUALITY, chat_id))
        return

    # @username
    ig_user = is_ig_username(text)
    if ig_user:
        status = await _new_status(chat_id, MSG_FETCHING)
        _spawn(dl_ig_username(status, ig_user, chat_id, uid))
        return

    await message.reply(MSG_SEND_LINK)


async def handle_post_selection(message: Message, uid: str, chat_id: str, text: str, state: dict) -> None:
    text_lower = text.strip().lower()

    if text_lower in ("بعدی", "next"):
        state["page"] = min(state["page"] + 1, (len(state["medias"]) - 1) // POSTS_PER_PAGE)
        await send_posts_page(chat_id, state)
        return
    if text_lower in ("قبلی", "prev"):
        state["page"] = max(0, state["page"] - 1)
        await send_posts_page(chat_id, state)
        return
    if text_lower in ("لغو", "cancel"):
        USER_STATE.pop(uid, None)
        await message.reply("❌ انتخاب لغو شد.")
        return

    numbers = [int(n) for n in NUMBERS_RE.findall(text)]
    if not numbers:
        await message.reply(MSG_INVALID_SELECTION)
        return

    medias = state["medias"]
    valid_indices = sorted(set(n for n in numbers if 1 <= n <= len(medias)))
    if not valid_indices:
        await message.reply(MSG_NO_VALID_POSTS)
        return
    if len(valid_indices) > MAX_SELECT:
        await message.reply(MSG_TOO_MANY_SELECTED)
        return

    selected = [medias[i - 1] for i in valid_indices]
    username = state["username"]
    USER_STATE.pop(uid, None)

    status = await _new_status(chat_id, MSG_SENDING_SELECTED.format(n=len(selected), username=username))
    _spawn(send_selected_medias(status, selected, chat_id, username))


async def send_posts_page(chat_id: str, state: dict) -> None:
    medias = state["medias"]
    page = state["page"]
    total = len(medias)
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    start = page * POSTS_PER_PAGE
    end = min(start + POSTS_PER_PAGE, total)

    lines = [format_post_line(i + 1, medias[i]) for i in range(start, end)]

    text = MSG_SELECT_PROMPT.format(
        username=state["username"],
        page=page + 1,
        total_pages=total_pages,
        start=start + 1,
        end=end,
        list="\n".join(lines),
        max_select=MAX_SELECT,
    )
    await bot.send_message(chat_id, text)


# --------------------------------------------------------------------------- #
# Download helpers (same as before)
# --------------------------------------------------------------------------- #
def _probe_formats(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 15}
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _available_heights(info: dict) -> list[int]:
    return sorted({int(f["height"]) for f in info.get("formats", []) if f.get("height")})


def _choose_height(requested: int, available: list[int]) -> Optional[int]:
    if not available:
        return None
    at_or_below = [h for h in available if h <= requested]
    return max(at_or_below) if at_or_below else min(available)


async def _resolve_format(loop, url, quality, is_instagram, status) -> str:
    if is_instagram:
        return "best"
    if quality == "audio":
        return "bestaudio/best"
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b"
    requested = int(quality)
    target = requested
    try:
        info = await asyncio.wait_for(loop.run_in_executor(None, _probe_formats, url), timeout=INFO_TIMEOUT)
        chosen = _choose_height(requested, _available_heights(info))
        if chosen:
            target = chosen
            if chosen != requested:
                await status.edit(MSG_DOWNGRADE.format(req=requested, chosen=chosen), force=True)
    except Exception as exc:
        log.warning("Format probe failed: %s", exc)
    return f"bv*[height<={target}][ext=mp4]+ba[ext=m4a]/bv*[height<={target}]+ba/b[height<={target}]/b"


def _cache_key(url: str, quality: str, is_instagram: bool) -> str:
    return f"{url}|{'best' if is_instagram else quality}"


def _store_in_cache(path: Path, key: str) -> Path:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        dest = CACHE_DIR / f"{digest}{path.suffix}"
        shutil.move(str(path), str(dest))
        CACHE[key] = dest
        while len(CACHE) > MAX_CACHE_ENTRIES:
            old_key, old_path = next(iter(CACHE.items()))
            CACHE.pop(old_key, None)
            try:
                old_path.unlink(missing_ok=True)
            except Exception:
                pass
        return dest
    except Exception:
        return path


async def _send_media(status: StatusHandle, path: Path, chat_id: str, quality: str) -> bool:
    size_mb = path.stat().st_size / 1_048_576
    if size_mb > MAX_FILE_MB:
        await status.edit(MSG_TOO_BIG.format(sz=size_mb, limit=MAX_FILE_MB), force=True)
        return False
    ext = path.suffix.lower()
    caption = f"🎵 {size_mb:.1f} MB" if quality == "audio" or ext in AUDIO_EXTS else f"{size_mb:.1f} MB"
    if ext not in AUDIO_EXTS:
        h = _probe_height(path)
        if h:
            caption += f" · {h}p"
    await status.edit(MSG_UPLOADING, force=True)
    try:
        if quality == "audio" or ext in AUDIO_EXTS:
            await bot.send_music(chat_id, path=str(path), text=caption)
        else:
            try:
                await bot.send_document(chat_id, path=str(path), text=caption, file_type="Video")
            except TypeError:
                await bot.send_document(chat_id, path=str(path), text=caption)
    except Exception as exc:
        log.error("Upload failed: %s", exc)
        await status.edit(MSG_FAILED, force=True)
        return False
    return True


async def _send_ig_media_list(paths: List[Path], chat_id: str, status: StatusHandle) -> int:
    sent = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        size_mb = p.stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            continue
        ext = p.suffix.lower()
        try:
            if ext in AUDIO_EXTS:
                await bot.send_music(chat_id, path=str(p))
            elif ext in {".jpg", ".jpeg", ".png", ".webp"}:
                await bot.send_image(chat_id, path=str(p))
            else:
                try:
                    await bot.send_document(chat_id, path=str(p), text=f"{size_mb:.1f} MB", file_type="Video")
                except TypeError:
                    await bot.send_document(chat_id, path=str(p), text=f"{size_mb:.1f} MB")
            sent += 1
        except Exception as exc:
            log.error("Send failed: %s", exc)
        finally:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return sent


def _build_ytdlp_opts(url, folder, fmt, quality, hook):
    opts = {
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
        opts["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--max-connection-per-server=16", "--min-split-size=1M"]}
    if quality == "audio":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    return opts


def _run_ytdlp(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


async def _download_with_fallback(loop, url, opts):
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _run_ytdlp, url, opts), timeout=DL_TIMEOUT)
    except yt_dlp.utils.DownloadError as exc:
        if "requested format is not available" in str(exc).lower():
            retry = dict(opts, format="best")
            retry.pop("postprocessors", None)
            return await asyncio.wait_for(loop.run_in_executor(None, _run_ytdlp, url, retry), timeout=DL_TIMEOUT)
        raise


async def dl_ytdlp(status: StatusHandle, url: str, quality: str, chat_id: str) -> None:
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
        except Exception:
            CACHE.pop(key, None)

    await status.edit(MSG_FETCHING, force=True)
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    last_edit = {"t": 0.0}

    def hook(data):
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
        text = f"⬇️ در حال دانلود {progress_bar(pct)} {pct:.0f}%\n{speed:.1f} MB/s" + (f" · {eta}s" if eta else "")
        try:
            loop.call_soon_threadsafe(lambda: _spawn(status.edit(text, interval=0)))
        except RuntimeError:
            pass

    try:
        fmt = await _resolve_format(loop, url, quality, is_instagram, status)
        opts = _build_ytdlp_opts(url, folder, fmt, quality, hook)
        await _download_with_fallback(loop, url, opts)
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() not in SKIP_EXTS]
        if not files:
            await status.edit(MSG_NO_OUTPUT, force=True)
            STATS["errors"] += 1
            return
        path = max(files, key=lambda f: f.stat().st_size)
        if path.stat().st_size > MAX_FILE_MB * 1_048_576:
            await status.edit(MSG_TOO_BIG.format(sz=path.stat().st_size / 1_048_576, limit=MAX_FILE_MB), force=True)
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
    except Exception:
        STATS["errors"] += 1
        await status.edit(MSG_FAILED, force=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def dl_ig(status: StatusHandle, url: str, chat_id: str) -> None:
    # اگر سشن مرده باشد، سعی کن دوباره لاگین کند
    if not ensure_ig_client():
        await dl_ytdlp(status, url, "best", chat_id)
        return
    await status.edit(MSG_FETCHING, force=True)
    loop = asyncio.get_running_loop()
    downloaded_paths = []
    try:
        match = re.search(r"/(p|reel|tv|stories)/([A-Za-z0-9_-]+)", url)
        if not match:
            await status.edit(MSG_IG_INVALID, force=True)
            return
        shortcode = match.group(2)

        def fetch_info():
            return ig_client.media_info(ig_client.media_pk_from_code(shortcode))

        post = await asyncio.wait_for(loop.run_in_executor(None, fetch_info), timeout=INFO_TIMEOUT)
        await status.edit(MSG_UPLOADING, force=True)

        if post.media_type == 2:
            p = await asyncio.wait_for(loop.run_in_executor(None, lambda: ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))), timeout=DL_TIMEOUT)
            downloaded_paths = [Path(p)]
        elif post.media_type == 1:
            p = await asyncio.wait_for(loop.run_in_executor(None, lambda: ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))), timeout=INFO_TIMEOUT)
            downloaded_paths = [Path(p)]
        elif post.media_type == 8:
            for res in (post.resources or []):
                try:
                    if res.media_type == 2:
                        rp = await asyncio.wait_for(loop.run_in_executor(None, lambda pk=res.pk: ig_client.video_download(pk, folder=str(DOWNLOAD_DIR))), timeout=DL_TIMEOUT)
                    else:
                        rp = await asyncio.wait_for(loop.run_in_executor(None, lambda pk=res.pk: ig_client.photo_download(pk, folder=str(DOWNLOAD_DIR))), timeout=INFO_TIMEOUT)
                    downloaded_paths.append(Path(rp))
                except Exception:
                    pass
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
    except Exception as exc:
        STATS["errors"] += 1
        log.warning("instagrapi failed, fallback: %s", exc)
        for p in downloaded_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        await dl_ytdlp(status, url, "best", chat_id)


async def dl_ig_username(status: StatusHandle, username: str, chat_id: str, uid: str) -> None:
    # اگر سشن مرده باشد، خودکار با یوزرنیم/پسورد دوباره لاگین می‌کند
    if not ensure_ig_client():
        await status.edit(MSG_IG_NO_SESSION, force=True)
        STATS["errors"] += 1
        return

    await status.edit(MSG_IG_FETCHING_USER.format(username=username), force=True)
    loop = asyncio.get_running_loop()
    try:
        def fetch():
            user_id = ig_client.user_id_from_username(username)
            return ig_client.user_medias(user_id, amount=MAX_IG_POSTS)
        medias = await asyncio.wait_for(loop.run_in_executor(None, fetch), timeout=INFO_TIMEOUT + 30)
    except Exception as exc:
        # ممکن است سشن وسط کار مرده باشد → یک‌بار دیگر تلاش کن
        log.warning("User lookup failed, trying re-login: %s", exc)
        if ensure_ig_client():
            try:
                def fetch2():
                    user_id = ig_client.user_id_from_username(username)
                    return ig_client.user_medias(user_id, amount=MAX_IG_POSTS)
                medias = await asyncio.wait_for(loop.run_in_executor(None, fetch2), timeout=INFO_TIMEOUT + 30)
            except Exception:
                await status.edit(MSG_IG_NOT_FOUND.format(username=username), force=True)
                STATS["errors"] += 1
                return
        else:
            await status.edit(MSG_IG_NOT_FOUND.format(username=username), force=True)
            STATS["errors"] += 1
            return

    if not medias:
        await status.edit(MSG_IG_NO_POSTS.format(username=username), force=True)
        STATS["errors"] += 1
        return

    USER_STATE[uid] = {"mode": "select_posts", "username": username, "medias": medias, "page": 0}
    await status.delete()
    await send_posts_page(chat_id, USER_STATE[uid])


async def send_selected_medias(status: StatusHandle, medias: list, chat_id: str, username: str) -> None:
    loop = asyncio.get_running_loop()
    total_sent = 0
    for media in medias:
        paths = []
        try:
            if media.media_type == 2:
                p = await asyncio.wait_for(loop.run_in_executor(None, lambda: ig_client.video_download(media.pk, folder=str(DOWNLOAD_DIR))), timeout=DL_TIMEOUT)
                paths = [Path(p)]
            elif media.media_type == 1:
                p = await asyncio.wait_for(loop.run_in_executor(None, lambda: ig_client.photo_download(media.pk, folder=str(DOWNLOAD_DIR))), timeout=INFO_TIMEOUT)
                paths = [Path(p)]
            elif media.media_type == 8:
                for res in (media.resources or []):
                    try:
                        if res.media_type == 2:
                            rp = await asyncio.wait_for(loop.run_in_executor(None, lambda pk=res.pk: ig_client.video_download(pk, folder=str(DOWNLOAD_DIR))), timeout=DL_TIMEOUT)
                        else:
                            rp = await asyncio.wait_for(loop.run_in_executor(None, lambda pk=res.pk: ig_client.photo_download(pk, folder=str(DOWNLOAD_DIR))), timeout=INFO_TIMEOUT)
                        paths.append(Path(rp))
                    except Exception:
                        pass
            sent = await _send_ig_media_list(paths, chat_id, status)
            total_sent += sent
        except Exception as exc:
            log.warning("Error downloading media: %s", exc)
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
    if total_sent:
        STATS["downloads"] += total_sent
        STATS["ig_downloads"] += total_sent
        await status.delete()
    else:
        await status.edit(MSG_NO_OUTPUT, force=True)
        STATS["errors"] += 1


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    init_instagram()
    log.warning(
        "Rubika bot v15 (no-inline) starting — engine: %s | IG: %s",
        "aria2c" if HAS_ARIA2 else "yt-dlp",
        "active" if ig_client else "off",
    )
    bot.run()
