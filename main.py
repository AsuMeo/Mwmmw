#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VK self-chat -> Telegram channel publisher.

Формат публикации (поддерживаются оба порядка первых двух строк):
    -1001234567890
    123456789:AA...
    Текст публикации

или:
    123456789:AA...
    -1001234567890
    Текст публикации

К сообщению ВК можно прикрепить одно фото или видео.

Команда поиска известных каналов:
    тг каналы
    123456789:AA...

ВАЖНО: Telegram Bot API не предоставляет метод «показать все каналы, где бот
администратор». Команда выше собирает каналы из доступных getUpdates и из
успешных публикаций этого приложения, затем повторно проверяет права бота.
"""

import hashlib
import html
import json
import logging
import mimetypes
import os
import random
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

import requests

# =============================================================================
# НАСТРОЙКИ
# =============================================================================

VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199")
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/tmp/vk_tg_bridge_config.json")
TG_STATE_FILE = os.environ.get("TG_STATE_FILE", "/tmp/vk_tg_bridge_tg_state.json")

# Безопасные консервативные лимиты. Их можно менять переменными окружения.
TG_MIN_INTERVAL = int(os.environ.get("TG_MIN_INTERVAL", "30"))       # секунд между попытками в один канал
TG_MAX_5_MIN = int(os.environ.get("TG_MAX_5_MIN", "3"))             # максимум за 5 минут
TG_MAX_HOUR = int(os.environ.get("TG_MAX_HOUR", "10"))              # максимум за час
TG_DUPLICATE_TTL = int(os.environ.get("TG_DUPLICATE_TTL", "86400")) # одинаковый пост блокируется сутки
TG_MAX_MEDIA_BYTES = int(os.environ.get("TG_MAX_MEDIA_BYTES", str(49 * 1024 * 1024)))
TG_ADMIN_CACHE_TTL = 300

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "VK-TG-Bridge/3.0"})

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s] %(levelname)s %(threadName)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vk-tg-bridge")

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,}$")
CHANNEL_RE = re.compile(r"^-100\d{6,}$")
USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,}$")


def extract_vk_token(value):
    value = (value or "").strip()
    if "access_token=" in value:
        match = re.search(r"access_token=([^&\s]+)", value)
        if match:
            return unquote(match.group(1))
    return value


VK_TOKEN = extract_vk_token(VK_TOKEN)


def mask_token(token):
    if not token:
        return "<пусто>"
    left = token.split(":", 1)[0]
    return f"{left}:***"


def token_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def atomic_json_save(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


# =============================================================================
# VK API
# =============================================================================

VK_TOKEN_LOCK = threading.RLock()
BOT_SENT_IDS = {}
BOT_SENT_LOCK = threading.Lock()
LISTENER_GENERATION = 0
LISTENER_LOCK = threading.Lock()
BOT_PAUSED = False
BOT_PAUSED_LOCK = threading.Lock()


def vk_api(method, params=None, timeout=30):
    params = dict(params or {})
    with VK_TOKEN_LOCK:
        token = VK_TOKEN
    if not token:
        return None
    params.update({"access_token": token, "v": VK_API_VERSION})
    try:
        response = HTTP.post(
            f"https://api.vk.com/method/{method}",
            data=params,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            err = data["error"]
            log.error("VK API %s: code=%s message=%s", method, err.get("error_code"), err.get("error_msg"))
            return None
        return data.get("response")
    except (requests.RequestException, ValueError) as exc:
        log.error("VK API %s network/json error: %s", method, exc)
        return None


def remember_bot_message(message_id):
    if message_id is None:
        return
    now = time.time()
    with BOT_SENT_LOCK:
        BOT_SENT_IDS[int(message_id)] = now
        stale = [mid for mid, stamp in BOT_SENT_IDS.items() if now - stamp > 600]
        for mid in stale:
            BOT_SENT_IDS.pop(mid, None)


def was_bot_message(message_id):
    with BOT_SENT_LOCK:
        return int(message_id) in BOT_SENT_IDS


def send_message(peer_id, text):
    response = vk_api("messages.send", {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randint(1, 2_147_483_647),
    })
    if isinstance(response, int):
        remember_bot_message(response)
        return True
    return False


def send_typing(peer_id):
    vk_api("messages.setActivity", {"peer_id": peer_id, "type": "typing"}, timeout=10)


def get_vk_user():
    response = vk_api("users.get")
    if response and isinstance(response, list):
        return response[0]
    return None


def get_vk_message(message_id):
    response = vk_api("messages.getById", {"message_ids": int(message_id), "extended": 0})
    if isinstance(response, dict):
        items = response.get("items", [])
        return items[0] if items else None
    return None


# =============================================================================
# TELEGRAM API
# =============================================================================


def tg_api(token, method, data=None, files=None, timeout=35):
    """Возвращает (ok, result_or_error, ambiguous).

    ambiguous=True означает сетевую неопределённость: сервер мог принять запрос,
    поэтому автоматический повтор намеренно запрещён, чтобы не задублировать пост.
    """
    try:
        response = HTTP.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=data or {},
            files=files,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            return False, f"Telegram вернул HTTP {response.status_code} без JSON", False
        if payload.get("ok"):
            return True, payload.get("result"), False
        description = payload.get("description", "неизвестная ошибка Telegram")
        parameters = payload.get("parameters") or {}
        if parameters.get("retry_after"):
            description += f"; повторить не раньше чем через {parameters['retry_after']} сек."
        return False, description, False
    except requests.Timeout:
        return False, "тайм-аут Telegram: результат неизвестен; повтор заблокирован от возможного дубля", True
    except requests.RequestException as exc:
        return False, f"сетевая ошибка Telegram: {exc}; результат может быть неизвестен", True


def telegram_identity(token):
    ok, result, _ = tg_api(token, "getMe", timeout=15)
    if not ok:
        return None, str(result)
    return result, None


ADMIN_CACHE = {}
ADMIN_CACHE_LOCK = threading.Lock()


def verify_channel_admin(token, chat_id):
    cache_key = (token_key(token), str(chat_id))
    now = time.time()
    with ADMIN_CACHE_LOCK:
        cached = ADMIN_CACHE.get(cache_key)
        if cached and now - cached[0] < TG_ADMIN_CACHE_TTL:
            return cached[1], cached[2], cached[3]

    me, error = telegram_identity(token)
    if not me:
        return False, None, f"токен бота не прошёл getMe: {error}"

    ok, member, _ = tg_api(token, "getChatMember", {
        "chat_id": str(chat_id),
        "user_id": str(me["id"]),
    }, timeout=15)
    if not ok:
        return False, None, f"не удалось проверить права: {member}"

    status = member.get("status")
    can_post = member.get("can_post_messages", True)
    allowed = status in ("administrator", "creator") and can_post is not False
    error = None if allowed else f"бот не может публиковать: status={status}, can_post_messages={can_post}"

    with ADMIN_CACHE_LOCK:
        ADMIN_CACHE[cache_key] = (now, allowed, me, error)
    return allowed, me, error


# =============================================================================
# ЗАЩИТА ОТ СПАМА И ДУБЛЕЙ
# =============================================================================

class TelegramGuard:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        raw = json_load(path, {})
        self.history = defaultdict(deque)
        for key, values in (raw.get("history") or {}).items():
            self.history[key] = deque(float(x) for x in values)
        self.duplicates = {
            key: {fingerprint: float(stamp) for fingerprint, stamp in values.items()}
            for key, values in (raw.get("duplicates") or {}).items()
        }
        self.channels = raw.get("channels") or {}

    def _prune(self, now):
        for key in list(self.history):
            q = self.history[key]
            while q and now - q[0] > 3600:
                q.popleft()
            if not q:
                self.history.pop(key, None)
        for key in list(self.duplicates):
            values = self.duplicates[key]
            for fp, stamp in list(values.items()):
                if now - stamp > TG_DUPLICATE_TTL:
                    values.pop(fp, None)
            if not values:
                self.duplicates.pop(key, None)

    def _save(self):
        atomic_json_save(self.path, {
            "history": {key: list(values) for key, values in self.history.items()},
            "duplicates": self.duplicates,
            "channels": self.channels,
        })

    @staticmethod
    def route_key(token, chat_id):
        return f"{token_key(token)}:{chat_id}"

    def reserve(self, token, chat_id, fingerprint):
        now = time.time()
        key = self.route_key(token, chat_id)
        with self.lock:
            self._prune(now)
            q = self.history[key]
            dup = self.duplicates.setdefault(key, {})

            if fingerprint in dup:
                left = max(1, int(TG_DUPLICATE_TTL - (now - dup[fingerprint])))
                return False, f"точно такой же пост уже обрабатывался; защита от дублей ещё {left} сек."
            if q and now - q[-1] < TG_MIN_INTERVAL:
                left = max(1, int(TG_MIN_INTERVAL - (now - q[-1])))
                return False, f"слишком быстро: подожди {left} сек."
            if sum(1 for stamp in q if now - stamp <= 300) >= TG_MAX_5_MIN:
                return False, f"лимит: не больше {TG_MAX_5_MIN} публикаций за 5 минут в один канал"
            if len(q) >= TG_MAX_HOUR:
                return False, f"лимит: не больше {TG_MAX_HOUR} публикаций за час в один канал"

            # Резервируем ДО сетевого вызова — два потока не отправят один пост.
            q.append(now)
            dup[fingerprint] = now
            self._save()
            return True, None

    def release_duplicate(self, token, chat_id, fingerprint):
        """Снимается только при однозначном отказе Telegram/ошибке до отправки."""
        key = self.route_key(token, chat_id)
        with self.lock:
            self.duplicates.get(key, {}).pop(fingerprint, None)
            self._save()

    def remember_channel(self, token, chat):
        key = token_key(token)
        chat_id = str(chat.get("id"))
        with self.lock:
            bucket = self.channels.setdefault(key, {})
            bucket[chat_id] = {
                "id": chat.get("id"),
                "title": chat.get("title") or "Без названия",
                "username": chat.get("username"),
                "seen_at": int(time.time()),
            }
            self._save()

    def known_channels(self, token):
        with self.lock:
            return list((self.channels.get(token_key(token)) or {}).values())


TG_GUARD = TelegramGuard(TG_STATE_FILE)


# =============================================================================
# РАЗБОР TG-КОМАНД
# =============================================================================


def is_bot_token(value):
    return bool(TOKEN_RE.fullmatch((value or "").strip()))


def is_channel(value):
    value = (value or "").strip()
    return bool(CHANNEL_RE.fullmatch(value) or USERNAME_RE.fullmatch(value))


def parse_tg_post(text):
    """Не принимает обычный текст за TG-команду. Поддерживает оба порядка."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = normalized.split("\n")
    if len(lines) < 3:
        return None

    first, second = lines[0].strip(), lines[1].strip()
    if is_channel(first) and is_bot_token(second):
        chat_id, token = first, second
    elif is_bot_token(first) and is_channel(second):
        token, chat_id = first, second
    else:
        return None

    message = "\n".join(lines[2:]).strip()
    if not message:
        return {"error": "текст публикации пуст"}
    if len(message) > 4096:
        return {"error": "текст длиннее 4096 символов"}
    return {"chat_id": chat_id, "token": token, "message": message}


def parse_channels_command(text):
    lines = [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]
    if len(lines) != 2:
        return None
    if lines[0].lower() not in ("тг каналы", "tg channels", "телеграм каналы"):
        return None
    return lines[1] if is_bot_token(lines[1]) else "INVALID"


# =============================================================================
# ВЛОЖЕНИЯ ВК -> TELEGRAM
# =============================================================================


def largest_photo_url(photo):
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda s: int(s.get("width", 0)) * int(s.get("height", 0)))
    return best.get("url") or best.get("src")


def resolve_video_url(video):
    files = video.get("files") or {}
    candidates = []
    for name, url in files.items():
        match = re.fullmatch(r"mp4_(\d+)", name)
        if match and url:
            candidates.append((int(match.group(1)), url))
    if candidates:
        return max(candidates)[1]

    owner_id, video_id = video.get("owner_id"), video.get("id")
    if owner_id is None or video_id is None:
        return None
    descriptor = f"{owner_id}_{video_id}"
    if video.get("access_key"):
        descriptor += f"_{video['access_key']}"
    response = vk_api("video.get", {"videos": descriptor})
    if isinstance(response, dict) and response.get("items"):
        return resolve_video_url(response["items"][0])
    return None


def extract_supported_media(vk_message):
    supported = []
    for attachment in (vk_message or {}).get("attachments", []):
        kind = attachment.get("type")
        obj = attachment.get(kind) or {}
        if kind == "photo":
            url = largest_photo_url(obj)
            if url:
                supported.append({"kind": "photo", "url": url, "name": "photo.jpg"})
        elif kind == "video":
            url = resolve_video_url(obj)
            if url:
                supported.append({"kind": "video", "url": url, "name": "video.mp4"})
            else:
                supported.append({"kind": "video_unavailable", "url": None, "name": None})
    return supported


def download_media(media):
    suffix = os.path.splitext(media.get("name") or "media.bin")[1] or ".bin"
    temp = tempfile.NamedTemporaryFile(prefix="vk_tg_", suffix=suffix, delete=False)
    path = temp.name
    total = 0
    try:
        with HTTP.get(media["url"], stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > TG_MAX_MEDIA_BYTES:
                raise ValueError(f"файл больше лимита {TG_MAX_MEDIA_BYTES // 1024 // 1024} МБ")
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > TG_MAX_MEDIA_BYTES:
                    raise ValueError(f"файл больше лимита {TG_MAX_MEDIA_BYTES // 1024 // 1024} МБ")
                temp.write(chunk)
        temp.close()
        if total == 0:
            raise ValueError("ВК вернул пустой файл")
        return path
    except Exception:
        temp.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def post_to_telegram(parsed, vk_message):
    token = parsed["token"]
    chat_id = parsed["chat_id"]
    message = parsed["message"]
    media_list = extract_supported_media(vk_message)

    if len(media_list) > 1:
        return "❌ Безопасная отправка разрешает только одно фото/видео за пост. Убери лишние вложения."
    if media_list and media_list[0]["kind"] == "video_unavailable":
        return "❌ ВК не отдал прямой файл видео. Прикрепи видео как файл/документ либо отправь фото."
    if media_list and len(message) > 1024:
        return "❌ С вложением текст должен быть не длиннее 1024 символов (лимит подписи Telegram)."

    media = media_list[0] if media_list else None
    media_mark = ""
    if media:
        media_mark = hashlib.sha256((media["kind"] + "|" + media["url"]).encode()).hexdigest()
    fingerprint = hashlib.sha256((chat_id + "\n" + message + "\n" + media_mark).encode("utf-8")).hexdigest()

    allowed, me, error = verify_channel_admin(token, chat_id)
    if not allowed:
        return f"❌ Telegram: {error}"

    reserved, reason = TG_GUARD.reserve(token, chat_id, fingerprint)
    if not reserved:
        return f"🛡 Публикация остановлена защитой: {reason}"

    log.info("TG publish reserved: bot=%s chat=%s text_len=%d media=%s", mask_token(token), chat_id, len(message), media["kind"] if media else "none")

    if not media:
        ok, result, ambiguous = tg_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        })
    else:
        try:
            path = download_media(media)
        except Exception as exc:
            TG_GUARD.release_duplicate(token, chat_id, fingerprint)
            return f"❌ Не удалось скачать вложение ВК: {exc}"
        try:
            method = "sendPhoto" if media["kind"] == "photo" else "sendVideo"
            field = "photo" if media["kind"] == "photo" else "video"
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as fh:
                ok, result, ambiguous = tg_api(token, method, {
                    "chat_id": chat_id,
                    "caption": message,
                }, files={field: (media["name"], fh, mime)}, timeout=90)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    if not ok:
        if not ambiguous:
            TG_GUARD.release_duplicate(token, chat_id, fingerprint)
        log.error("TG publish failed: bot=%s chat=%s error=%s", mask_token(token), chat_id, result)
        return f"❌ Telegram не отправил пост: {result}"

    chat = (result or {}).get("chat") or {"id": chat_id}
    TG_GUARD.remember_channel(token, chat)
    message_id = (result or {}).get("message_id", "?")
    log.info("TG publish success: chat=%s message_id=%s", chat_id, message_id)
    return f"✅ Пост отправлен в {chat.get('title') or chat_id}\nID сообщения: {message_id}\n🛡 Следующий пост в этот канал — не раньше чем через {TG_MIN_INTERVAL} сек."


def discover_channels(token):
    me, error = telegram_identity(token)
    if not me:
        return f"❌ Неверный токен или Telegram недоступен: {error}"

    candidates = {str(item["id"]): item for item in TG_GUARD.known_channels(token)}
    ok, webhook, _ = tg_api(token, "getWebhookInfo", timeout=15)
    webhook_note = ""
    if ok and webhook.get("url"):
        webhook_note = "\n⚠️ У бота установлен webhook; getUpdates недоступен. Показаны только уже известные приложению каналы."
    else:
        ok, updates, _ = tg_api(token, "getUpdates", {"timeout": "0", "limit": "100"}, timeout=20)
        if ok:
            for update in updates:
                for key in ("channel_post", "edited_channel_post"):
                    chat = (update.get(key) or {}).get("chat")
                    if chat and chat.get("type") == "channel":
                        candidates[str(chat["id"])] = chat
                change = update.get("my_chat_member") or update.get("chat_member") or {}
                chat = change.get("chat")
                if chat and chat.get("type") == "channel":
                    candidates[str(chat["id"])] = chat
        else:
            webhook_note = f"\n⚠️ getUpdates не сработал: {updates}"

    verified = []
    for chat_id, chat in candidates.items():
        allowed, _, _ = verify_channel_admin(token, chat_id)
        if allowed:
            ok, full_chat, _ = tg_api(token, "getChat", {"chat_id": chat_id}, timeout=15)
            if ok:
                chat = full_chat
            TG_GUARD.remember_channel(token, chat)
            verified.append(chat)

    if not verified:
        return (
            f"ℹ️ Бот @{me.get('username', me.get('id'))}: подтверждённых каналов пока не найдено.\n"
            "Bot API не умеет выдавать полный список каналов. Напиши один пост с ID канала — после успешной проверки канал запомнится."
            + webhook_note
        )

    rows = [f"✅ Каналы, где подтверждено право публикации для @{me.get('username', me.get('id'))}:"]
    for chat in verified:
        username = f" (@{chat['username']})" if chat.get("username") else ""
        rows.append(f"• {chat.get('title', 'Без названия')}{username}\n  ID: {chat.get('id')}")
    rows.append("\nВажно: это известные боту каналы, а не гарантированно полный список Telegram.")
    if webhook_note:
        rows.append(webhook_note)
    return "\n".join(rows)


# =============================================================================
# ОСТАЛЬНЫЕ КОМАНДЫ
# =============================================================================

HELP = f"""📋 Команды

📤 Пост в Telegram (оба порядка поддерживаются):
-1001234567890
BOT_TOKEN
Текст поста

Можно приложить ОДНО фото или видео из ВК.

🔎 Известные каналы бота:
тг каналы
BOT_TOKEN

⏸ стоп — пауза
▶️ старт — снять паузу
🔍 поиск <запрос>
📖 вики <запрос>
🌤 погода <город>
💰 курс

🛡 Защита канала:
• минимум {TG_MIN_INTERVAL} сек. между постами;
• максимум {TG_MAX_5_MIN} за 5 минут;
• максимум {TG_MAX_HOUR} за час;
• точный дубль блокируется на {TG_DUPLICATE_TTL // 3600} ч.;
• перед отправкой проверяются права администратора;
• при сетевой неопределённости нет автоматического повтора.
"""


def search_duckduckgo(query):
    try:
        response = HTTP.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "ru-ru"},
            headers={"Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=20,
        )
        response.raise_for_status()
        matches = re.findall(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', response.text, re.I | re.S)
        rows = []
        for link, title in matches[:5]:
            if "uddg=" in link:
                link = unquote(link.split("uddg=", 1)[1].split("&", 1)[0])
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            rows.append(f"📌 {title}\n🔗 {html.unescape(link)}")
        return "\n\n".join(rows) if rows else "❌ Ничего не найдено"
    except Exception as exc:
        return f"❌ Ошибка поиска: {exc}"


def simple_command(text):
    low = text.lower().strip()
    if low in ("помощь", "help", "команды", "?", "меню"):
        return HELP
    if low.startswith("поиск "):
        return search_duckduckgo(text.split(" ", 1)[1].strip())
    if low.startswith("вики "):
        query = text.split(" ", 1)[1].strip()
        try:
            response = HTTP.get(f"https://ru.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'))}", timeout=15)
            if response.ok:
                data = response.json()
                return f"📖 {data.get('title', query)}\n\n{data.get('extract', 'Нет описания')}\n\n🔗 {data.get('content_urls', {}).get('desktop', {}).get('page', '')}"
        except Exception:
            pass
        return search_duckduckgo(f"википедия {query}")
    if low.startswith("погода "):
        city = text.split(" ", 1)[1].strip()
        try:
            response = HTTP.get(f"https://wttr.in/{quote(city)}", params={"format": "3", "lang": "ru"}, timeout=15)
            return f"🌤 {response.text.strip()}" if response.ok else "❌ Погода недоступна"
        except Exception as exc:
            return f"❌ Ошибка погоды: {exc}"
    if low in ("курс", "валюта", "usd", "eur"):
        try:
            data = HTTP.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=15).json()
            return f"💰 ЦБ РФ\nUSD: {data['Valute']['USD']['Value']:.2f} ₽\nEUR: {data['Valute']['EUR']['Value']:.2f} ₽"
        except Exception as exc:
            return f"❌ Ошибка курсов: {exc}"
    return "ℹ️ Команда не распознана. Напиши «помощь»."


# =============================================================================
# ОБРАБОТКА СООБЩЕНИЙ И LONG POLL
# =============================================================================

RECENT_INCOMING = {}
RECENT_LOCK = threading.Lock()
START_TIME = int(time.time())


def incoming_duplicate(message_id):
    now = time.time()
    with RECENT_LOCK:
        for mid, stamp in list(RECENT_INCOMING.items()):
            if now - stamp > 900:
                RECENT_INCOMING.pop(mid, None)
        if message_id in RECENT_INCOMING:
            return True
        RECENT_INCOMING[message_id] = now
        return False


def process_vk_message(peer_id, message_id, text):
    global BOT_PAUSED
    text = (text or "").strip()
    low = text.lower()

    # «старт» проверяется ДО состояния паузы — это исправляет старую вечную паузу.
    if low in ("старт", "start"):
        with BOT_PAUSED_LOCK:
            BOT_PAUSED = False
        return "▶️ Бот запущен."
    if low in ("стоп", "stop"):
        with BOT_PAUSED_LOCK:
            BOT_PAUSED = True
        return "⏸ Бот на паузе. Напиши «старт», чтобы продолжить."

    with BOT_PAUSED_LOCK:
        paused = BOT_PAUSED
    if paused:
        return None

    channel_token = parse_channels_command(text)
    if channel_token == "INVALID":
        return "❌ Формат: тг каналы\\nBOT_TOKEN"
    if channel_token:
        return discover_channels(channel_token)

    parsed = parse_tg_post(text)
    if parsed:
        if parsed.get("error"):
            return f"❌ {parsed['error']}"
        vk_message = get_vk_message(message_id) or {"attachments": []}
        return post_to_telegram(parsed, vk_message)

    # Обычные команды больше НЕ попадают в ошибку формата Telegram.
    return simple_command(text)


def get_long_poll_server():
    response = vk_api("messages.getLongPollServer", {"lp_version": 3, "need_pts": 0})
    return response if isinstance(response, dict) else None


def listen_messages(user_id, generation):
    log.info("VK listener #%s started for user_id=%s", generation, user_id)
    first_poll = True
    server_data = None

    while True:
        with LISTENER_LOCK:
            if generation != LISTENER_GENERATION:
                log.info("VK listener #%s stopped: replaced by newer listener", generation)
                return

        if not server_data:
            server_data = get_long_poll_server()
            if not server_data:
                time.sleep(5)
                continue

        try:
            response = HTTP.get(
                f"https://{server_data['server']}",
                params={
                    "act": "a_check",
                    "key": server_data["key"],
                    "ts": server_data["ts"],
                    "wait": 25,
                    "mode": 2,
                    "version": 3,
                },
                timeout=35,
            )
            data = response.json()

            if "failed" in data:
                if data["failed"] == 1:
                    server_data["ts"] = data["ts"]
                else:
                    server_data = None
                continue

            server_data["ts"] = data["ts"]
            updates = data.get("updates", [])
            if first_poll:
                first_poll = False
                if updates:
                    log.info("Skipped %d queued events at listener startup", len(updates))
                continue

            for update in updates:
                if not update or update[0] != 4 or len(update) < 6:
                    continue
                message_id = int(update[1])
                peer_id = int(update[3])
                timestamp = int(update[4])
                text = str(update[5] or "")

                # В чате с самим собой пользовательские сообщения имеют outgoing-флаг.
                # Поэтому старое `if flags & 2: continue` ломало ВСЕ команды.
                if peer_id != int(user_id):
                    continue
                if was_bot_message(message_id):
                    continue
                if timestamp < START_TIME - 60:
                    continue
                if incoming_duplicate(message_id):
                    continue

                log.info("VK request: message_id=%s text_len=%d", message_id, len(text))
                send_typing(peer_id)
                try:
                    result = process_vk_message(peer_id, message_id, text)
                except Exception:
                    log.exception("Unhandled command error for VK message_id=%s", message_id)
                    result = "❌ Внутренняя ошибка. Подробности записаны в лог сервера."
                if result:
                    send_message(peer_id, result)

        except (requests.RequestException, ValueError, KeyError) as exc:
            log.error("Long Poll error: %s", exc)
            server_data = None
            time.sleep(3)
        except Exception:
            log.exception("Unexpected Long Poll error")
            time.sleep(3)


def start_listener(user_id):
    global LISTENER_GENERATION
    with LISTENER_LOCK:
        LISTENER_GENERATION += 1
        generation = LISTENER_GENERATION
    thread = threading.Thread(
        target=listen_messages,
        args=(int(user_id), generation),
        daemon=True,
        name=f"vk-listener-{generation}",
    )
    thread.start()


# =============================================================================
# WEB UI
# =============================================================================

HTML_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VK → Telegram Bridge</title>
<style>
body{margin:0;background:#0f1020;color:#fff;font:16px system-ui;min-height:100vh;display:grid;place-items:center}
main{width:min(560px,calc(100% - 32px));background:#191b32;padding:32px;border-radius:20px;box-shadow:0 20px 60px #0008}
h1{margin:0 0 8px;color:#9ca8ff}p{color:#aeb1c6;line-height:1.5}input,button{box-sizing:border-box;width:100%;padding:14px;border-radius:11px;font:inherit}
input{color:#fff;background:#0f1020;border:1px solid #434765;margin:10px 0}button{border:0;color:#fff;background:#667eea;font-weight:700;cursor:pointer}
#status{white-space:pre-wrap;margin-top:16px;padding:12px;border-radius:10px;background:#101224}.warn{color:#ffcf66;font-size:14px}
</style></head>
<body><main><h1>VK → Telegram Bridge</h1><p>Вставь токен ВК или полную OAuth-ссылку.</p>
<p class="warn">TG-токены сюда не вводятся. Их отправляй только в закрытый чат ВК с самим собой.</p>
<form id="f"><input id="token" type="password" autocomplete="off" required placeholder="VK access token"><button>Запустить / перезапустить</button></form>
<div id="status"></div></main>
<script>
f.onsubmit=async e=>{e.preventDefault();status.textContent='Проверяю…';try{let r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token.value})});let d=await r.json();status.textContent=d.ok?'✅ Запущено для '+d.name+' (ID '+d.user_id+')\nНапиши «помощь» в чат с самим собой.':'❌ '+d.error}catch(x){status.textContent='❌ '+x}}
</script></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("HTTP " + fmt, *args)

    def reply_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.reply_json(200, {"status": "ok", "listener_generation": LISTENER_GENERATION})
        else:
            self.reply_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        global VK_TOKEN
        if self.path != "/save":
            self.reply_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                raise ValueError("неверный размер запроса")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            token = extract_vk_token(data.get("token"))
            if not token:
                raise ValueError("пустой токен")

            # Проверяем локально перед заменой глобального токена.
            response = HTTP.post("https://api.vk.com/method/users.get", data={
                "access_token": token,
                "v": VK_API_VERSION,
            }, timeout=15).json()
            if "error" in response:
                raise ValueError(response["error"].get("error_msg", "VK отклонил токен"))
            user = response["response"][0]

            with VK_TOKEN_LOCK:
                VK_TOKEN = token
            atomic_json_save(CONFIG_FILE, {"token": token, "user_id": user["id"]})
            start_listener(user["id"])
            self.reply_json(200, {
                "ok": True,
                "user_id": user["id"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            })
        except Exception as exc:
            log.warning("Web token save failed: %s", exc)
            self.reply_json(400, {"ok": False, "error": str(exc)})


def start_web_server():
    server = ThreadingHTTPServer(("", PORT), WebHandler)
    server.daemon_threads = True
    log.info("Web UI listening on 0.0.0.0:%s", PORT)
    server.serve_forever()


# =============================================================================
# START
# =============================================================================

if __name__ == "__main__":
    log.info("Starting VK → Telegram Bridge 3.0")

    config = json_load(CONFIG_FILE, {})
    if not VK_TOKEN and config.get("token"):
        with VK_TOKEN_LOCK:
            VK_TOKEN = extract_vk_token(config["token"])

    if VK_TOKEN:
        user = get_vk_user()
        if user:
            log.info("VK token accepted: user_id=%s", user["id"])
            start_listener(user["id"])
        else:
            log.error("Saved VK token is invalid; open Web UI and replace it")
    else:
        log.warning("VK token is not configured; open Web UI")

    start_web_server()
