# -*- coding: utf-8 -*-
"""Rubika Instagram Downloader Bot — v15

تغییرات اصلی نسبت به نسخه قبلی:
--------------------------------
* انتخاب پست با شماره: وقتی @username بفرستید، تا ۱۰۰ پست آخر لیست می‌شود
  و کاربر با نوشتن شماره (مثلاً 1 5 12 68) انتخاب می‌کند (حداکثر ۱۰ پست).
* پنل ادمین کامل‌تر: روشن/خاموش، آمار، مدیریت کاربران، حذف دسته‌جمعی، پیام همگانی.
* ذخیره پایدار کاربران در فایل JSON.
* صفحه‌بندی لیست پست‌ها (۱۰ پست در هر صفحه).
* کاملاً سازگار با کتابخانه rubka.
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
from rubka.button import InlineBuilder

try:
    from instagrapi import Client as InstaClient
    from instagrapi.types import Media
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
MAX_IG_POSTS = int(os.environ.get("MAX_IG_POSTS", "100"))   # تا ۱۰۰ پست
MAX_SELECT = 10                                               # حداکثر انتخاب همزمان
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
# Messages (Persian)
# --------------------------------------------------------------------------- #
MSG_SEND_LINK = (
    "🔗 یک لینک بفرست یا نام کاربری اینستاگرام را با @ بفرست "
    "تا برایت دانلود کنم.\n\n"
    "مثال: @realityofday"
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
MSG_IG_FETCHING_USER = "🔎 در حال دریافت آخرین پست‌های @{username}…\n(تا ۱۰۰ پست)"
MSG_SELECT_PROMPT = (
    "📋 لیست پست‌های @{username}\n"
    "صفحه {page}/{total_pages} — پست‌های {start} تا {end}\n\n"
    "{list}\n\n"
    "🔢 شماره پست‌هایی که می‌خوای رو بفرست (حداکثر {max_select} تا)\n"
    "مثال: 1 5 12 68\n"
    "یا برای صفحه بعد بنویس: بعدی"
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
    "باشه", "چطور", "چی", "چه", "بعدی", "قبلی", "صفحه",
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

# وضعیت انتخاب پست برای هر کاربر
# USER_STATE[uid] = {
#   "mode": "select_posts",
#   "username": str,
#   "medias": List[Media],
#   "page": int,
# }
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


# بارگذاری اولیه
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
    """یک خط خلاصه برای هر پست."""
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
        log.warning("Timed out waiting for IG verification code.")
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
        log.warning("Could not parse sessionid from cookies file: %s", exc)
    return ""


def init_instagram() -> None:
    from urllib.parse import unquote
    global ig_client
    if not HAS_INSTA:
        return

    session_id = unquote(IG_SESSION_ID) if IG_SESSION_ID else ""
    if not session_id and COOKIES_FILE.exists():
        session_id = _parse_sessionid_from_cookies(COOKIES_FILE)

    if session_id:
        log.warning("Attempting Instagram login via sessionid: %s...", session_id[:20])
        try:
            client = InstaClient()
            client.login_by_sessionid(session_id)
            try:
                IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                client.dump_settings(IG_SESSION_FILE)
            except Exception as exc:
                log.warning("Could not persist IG session: %s", exc)
            ig_client = client
            log.warning("✅ Instagram logged in via sessionid")
            _notify_admins("✅ ورود اینستاگرام با sessionid موفق بود.")
            return
        except Exception as exc:
            log.error("❌ sessionid login failed: %s", exc)
            _notify_admins(f"❌ sessionid کار نکرد: {exc}")

    if not (IG_USERNAME and IG_PASSWORD):
        return
    try:
        client = InstaClient()
        client.challenge_code_handler = _ig_challenge_code_handler
        if IG_SESSION_FILE.exists():
            try:
                client.load_settings(IG_SESSION_FILE)
                client.login(IG_USERNAME, IG_PASSWORD)
                ig_client = client
                log.warning("Instagram session restored from %s", IG_SESSION_FILE)
                _notify_admins("✅ سشن اینستاگرام بازیابی شد.")
                return
            except Exception as exc:
                log.warning("Saved IG session invalid, fresh login: %s", exc)
        client.login(IG_USERNAME, IG_PASSWORD)
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception as exc:
            log.warning("Could not persist IG session: %s", exc)
        ig_client = client
        log.warning("Instagram logged in as %s", IG_USERNAME)
        _notify_admins(f"✅ ورود اینستاگرام با اکانت {IG_USERNAME} موفق بود.")
    except Exception as exc:
        log.error("Instagram login failed: %s", exc)
        ig_client = None
        _notify_admins(f"❌ ورود اینستاگرام ناموفق بود: {exc}")


async def _async_init_instagram(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop
    await loop.run_in_executor(None, init_instagram)


# --------------------------------------------------------------------------- #
# Inline panels
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
        .row(
            InlineBuilder().button_simple("a_users", "کاربران"),
            InlineBuilder().button_simple("a_broadcast", "پیام همگانی"),
        )
        .row(InlineBuilder().button_simple("home", "بازگشت"))
        .build()
    )


def back_panel() -> dict:
    return InlineBuilder().row(
        InlineBuilder().button_simple("home", "بازگشت")
    ).build()


def users_panel() -> dict:
    return (
        InlineBuilder()
        .row(
            InlineBuilder().button_simple("a_users_list", "لیست کاربران"),
            InlineBuilder().button_simple("a_users_clear", "حذف همه"),
        )
        .row(InlineBuilder().button_simple("admin", "بازگشت"))
        .build()
    )


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
    await message.reply_inline(
        f"سلام {name} 👋\n\n"
        f"من ربات دانلودر هستم · موتور: {engine}{ig_note}\n\n"
        f"• لینک ویدیو بفرست (یوتیوب، تیک‌تاک، توییتر، اینستاگرام و …)\n"
        f"• یا نام کاربری اینستاگرام را با @ بفرست تا آخرین پست‌ها را ببینی و انتخاب کنی.\n\n"
        f"فایل با کیفیت تا {DEFAULT_QUALITY}p خودکار ارسال می‌شه.",
        main_panel(uid),
    )


@bot.on_message(commands=["myid"])
async def cmd_myid(bot: Robot, message: Message) -> None:
    await message.reply(f"🆔 آیدی شما: {message.sender_id}")


@bot.on_message(commands=["help"])
async def cmd_help(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    await message.reply_inline(
        f"📌 راهنمای ربات\n\n"
        f"① لینک ویدیو بفرست ← فایل تا {DEFAULT_QUALITY}p ارسال می‌شه.\n"
        f"② نام کاربری اینستاگرام با @ بفرست ← لیست تا ۱۰۰ پست آخر نمایش داده می‌شه\n"
        f"   بعد شماره پست‌هایی که می‌خوای رو بنویس (حداکثر {MAX_SELECT} تا).\n\n"
        f"مثال انتخاب: 1 5 12 68\n\n"
        f"پلتفرم‌های پشتیبانی‌شده: یوتیوب، تیک‌تاک، توییتر/X، اینستاگرام و هر "
        f"سایتی که yt-dlp پشتیبانی می‌کند.",
        main_panel(uid),
    )


@bot.on_message(commands=["igstatus"])
async def cmd_igstatus(bot: Robot, message: Message) -> None:
    from urllib.parse import unquote
    uid = message.sender_id
    if uid not in ADMIN_IDS:
        return
    sid_env = unquote(IG_SESSION_ID) if IG_SESSION_ID else ""
    sid_cookie = _parse_sessionid_from_cookies(COOKIES_FILE) if COOKIES_FILE.exists() else ""
    lines = [
        f"HAS_INSTA: {HAS_INSTA}",
        f"ig_client: {'✅ متصل' if ig_client else '❌ None'}",
        f"IG_SESSION_ID env: {'✅ ' + sid_env[:25] + '...' if sid_env else '❌ خالی'}",
        f"COOKIES_FILE: {'✅ ' + str(COOKIES_FILE) if COOKIES_FILE.exists() else '❌ پیدا نشد'}",
        f"sessionid در کوکی: {'✅ ' + sid_cookie[:25] + '...' if sid_cookie else '❌ پیدا نشد'}",
        f"IG_SESSION_FILE: {'✅ وجود داره' if IG_SESSION_FILE.exists() else '❌ نیست'}",
        f"کاربران ذخیره‌شده: {len(STATS['users'])}",
    ]
    await message.reply("\n".join(lines))


@bot.on_message(commands=["login"])
async def cmd_login(bot: Robot, message: Message) -> None:
    global _main_loop, ig_client
    uid = message.sender_id
    if uid not in ADMIN_IDS:
        return
    if not HAS_INSTA:
        await message.reply("❌ instagrapi نصب نیست.")
        return
    from urllib.parse import unquote
    sid = unquote(IG_SESSION_ID) if IG_SESSION_ID else _parse_sessionid_from_cookies(COOKIES_FILE)
    if not sid:
        await message.reply("❌ sessionid پیدا نشد. IG_SESSION_ID رو ست کن.")
        return

    await message.reply(f"🔄 تلاش login با sessionid:\n{sid[:30]}...")
    _main_loop = asyncio.get_running_loop()
    loop = asyncio.get_running_loop()

    if ig_client:
        await message.reply("✅ ig_client قبلاً متصله. نیازی به login مجدد نیست.\nاگه مشکل داری اول /igstatus بزن.")
        return

    def _try_login():
        from instagrapi import Client as _C
        c = _C()
        c.login_by_sessionid(sid)
        return c

    try:
        client = await asyncio.wait_for(
            loop.run_in_executor(None, _try_login), timeout=30
        )
        ig_client = client
        try:
            IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(IG_SESSION_FILE)
        except Exception:
            pass
        await message.reply("✅ ورود موفق! ig_client ست شد.")
    except Exception as exc:
        await message.reply(f"❌ login_by_sessionid شکست خورد:\n{exc}")


@bot.on_message(commands=["code"])
async def cmd_code(bot: Robot, message: Message) -> None:
    global _ig_challenge_code
    uid = message.sender_id
    if uid not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await message.reply("کد رو اینطوری بفرست: /code 123456")
        return
    _ig_challenge_code = code
    _ig_challenge_event.set()
    await message.reply("✅ کد ثبت شد، در حال تلاش برای ورود…")


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
    log.warning("Incoming message from sender_id=%s", uid)

    if not BOT_ENABLED and uid not in ADMIN_IDS:
        await message.reply(MSG_DISABLED)
        return

    # ----- حالت انتخاب پست -----
    state = USER_STATE.get(uid)
    if state and state.get("mode") == "select_posts":
        await handle_post_selection(message, uid, chat_id, text, state)
        return

    # ----- لینک -----
    url = extract_url(text)
    if url:
        status = await _new_status(chat_id, MSG_FETCHING)
        if INSTA_RE.search(url):
            _spawn(dl_ig(status, url, chat_id))
        else:
            _spawn(dl_ytdlp(status, url, DEFAULT_QUALITY, chat_id))
        return

    # ----- @username -----
    ig_user = is_ig_username(text)
    if ig_user:
        status = await _new_status(chat_id, MSG_FETCHING)
        _spawn(dl_ig_username(status, ig_user, chat_id, uid))
        return

    # ----- fallback -----
    await message.reply_inline(MSG_SEND_LINK, main_panel(uid))


async def handle_post_selection(
    message: Message, uid: str, chat_id: str, text: str, state: dict
) -> None:
    """پردازش انتخاب شماره پست یا دستور صفحه."""
    text_lower = text.strip().lower()

    # صفحه بعد / قبل
    if text_lower in ("بعدی", "next", "صفحه بعد"):
        state["page"] = min(state["page"] + 1, (len(state["medias"]) - 1) // POSTS_PER_PAGE)
        await send_posts_page(chat_id, uid, state)
        return
    if text_lower in ("قبلی", "prev", "صفحه قبل"):
        state["page"] = max(0, state["page"] - 1)
        await send_posts_page(chat_id, uid, state)
        return
    if text_lower in ("لغو", "cancel", "انصراف"):
        USER_STATE.pop(uid, None)
        await message.reply("❌ انتخاب لغو شد.")
        return

    # استخراج شماره‌ها
    numbers = [int(n) for n in NUMBERS_RE.findall(text)]
    if not numbers:
        await message.reply(MSG_INVALID_SELECTION)
        return

    # یکتا و محدود به بازه
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
    USER_STATE.pop(uid, None)  # پاک کردن حالت

    status = await _new_status(chat_id, MSG_SENDING_SELECTED.format(n=len(selected), username=username))
    _spawn(send_selected_medias(status, selected, chat_id, username))


async def send_posts_page(chat_id: str, uid: str, state: dict) -> None:
    medias = state["medias"]
    page = state["page"]
    total = len(medias)
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    start = page * POSTS_PER_PAGE
    end = min(start + POSTS_PER_PAGE, total)

    lines = []
    for i in range(start, end):
        lines.append(format_post_line(i + 1, medias[i]))

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
# Format / quality helpers (kept from original)
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
    except Exception as exc:
        log.warning("Format probe failed for %s: %s", url, exc)

    return (
        f"bv*[height<={target}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={target}]+ba/"
        f"b[height<={target}]/b"
    )


# --------------------------------------------------------------------------- #
# Cache
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
    except Exception as exc:
        log.warning("Cache store failed: %s", exc)
        return path


def _evict_cache() -> None:
    while len(CACHE) > MAX_CACHE_ENTRIES:
        old_key, old_path = next(iter(CACHE.items()))
        CACHE.pop(old_key, None)
        try:
            old_path.unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Upload helpers
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
            try:
                await bot.send_document(
                    chat_id, path=str(path), text=caption, file_type="Video"
                )
            except TypeError:
                await bot.send_document(chat_id, path=str(path), text=caption)
    except Exception as exc:
        log.error("Upload failed for %s: %s", path.name, exc)
        await status.edit(MSG_FAILED, force=True)
        return False
    return True


async def _send_ig_media_list(
    paths: List[Path],
    chat_id: str,
    status: StatusHandle,
) -> int:
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
        except Exception as exc:
            log.error("Send failed for %s: %s", p.name, exc)
        finally:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return sent


# --------------------------------------------------------------------------- #
# yt-dlp pipeline
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
            log.warning("Cached upload failed, re-downloading: %s", url)
            CACHE.pop(key, None)

    await status.edit(MSG_FETCHING, force=True)
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    last_edit: dict[str, float] = {"t": 0.0}

    def hook(data: dict) -> None:
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
    except Exception:
        STATS["errors"] += 1
        log.error("Unexpected error for %s", url, exc_info=True)
        await status.edit(MSG_FAILED, force=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Instagram single URL
# --------------------------------------------------------------------------- #
async def dl_ig(status: StatusHandle, url: str, chat_id: str) -> None:
    if not ig_client:
        await dl_ytdlp(status, url, "best", chat_id)
        return

    await status.edit(MSG_FETCHING, force=True)
    loop = asyncio.get_running_loop()
    downloaded_paths: List[Path] = []

    try:
        match = re.search(r"/(p|reel|tv|stories)/([A-Za-z0-9_-]+)", url)
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

        if post.media_type == 2:
            def dl_vid():
                return ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))
            p = await asyncio.wait_for(
                loop.run_in_executor(None, dl_vid), timeout=DL_TIMEOUT
            )
            downloaded_paths = [Path(p)]

        elif post.media_type == 1:
            def dl_photo():
                return ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))
            p = await asyncio.wait_for(
                loop.run_in_executor(None, dl_photo), timeout=INFO_TIMEOUT
            )
            downloaded_paths = [Path(p)]

        elif post.media_type == 8:
            resources = post.resources or []
            for res in resources:
                try:
                    if res.media_type == 2:
                        def dl_res_vid(pk=res.pk):
                            return ig_client.video_download(pk, folder=str(DOWNLOAD_DIR))
                        rp = await asyncio.wait_for(
                            loop.run_in_executor(None, dl_res_vid), timeout=DL_TIMEOUT
                        )
                    else:
                        def dl_res_img(pk=res.pk):
                            return ig_client.photo_download(pk, folder=str(DOWNLOAD_DIR))
                        rp = await asyncio.wait_for(
                            loop.run_in_executor(None, dl_res_img), timeout=INFO_TIMEOUT
                        )
                    downloaded_paths.append(Path(rp))
                except Exception as exc:
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

    except Exception as exc:
        STATS["errors"] += 1
        log.warning("instagrapi URL fetch failed (%s), falling back to yt-dlp", exc)
        for p in downloaded_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        await dl_ytdlp(status, url, "best", chat_id)


# --------------------------------------------------------------------------- #
# Username → list + select
# --------------------------------------------------------------------------- #
async def dl_ig_username(
    status: StatusHandle, username: str, chat_id: str, uid: str
) -> None:
    if not ig_client:
        await status.edit("🔄 در حال اتصال به اینستاگرام...", force=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, init_instagram)
        if not ig_client:
            await status.edit(MSG_IG_NO_SESSION, force=True)
            STATS["errors"] += 1
            return

    await status.edit(MSG_IG_FETCHING_USER.format(username=username), force=True)
    loop = asyncio.get_running_loop()

    try:
        def fetch_user_medias():
            user_id = ig_client.user_id_from_username(username)
            return ig_client.user_medias(user_id, amount=MAX_IG_POSTS)

        medias = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_user_medias),
            timeout=INFO_TIMEOUT + 30,
        )
    except Exception as exc:
        err_str = str(exc).lower()
        if "not found" in err_str or "usernamenotfound" in err_str:
            await status.edit(MSG_IG_NOT_FOUND.format(username=username), force=True)
        else:
            log.warning("User lookup @%s failed: %s", username, exc)
            await status.edit(MSG_IG_NOT_FOUND.format(username=username), force=True)
        STATS["errors"] += 1
        return

    if not medias:
        await status.edit(MSG_IG_NO_POSTS.format(username=username), force=True)
        STATS["errors"] += 1
        return

    # ذخیره وضعیت انتخاب
    USER_STATE[uid] = {
        "mode": "select_posts",
        "username": username,
        "medias": medias,
        "page": 0,
    }

    await status.delete()
    await send_posts_page(chat_id, uid, USER_STATE[uid])


async def send_selected_medias(
    status: StatusHandle, medias: list, chat_id: str, username: str
) -> None:
    loop = asyncio.get_running_loop()
    total_sent = 0

    for media in medias:
        paths_this_post: List[Path] = []
        try:
            if media.media_type == 2:
                def dl_v(pk=media.pk):
                    return ig_client.video_download(pk, folder=str(DOWNLOAD_DIR))
                p = await asyncio.wait_for(
                    loop.run_in_executor(None, dl_v), timeout=DL_TIMEOUT
                )
                paths_this_post = [Path(p)]

            elif media.media_type == 1:
                def dl_p(pk=media.pk):
                    return ig_client.photo_download(pk, folder=str(DOWNLOAD_DIR))
                p = await asyncio.wait_for(
                    loop.run_in_executor(None, dl_p), timeout=INFO_TIMEOUT
                )
                paths_this_post = [Path(p)]

            elif media.media_type == 8:
                resources = media.resources or []
                for res in resources:
                    try:
                        if res.media_type == 2:
                            def dl_rv(pk=res.pk):
                                return ig_client.video_download(pk, folder=str(DOWNLOAD_DIR))
                            rp = await asyncio.wait_for(
                                loop.run_in_executor(None, dl_rv), timeout=DL_TIMEOUT
                            )
                        else:
                            def dl_ri(pk=res.pk):
                                return ig_client.photo_download(pk, folder=str(DOWNLOAD_DIR))
                            rp = await asyncio.wait_for(
                                loop.run_in_executor(None, dl_ri), timeout=INFO_TIMEOUT
                            )
                        paths_this_post.append(Path(rp))
                    except Exception as exc:
                        log.warning("Album resource %s failed: %s", res.pk, exc)

            sent = await _send_ig_media_list(paths_this_post, chat_id, status)
            total_sent += sent

        except asyncio.TimeoutError:
            log.warning("Timeout downloading media %s from @%s", media.pk, username)
            for p in paths_this_post:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Error on media %s from @%s: %s", media.pk, username, exc)
            for p in paths_this_post:
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
# Callback handler (admin panel)
# --------------------------------------------------------------------------- #
def _extract_button_id(message: Message) -> str:
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
    log.warning("callback: button_id=%r", data)
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
        except Exception as exc:
            log.warning("safe_edit failed: %s", exc)

    if data == "home":
        await safe_edit("🏠 خانه", main_panel(uid))
    elif data == "help":
        await safe_edit(
            f"کافیست لینک ویدیو بفرستی یا نام کاربری اینستاگرام را با @ بفرستی.\n"
            f"بعد شماره پست‌هایی که می‌خوای رو بنویس (حداکثر {MAX_SELECT} تا).\n"
            f"فایل با کیفیت تا {DEFAULT_QUALITY}p ارسال می‌شه.",
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
            f"⏱ مدت فعالیت: {up // 3600}س {(up % 3600) // 60}د",
            admin_panel(),
        )
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await safe_edit(
            "✅ ربات روشن شد" if BOT_ENABLED else "⛔ ربات خاموش شد",
            admin_panel(),
        )
    elif data == "a_users" and uid in ADMIN_IDS:
        await safe_edit(
            f"👥 مدیریت کاربران\nتعداد فعلی: {len(STATS['users'])}",
            users_panel(),
        )
    elif data == "a_users_list" and uid in ADMIN_IDS:
        users = list(STATS["users"])
        if not users:
            await safe_edit("هیچ کاربری ثبت نشده.", users_panel())
        else:
            # نمایش حداکثر ۵۰ تا اول
            sample = users[:50]
            text = f"👥 لیست کاربران ({len(users)} نفر):\n\n" + "\n".join(sample)
            if len(users) > 50:
                text += f"\n\n... و {len(users) - 50} نفر دیگر"
            await safe_edit(text, users_panel())
    elif data == "a_users_clear" and uid in ADMIN_IDS:
        STATS["users"].clear()
        save_users(STATS["users"])
        await safe_edit("✅ همه کاربران حذف شدند.", users_panel())
    elif data == "a_broadcast" and uid in ADMIN_IDS:
        await safe_edit(
            "📢 برای ارسال پیام همگانی، پیام خودت رو با دستور زیر بفرست:\n\n"
            "/broadcast متن پیام شما\n\n"
            "پیام به همه کاربران ثبت‌شده ارسال می‌شود.",
            admin_panel(),
        )


@bot.on_message(commands=["broadcast"])
async def cmd_broadcast(bot: Robot, message: Message) -> None:
    uid = message.sender_id
    if uid not in ADMIN_IDS:
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
            await asyncio.sleep(0.15)  # کمی فاصله برای جلوگیری از محدودیت
        except Exception:
            fail += 1
    await message.reply(f"✅ ارسال تمام شد.\nموفق: {success}\nناموفق: {fail}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    init_instagram()
    log.warning(
        "Rubika bot v15 starting — engine: %s | IG: %s | Max posts: %s",
        "aria2c" if HAS_ARIA2 else "yt-dlp",
        "active" if ig_client else "off",
        MAX_IG_POSTS,
    )
    bot.run()
