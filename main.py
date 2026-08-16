#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK self-chat -> Telegram channel publisher.

Python: 3.10+
Dependency: requests

Environment:
  VK_TOKEN=...                 optional; can also be entered in the web UI
  PORT=8080
  CONFIG_FILE=./vk_config.json
  STATE_DB=./bot_state.sqlite3
  LOG_FILE=./bot.log

Telegram post format (the first two lines may be swapped):
  -1001234567890
  123456789:AA...
  Post text

A single VK photo/video attachment is supported.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from socketserver import ThreadingTCPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==================== CONFIG ====================

VK_API_VERSION = "5.199"
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
CONFIG_FILE = os.environ.get("CONFIG_FILE", "./vk_config.json")
STATE_DB = os.environ.get("STATE_DB", "./bot_state.sqlite3")
LOG_FILE = os.environ.get("LOG_FILE", "./bot.log")

TG_CHANNEL_COOLDOWN = int(os.environ.get("TG_CHANNEL_COOLDOWN", "30"))
TG_CHANNEL_HOURLY_LIMIT = int(os.environ.get("TG_CHANNEL_HOURLY_LIMIT", "6"))
TG_GLOBAL_HOURLY_LIMIT = int(os.environ.get("TG_GLOBAL_HOURLY_LIMIT", "15"))
TG_SAME_CONTENT_TTL = int(os.environ.get("TG_SAME_CONTENT_TTL", "86400"))
TG_MAX_TEXT = 4096
TG_MAX_CAPTION = 1024
TG_MAX_PHOTO_BYTES = 10 * 1024 * 1024
TG_MAX_VIDEO_BYTES = 50 * 1024 * 1024

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^(?:-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")

BOT_PAUSED = False
BOT_THREAD: Optional[threading.Thread] = None
BOT_THREAD_LOCK = threading.Lock()
ACTIVE_VK_TOKEN = ""
STOP_EVENT = threading.Event()
POST_LOCK = threading.Lock()
RECENT_VK_IDS: deque[int] = deque(maxlen=3000)
RECENT_VK_SET: set[int] = set()


def extract_vk_token(value: str) -> str:
    value = html.unescape((value or "").strip())
    if "access_token=" not in value:
        return value
    parsed = urlparse(value)
    values = parse_qs(parsed.query).get("access_token") or parse_qs(parsed.fragment).get("access_token")
    if values:
        return unquote(values[0]).strip()
    match = re.search(r"access_token=([^&\s]+)", value)
    return unquote(match.group(1)).strip() if match else value


VK_TOKEN = extract_vk_token(VK_TOKEN)


# ==================== LOGGING ====================

class SecretFilter(logging.Filter):
    TOKEN_IN_TEXT = re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b")
    VK_IN_TEXT = re.compile(r"\bvk1\.[A-Za-z0-9._-]+\b")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = self.TOKEN_IN_TEXT.sub("<TG_TOKEN_REDACTED>", message)
        message = self.VK_IN_TEXT.sub("<VK_TOKEN_REDACTED>", message)
        record.msg = message
        record.args = ()
        return True


log = logging.getLogger("vk-tg-publisher")
log.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(threadName)s: %(message)s", "%Y-%m-%d %H:%M:%S")
for handler in (
    logging.StreamHandler(),
    RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
):
    handler.setFormatter(formatter)
    handler.addFilter(SecretFilter())
    log.addHandler(handler)


# ==================== HTTP ====================

SESSION = requests.Session()
# Retries are allowed only for safe GET calls. POST is deliberately not retried:
# retrying sendMessage/sendPhoto after a timeout can duplicate a channel post.
retry = Retry(total=3, connect=3, read=2, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))
SESSION.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20))
SESSION.headers.update({"User-Agent": "VK-TG-Publisher/3.0"})


# ==================== PERSISTENT STATE / ANTISPAM ====================

def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(STATE_DB, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def init_db() -> None:
    with db_connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                vk_message_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL,
                channel TEXT NOT NULL,
                bot_fp TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_channel_time ON posts(channel, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_content ON posts(channel, content_hash, created_at);
            CREATE TABLE IF NOT EXISTS known_channels (
                bot_fp TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                username TEXT DEFAULT '',
                last_seen INTEGER NOT NULL,
                PRIMARY KEY(bot_fp, chat_id)
            );
            """
        )
        con.execute("DELETE FROM posts WHERE created_at < ?", (int(time.time()) - 30 * 86400,))


def token_fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def content_digest(chat_id: str, text: str, media: Optional[dict[str, Any]]) -> str:
    media_key = ""
    if media:
        media_key = f"{media.get('type','')}:{media.get('owner_id','')}:{media.get('id','')}"
    return hashlib.sha256(f"{chat_id}\0{text}\0{media_key}".encode()).hexdigest()


def reserve_post(vk_message_id: int, chat_id: str, fp: str, digest: str) -> tuple[bool, str]:
    now = int(time.time())
    with db_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT status FROM posts WHERE vk_message_id=?", (vk_message_id,)).fetchone()
        if existing:
            return False, f"Это VK-сообщение уже обработано (статус: {existing[0]}). Повтор запрещён."

        last = con.execute(
            "SELECT created_at FROM posts WHERE channel=? AND status IN ('pending','sent') ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if last and now - last[0] < TG_CHANNEL_COOLDOWN:
            return False, f"Защита от спама: подожди ещё {TG_CHANNEL_COOLDOWN - (now - last[0])} сек."

        channel_count = con.execute(
            "SELECT COUNT(*) FROM posts WHERE channel=? AND created_at>? AND status IN ('pending','sent')",
            (chat_id, now - 3600),
        ).fetchone()[0]
        if channel_count >= TG_CHANNEL_HOURLY_LIMIT:
            return False, f"Лимит канала: {TG_CHANNEL_HOURLY_LIMIT} постов в час."

        global_count = con.execute(
            "SELECT COUNT(*) FROM posts WHERE created_at>? AND status IN ('pending','sent')",
            (now - 3600,),
        ).fetchone()[0]
        if global_count >= TG_GLOBAL_HOURLY_LIMIT:
            return False, f"Глобальный лимит: {TG_GLOBAL_HOURLY_LIMIT} постов в час."

        duplicate = con.execute(
            "SELECT 1 FROM posts WHERE channel=? AND content_hash=? AND created_at>? AND status IN ('pending','sent') LIMIT 1",
            (chat_id, digest, now - TG_SAME_CONTENT_TTL),
        ).fetchone()
        if duplicate:
            return False, "Такой же пост уже отправлялся в этот канал за последние 24 часа."

        con.execute(
            "INSERT INTO posts(vk_message_id,created_at,channel,bot_fp,content_hash,status) VALUES(?,?,?,?,?,'pending')",
            (vk_message_id, now, chat_id, fp, digest),
        )
    return True, ""


def finish_post(vk_message_id: int, status: str, error: str = "") -> None:
    with db_connect() as con:
        con.execute("UPDATE posts SET status=?, error=? WHERE vk_message_id=?", (status, error[:500], vk_message_id))


def remember_channel(token: str, chat: dict[str, Any]) -> None:
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    with db_connect() as con:
        con.execute(
            """INSERT INTO known_channels(bot_fp,chat_id,title,username,last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(bot_fp,chat_id) DO UPDATE SET
               title=excluded.title, username=excluded.username, last_seen=excluded.last_seen""",
            (token_fp(token), chat_id, chat.get("title", ""), chat.get("username", ""), int(time.time())),
        )


# ==================== VK API ====================

def vk_api(method: str, params: Optional[dict[str, Any]] = None, token: Optional[str] = None) -> Optional[dict[str, Any]]:
    actual_token = token if token is not None else VK_TOKEN
    payload = dict(params or {})
    payload.update({"access_token": actual_token, "v": VK_API_VERSION})
    try:
        response = SESSION.post(f"https://api.vk.com/method/{method}", data=payload, timeout=(10, 30))
        data = response.json()
        if "error" in data:
            err = data["error"]
            log.error("VK API %s error %s: %s", method, err.get("error_code"), err.get("error_msg"))
            return None
        return data
    except Exception as exc:
        log.exception("VK API %s request failed: %s", method, exc)
        return None


def get_vk_user(token: Optional[str] = None) -> Optional[dict[str, Any]]:
    data = vk_api("users.get", token=token)
    items = (data or {}).get("response", [])
    return items[0] if items else None


def send_vk_message(peer_id: int, text: str) -> None:
    result = vk_api("messages.send", {
        "peer_id": peer_id,
        "message": text[:4000],
        "random_id": random.randint(1, 2_147_483_647),
    })
    if not result:
        log.error("Не удалось отправить ответ в VK peer_id=%s", peer_id)


def send_typing(peer_id: int) -> None:
    vk_api("messages.setActivity", {"peer_id": peer_id, "type": "typing"})


def get_vk_message(message_id: int) -> Optional[dict[str, Any]]:
    data = vk_api("messages.getById", {"message_ids": message_id, "extended": 0})
    response = (data or {}).get("response", {})
    items = response.get("items", []) if isinstance(response, dict) else response
    return items[0] if items else None


# ==================== TELEGRAM API ====================

def tg_api(token: str, method: str, data: Optional[dict[str, Any]] = None, files: Optional[dict[str, Any]] = None, timeout: int = 45) -> tuple[bool, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = SESSION.post(url, data=data or {}, files=files, timeout=(10, timeout))
        try:
            body = response.json()
        except ValueError:
            return False, f"HTTP {response.status_code}: Telegram вернул не JSON"
        if body.get("ok"):
            return True, body.get("result")
        description = body.get("description", "Unknown Telegram error")
        parameters = body.get("parameters") or {}
        if parameters.get("retry_after"):
            description += f"; retry_after={parameters['retry_after']} сек. Автоповтор отключён."
        return False, description
    except requests.Timeout:
        # Outcome is uncertain. Never retry automatically: it could duplicate a post.
        return False, "Тайм-аут Telegram. Результат неизвестен; автоповтор отключён во избежание дубля. Проверь канал."
    except Exception as exc:
        return False, f"Сетевая ошибка: {exc}"


def validate_bot_admin(token: str, chat_id: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
    ok, me = tg_api(token, "getMe")
    if not ok:
        return False, f"Токен бота не работает: {me}", None

    ok, chat = tg_api(token, "getChat", {"chat_id": chat_id})
    if not ok:
        return False, f"Канал не найден/бот не имеет доступа: {chat}", None

    chat_type = chat.get("type")
    if chat_type not in ("channel", "supergroup", "group"):
        return False, f"Нельзя публиковать: тип чата {chat_type!r}.", None

    ok, member = tg_api(token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]})
    if not ok:
        return False, f"Не удалось проверить права бота: {member}", None

    if member.get("status") not in ("administrator", "creator"):
        return False, f"Бот не администратор (status={member.get('status')}).", None
    if chat_type == "channel" and member.get("status") != "creator" and not member.get("can_post_messages", False):
        return False, "Бот администратор, но у него выключено право «Публикация сообщений».", None

    remember_channel(token, chat)
    return True, "", chat


def parse_tg_post(text: str) -> Optional[dict[str, str]]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = normalized.split("\n")
    if len(lines) < 3:
        return None
    first, second = lines[0].strip(), lines[1].strip()
    if CHAT_RE.fullmatch(first) and TOKEN_RE.fullmatch(second):
        chat_id, token = first, second
    elif TOKEN_RE.fullmatch(first) and CHAT_RE.fullmatch(second):
        token, chat_id = first, second
    else:
        return None
    return {"chat_id": chat_id, "token": token, "message": "\n".join(lines[2:]).strip()}


def choose_photo_url(photo: dict[str, Any]) -> Optional[str]:
    sizes = photo.get("sizes") or []
    sizes = sorted(sizes, key=lambda x: int(x.get("width", 0)) * int(x.get("height", 0)), reverse=True)
    return sizes[0].get("url") if sizes else None


def choose_video_url(video: dict[str, Any]) -> Optional[str]:
    owner_id, video_id = video.get("owner_id"), video.get("id")
    if owner_id is None or video_id is None:
        return None
    key = f"{owner_id}_{video_id}"
    if video.get("access_key"):
        key += f"_{video['access_key']}"
    data = vk_api("video.get", {"videos": key})
    items = ((data or {}).get("response") or {}).get("items", [])
    if not items:
        return None
    files = items[0].get("files") or {}
    candidates = []
    for name, url in files.items():
        match = re.fullmatch(r"mp4_(\d+)", name)
        if match and isinstance(url, str):
            candidates.append((int(match.group(1)), url))
    return max(candidates, default=(0, None))[1]


def extract_media(message: Optional[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], str]:
    attachments = (message or {}).get("attachments") or []
    supported = [a for a in attachments if a.get("type") in ("photo", "video")]
    if not supported:
        return None, ""
    warning = ""
    if len(supported) > 1:
        warning = "В сообщении несколько медиа; безопасно отправлено только первое."
    item = supported[0]
    kind = item["type"]
    obj = item.get(kind) or {}
    url = choose_photo_url(obj) if kind == "photo" else choose_video_url(obj)
    if not url:
        return None, f"Не удалось получить прямую ссылку на {kind} из VK. Возможно, файл приватный."
    return {
        "type": kind,
        "url": url,
        "owner_id": obj.get("owner_id", ""),
        "id": obj.get("id", ""),
    }, warning


def download_media(media: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    kind = media["type"]
    limit = TG_MAX_PHOTO_BYTES if kind == "photo" else TG_MAX_VIDEO_BYTES
    try:
        with SESSION.get(media["url"], stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > limit:
                return None, None, f"Файл слишком большой: {declared // 1024 // 1024} МБ, лимит {limit // 1024 // 1024} МБ."
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            suffix = mimetypes.guess_extension(mime) or (".jpg" if kind == "photo" else ".mp4")
            temp = tempfile.NamedTemporaryFile(prefix="vk_tg_", suffix=suffix, delete=False)
            total = 0
            try:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        temp.close()
                        os.unlink(temp.name)
                        return None, None, f"Файл превысил лимит {limit // 1024 // 1024} МБ."
                    temp.write(chunk)
                temp.close()
                if total == 0:
                    os.unlink(temp.name)
                    return None, None, "VK вернул пустой медиафайл."
                return temp.name, mime or None, None
            except Exception:
                temp.close()
                if os.path.exists(temp.name):
                    os.unlink(temp.name)
                raise
    except Exception as exc:
        return None, None, f"Не удалось скачать медиа из VK: {exc}"


def publish_tg(token: str, chat_id: str, text: str, media: Optional[dict[str, Any]]) -> tuple[bool, str]:
    if not media:
        ok, result = tg_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
        })
        return ok, "" if ok else str(result)

    path, mime, error = download_media(media)
    if error:
        return False, error
    assert path
    kind = media["type"]
    method = "sendPhoto" if kind == "photo" else "sendVideo"
    field = "photo" if kind == "photo" else "video"
    filename = os.path.basename(path)
    try:
        with open(path, "rb") as stream:
            data = {"chat_id": chat_id, "caption": text}
            if kind == "video":
                data["supports_streaming"] = "true"
            ok, result = tg_api(token, method, data, {field: (filename, stream, mime or "application/octet-stream")}, timeout=120)
            return ok, "" if ok else str(result)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ==================== COMMANDS ====================

HELP = """🚀 VK → Telegram publisher

Пост (ID и токен можно менять местами):
-1001234567890
123456789:AA...
Текст поста

К этому VK-сообщению можно прикрепить одно фото или видео.

Команды:
• тг проверить\nTOKEN\nCHAT_ID — проверить канал и права
• тг каналы\nTOKEN — показать каналы, которые Bot API уже видел
• статус — состояние и лимиты
• стоп / старт — пауза и продолжение

Защита: без автоповторов, дедупликация, пауза между постами, часовые лимиты и запрет одинакового поста на 24 часа."""


def command_check_channel(text: str) -> Optional[str]:
    lines = [x.strip() for x in text.replace("\r", "").split("\n")]
    if not lines or lines[0].lower() not in ("тг проверить", "tg check"):
        return None
    if len(lines) < 3:
        return "❌ Формат:\nтг проверить\nBOT_TOKEN\n-100CHANNEL_ID"
    token, chat_id = lines[1], lines[2]
    if not TOKEN_RE.fullmatch(token) or not CHAT_RE.fullmatch(chat_id):
        return "❌ Неверный токен или ID канала."
    ok, error, chat = validate_bot_admin(token, chat_id)
    if not ok:
        return f"❌ {error}"
    return f"✅ Всё готово.\nКанал: {chat.get('title') or chat.get('username') or chat_id}\nID: {chat.get('id')}\nБот — администратор и может публиковать."


def command_known_channels(text: str) -> Optional[str]:
    lines = [x.strip() for x in text.replace("\r", "").split("\n")]
    if not lines or lines[0].lower() not in ("тг каналы", "tg channels"):
        return None
    if len(lines) < 2 or not TOKEN_RE.fullmatch(lines[1]):
        return "❌ Формат:\nтг каналы\nBOT_TOKEN"
    token = lines[1]
    ok, me = tg_api(token, "getMe")
    if not ok:
        return f"❌ Токен не работает: {me}"

    # Telegram Bot API has no method that returns every channel where a bot is admin.
    # We can only inspect channels already seen in updates or stored after successful checks/posts.
    ok_webhook, webhook = tg_api(token, "getWebhookInfo")
    update_note = ""
    if ok_webhook and webhook.get("url"):
        update_note = "У бота включён webhook, поэтому getUpdates не вызывался."
    else:
        ok_updates, updates = tg_api(token, "getUpdates", {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(["channel_post", "edited_channel_post", "my_chat_member"]),
        })
        if ok_updates:
            for update in updates:
                chat = None
                for key in ("channel_post", "edited_channel_post"):
                    if update.get(key):
                        chat = update[key].get("chat")
                if update.get("my_chat_member"):
                    chat = update["my_chat_member"].get("chat")
                if chat and chat.get("type") == "channel":
                    remember_channel(token, chat)
        else:
            update_note = f"Не удалось прочитать updates: {updates}"

    with db_connect() as con:
        rows = con.execute(
            "SELECT chat_id,title,username FROM known_channels WHERE bot_fp=? ORDER BY last_seen DESC",
            (token_fp(token),),
        ).fetchall()

    admins = []
    for chat_id, title, username in rows[:50]:
        ok_member, member = tg_api(token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]})
        if ok_member and member.get("status") in ("administrator", "creator"):
            label = title or (f"@{username}" if username else "без названия")
            admins.append(f"• {label} — {chat_id}")

    header = "⚠️ Telegram не предоставляет API-список всех каналов бота. Ниже только уже обнаруженные каналы."
    body = "\n".join(admins) if admins else "Пока ни одного канала не обнаружено. Сделай «тг проверить» или отправь пост по ID."
    return f"{header}\n\n{body}" + (f"\n\nℹ️ {update_note}" if update_note else "")


def handle_tg_post(peer_id: int, vk_message_id: int, text: str) -> Optional[str]:
    parsed = parse_tg_post(text)
    if not parsed:
        return None

    token, chat_id, message = parsed["token"], parsed["chat_id"], parsed["message"]
    vk_message = get_vk_message(vk_message_id)
    media, media_warning = extract_media(vk_message)

    # An attachment can exist with empty text; without either, reject.
    if not message and not media:
        return "❌ Нужен текст, фото или видео."
    max_len = TG_MAX_CAPTION if media else TG_MAX_TEXT
    if len(message) > max_len:
        return f"❌ Текст слишком длинный: {len(message)} символов. Лимит: {max_len}."
    if media_warning and not media:
        return f"❌ {media_warning}"

    ok, error, chat = validate_bot_admin(token, chat_id)
    if not ok:
        return f"❌ Telegram: {error}"

    digest = content_digest(chat_id, message, media)
    with POST_LOCK:
        reserved, reason = reserve_post(vk_message_id, chat_id, token_fp(token), digest)
        if not reserved:
            return f"🛡 {reason}"

        log.info("TG publish reserved: vk_message_id=%s channel=%s media=%s", vk_message_id, chat_id, (media or {}).get("type", "none"))
        sent, send_error = publish_tg(token, chat_id, message, media)
        if sent:
            finish_post(vk_message_id, "sent")
            title = (chat or {}).get("title") or (chat or {}).get("username") or chat_id
            suffix = f"\n⚠️ {media_warning}" if media_warning else ""
            log.info("TG publish sent: vk_message_id=%s channel=%s", vk_message_id, chat_id)
            return f"✅ Пост отправлен: {title} ({chat_id}){suffix}"

        # A timeout is uncertain, so it remains blocked as 'unknown'. Other definite errors are 'failed'.
        status = "unknown" if "Результат неизвестен" in send_error else "failed"
        finish_post(vk_message_id, status, send_error)
        log.error("TG publish failed: vk_message_id=%s channel=%s error=%s", vk_message_id, chat_id, send_error)
        return f"❌ Telegram: {send_error}"


def process_message(peer_id: int, vk_message_id: int, text: str) -> None:
    global BOT_PAUSED
    stripped = (text or "").strip()
    lower = stripped.lower()

    if lower in ("старт", "start"):
        BOT_PAUSED = False
        send_vk_message(peer_id, "▶️ Бот запущен.")
        return
    if lower in ("стоп", "stop"):
        BOT_PAUSED = True
        send_vk_message(peer_id, "⏸ Бот на паузе. Команда «старт» продолжит работу.")
        return
    if lower in ("помощь", "help", "команды", "меню", "?"):
        send_vk_message(peer_id, HELP)
        return
    if lower == "статус":
        state = "пауза" if BOT_PAUSED else "работает"
        send_vk_message(peer_id, f"✅ Состояние: {state}\nПауза канала: {TG_CHANNEL_COOLDOWN} сек.\nЛимит канала: {TG_CHANNEL_HOURLY_LIMIT}/час\nОбщий лимит: {TG_GLOBAL_HOURLY_LIMIT}/час")
        return
    if BOT_PAUSED:
        send_vk_message(peer_id, "⏸ Бот на паузе. Напиши «старт».")
        return

    send_typing(peer_id)
    for handler in (command_check_channel, command_known_channels):
        result = handler(stripped)
        if result is not None:
            send_vk_message(peer_id, result)
            return

    result = handle_tg_post(peer_id, vk_message_id, stripped)
    if result is not None:
        send_vk_message(peer_id, result)
        return

    # Crucial fix: ordinary text is no longer treated as a malformed TG post.
    send_vk_message(peer_id, "❌ Команда не распознана. Напиши «помощь». Для TG-поста нужны первые две строки: ID канала и токен бота.")


# ==================== VK USER LONG POLL ====================

def remember_vk_id(message_id: int) -> bool:
    if message_id in RECENT_VK_SET:
        return False
    if len(RECENT_VK_IDS) == RECENT_VK_IDS.maxlen:
        old = RECENT_VK_IDS.popleft()
        RECENT_VK_SET.discard(old)
    RECENT_VK_IDS.append(message_id)
    RECENT_VK_SET.add(message_id)
    return True


def get_long_poll_server() -> Optional[dict[str, Any]]:
    data = vk_api("messages.getLongPollServer", {"lp_version": 3, "need_pts": 0})
    return (data or {}).get("response")


def listen_messages(user_id: int, owned_token: str) -> None:
    global VK_TOKEN
    log.info("VK listener starting for user_id=%s", user_id)
    server_data: Optional[dict[str, Any]] = None

    while not STOP_EVENT.is_set() and owned_token == ACTIVE_VK_TOKEN:
        try:
            if not server_data:
                server_data = get_long_poll_server()
                if not server_data:
                    time.sleep(5)
                    continue

            server = server_data["server"]
            if not server.startswith("http"):
                server = "https://" + server
            response = SESSION.get(server, params={
                "act": "a_check",
                "key": server_data["key"],
                "ts": server_data["ts"],
                "wait": 25,
                "mode": 2,
                "version": 3,
            }, timeout=(10, 35))
            data = response.json()

            failed = data.get("failed")
            if failed == 1:
                server_data["ts"] = data["ts"]
                continue
            if failed:
                log.warning("VK Long Poll failed=%s; reconnecting", failed)
                server_data = None
                continue

            server_data["ts"] = data["ts"]
            for update in data.get("updates", []):
                if not isinstance(update, list) or len(update) < 6 or update[0] != 4:
                    continue
                message_id = int(update[1])
                flags = int(update[2])
                peer_id = int(update[3])
                text = str(update[5] or "")

                # Ignore outgoing messages and every chat except Saved Messages.
                if flags & 2 or peer_id != user_id:
                    continue
                if not remember_vk_id(message_id):
                    log.warning("Duplicate VK event ignored: message_id=%s", message_id)
                    continue

                log.info("VK incoming: message_id=%s peer_id=%s chars=%s", message_id, peer_id, len(text))
                try:
                    process_message(peer_id, message_id, text)
                except Exception as exc:
                    log.exception("Message processing crashed: message_id=%s error=%s", message_id, exc)
                    send_vk_message(peer_id, f"❌ Внутренняя ошибка. ID события: {message_id}. Подробности записаны в лог.")

        except Exception as exc:
            log.exception("VK Long Poll loop error: %s", exc)
            server_data = None
            time.sleep(3)

    log.info("VK listener stopped for user_id=%s", user_id)


def start_listener(token: str, user_id: int) -> None:
    global VK_TOKEN, ACTIVE_VK_TOKEN, BOT_THREAD
    with BOT_THREAD_LOCK:
        VK_TOKEN = token
        ACTIVE_VK_TOKEN = token
        if BOT_THREAD and BOT_THREAD.is_alive():
            # The old thread exits after its current poll because ACTIVE_VK_TOKEN changed.
            log.info("Replacing existing VK listener")
        BOT_THREAD = threading.Thread(target=listen_messages, args=(user_id, token), daemon=True, name=f"vk-listener-{user_id}")
        BOT_THREAD.start()


# ==================== CONFIG / WEB UI ====================

def load_config() -> dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("Cannot load config: %s", exc)
        return {}


def save_config(config: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(CONFIG_FILE))
    os.makedirs(directory, exist_ok=True)
    temp = CONFIG_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, CONFIG_FILE)


HTML_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VK → Telegram</title><style>
body{margin:0;background:#10131d;color:#eef;font:16px system-ui;display:grid;place-items:center;min-height:100vh}.box{width:min(560px,90%);background:#1a2030;padding:28px;border-radius:18px;box-shadow:0 20px 60px #0008}h1{margin-top:0}input,button{box-sizing:border-box;width:100%;padding:14px;border-radius:10px;border:1px solid #39435d;background:#0f1420;color:white;margin-top:10px}button{background:#5865f2;border:0;font-weight:700;cursor:pointer}.note{color:#aeb8d0;font-size:14px}.ok{color:#62db8a}.err{color:#ff7777}code{word-break:break-all}
</style></head><body><main class="box"><h1>🚀 VK → Telegram</h1><p class="note">Введите VK user token. Он хранится локально в файле с правами 600.</p>
<form id="f"><input id="token" type="password" placeholder="VK token или OAuth-ссылка" required><button>Запустить</button></form><p id="s"></p>
<p class="note">После запуска откройте «Избранное» VK и напишите <b>помощь</b>.</p></main><script>
f.onsubmit=async(e)=>{e.preventDefault();s.className='';s.textContent='Проверяю…';try{let r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token.value})});let d=await r.json();s.className=d.ok?'ok':'err';s.textContent=d.ok?'✅ Запущено: '+d.name+' (ID '+d.user_id+')':'❌ '+d.error}catch(x){s.className='err';s.textContent='❌ '+x}}</script></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "VK-TG-Publisher/3.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("WEB %s - %s", self.client_address[0], fmt % args)

    def send_json(self, code: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_json(200, {"status": "ok", "listener_alive": bool(BOT_THREAD and BOT_THREAD.is_alive())})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/save":
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                self.send_json(400, {"ok": False, "error": "Неверный размер запроса"})
                return
            data = json.loads(self.rfile.read(length).decode())
            token = extract_vk_token(str(data.get("token", "")))
            user = get_vk_user(token)
            if not user:
                self.send_json(400, {"ok": False, "error": "VK-токен не работает или не имеет доступа к messages"})
                return
            save_config({"token": token, "user_id": user["id"]})
            start_listener(token, int(user["id"]))
            name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
            self.send_json(200, {"ok": True, "user_id": user["id"], "name": name})
        except Exception as exc:
            log.exception("WEB /save failed: %s", exc)
            self.send_json(400, {"ok": False, "error": str(exc)})


class ReusableThreadingServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    global VK_TOKEN
    init_db()
    config = load_config()
    configured_token = VK_TOKEN or extract_vk_token(str(config.get("token", "")))
    if configured_token:
        user = get_vk_user(configured_token)
        if user:
            start_listener(configured_token, int(user["id"]))
            log.info("Configured VK account: user_id=%s", user["id"])
        else:
            log.error("Saved VK token is invalid; open the web UI and replace it")

    with ReusableThreadingServer(("", PORT), WebHandler) as server:
        log.info("Web UI listening on 0.0.0.0:%s", PORT)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("Shutdown requested")
        finally:
            STOP_EVENT.set()


if __name__ == "__main__":
    main()
