import os
import sys
import re
import json
import time
import random
import logging
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

# ============ НАСТРОЙКИ И ЛОГИ ============
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("vk-tg-bot")

CONFIG_FILE = "/tmp/vk_config.json"
DISCOVERED_CHANNELS_FILE = "/tmp/tg_discovered_channels.json"
VK_API_VERSION = "5.199"

# Глобальное состояние
VK_TOKEN = ""
TG_TOKEN = ""
CHANNELS_CONFIG = {}  # Ручная настройка через веб-панель
CHANNELS_MAP = {}     # Синонимы из веб-панели
DISCOVERED_CHANNELS = {} # Автоматически найденные каналы в Telegram
BOT_THREAD_STARTED = False
TG_OFFSET = 0

# ============ ХРАНЕНИЕ НАЙДЕННЫХ КАНАЛОВ TELEGRAM ============

def load_discovered_channels():
    global DISCOVERED_CHANNELS
    if os.path.exists(DISCOVERED_CHANNELS_FILE):
        try:
            with open(DISCOVERED_CHANNELS_FILE, "r", encoding="utf-8") as f:
                DISCOVERED_CHANNELS = json.load(f)
        except Exception as e:
            log.error(f"❌ Ошибка загрузки найденных каналов: {e}")

def save_discovered_channels():
    try:
        with open(DISCOVERED_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(DISCOVERED_CHANNELS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"❌ Ошибка сохранения найденных каналов: {e}")

def register_discovered_channel(chat, status=None):
    """Добавляет или обновляет информацию о найденном канале/чате в TG"""
    if not chat or not isinstance(chat, dict):
        return
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    
    title = chat.get("title") or chat.get("username") or f"Канал {chat_id}"
    username = chat.get("username", "")
    ctype = chat.get("type", "")

    DISCOVERED_CHANNELS[chat_id] = {
        "id": chat_id,
        "title": title,
        "username": username,
        "type": ctype,
        "status": status or "administrator",
        "updated_at": time.time()
    }
    save_discovered_channels()
    log.info(f"📢 Зафиксирован канал/чат TG: '{title}' ({chat_id})")

def poll_tg_updates():
    """Слушает обновления Telegram Bot API для автоопределения всех каналов бота"""
    global TG_OFFSET
    if not TG_TOKEN:
        return
    try:
        res = tg_api("getUpdates", {
            "offset": TG_OFFSET,
            "timeout": 2,
            "allowed_updates": ["my_chat_member", "chat_member", "channel_post", "message"]
        })
        if res.get("ok"):
            updates = res.get("result", [])
            for upd in updates:
                TG_OFFSET = max(TG_OFFSET, upd["update_id"] + 1)
                if "my_chat_member" in upd:
                    mcm = upd["my_chat_member"]
                    chat = mcm.get("chat", {})
                    new_mem = mcm.get("new_chat_member", {})
                    st = new_mem.get("status", "")
                    if st in ["administrator", "creator", "member"]:
                        register_discovered_channel(chat, st)
                if "channel_post" in upd:
                    chat = upd["channel_post"].get("chat", {})
                    register_discovered_channel(chat)
                if "message" in upd:
                    chat = upd["message"].get("chat", {})
                    register_discovered_channel(chat)
    except Exception as e:
        log.error(f"❌ Ошибка получения обновлений TG: {e}")

# ============ УПРАВЛЕНИЕ КОНФИГУРАЦИЕЙ ============

def extract_vk_token(token_raw):
    """Извлекает access_token если передана полная URL-ссылка"""
    token_raw = token_raw.strip()
    if "access_token=" in token_raw:
        match = re.search(r'access_token=([^&\s]+)', token_raw)
        if match:
            return match.group(1)
    return token_raw

def rebuild_channels_map(ch1_name, ch1_id, ch2_name, ch2_id):
    """Формирует карту синонимов каналов для быстрого распознавания из ВК"""
    global CHANNELS_CONFIG, CHANNELS_MAP
    CHANNELS_CONFIG = {}
    CHANNELS_MAP = {}

    if ch1_id:
        name1 = ch1_name.strip() if ch1_name else "Канал1"
        cid1 = ch1_id.strip()
        CHANNELS_CONFIG["1"] = {"name": name1, "id": cid1}
        for key in [name1.lower(), name1.lower().replace(" ", ""), "канал1", "канал 1", "1", "первый"]:
            CHANNELS_MAP[key] = cid1

    if ch2_id:
        name2 = ch2_name.strip() if ch2_name else "Канал2"
        cid2 = ch2_id.strip()
        CHANNELS_CONFIG["2"] = {"name": name2, "id": cid2}
        for key in [name2.lower(), name2.lower().replace(" ", ""), "канал2", "канал 2", "2", "второй"]:
            CHANNELS_MAP[key] = cid2

def load_config():
    global VK_TOKEN, TG_TOKEN
    load_discovered_channels()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                VK_TOKEN = cfg.get("vk_token", "")
                TG_TOKEN = cfg.get("tg_token", "")
                rebuild_channels_map(
                    cfg.get("ch1_name", "Канал1"),
                    cfg.get("ch1_id", ""),
                    cfg.get("ch2_name", "Канал2"),
                    cfg.get("ch2_id", "")
                )
                log.info("📂 Конфигурация успешно загружена из файла")
                return cfg
        except Exception as e:
            log.error(f"❌ Ошибка загрузки конфига: {e}")
    return {}

def save_config_data(vk_token, tg_token, ch1_name, ch1_id, ch2_name, ch2_id, user_id):
    global VK_TOKEN, TG_TOKEN
    VK_TOKEN = extract_vk_token(vk_token)
    TG_TOKEN = tg_token.strip()
    rebuild_channels_map(ch1_name, ch1_id, ch2_name, ch2_id)

    cfg_data = {
        "vk_token": VK_TOKEN,
        "tg_token": TG_TOKEN,
        "ch1_name": ch1_name,
        "ch1_id": ch1_id,
        "ch2_name": ch2_name,
        "ch2_id": ch2_id,
        "user_id": user_id
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, ensure_ascii=False, indent=2)
        log.info("💾 Конфигурация сохранена")
    except Exception as e:
        log.error(f"❌ Ошибка сохранения конфига: {e}")

# ============ ДИНАМИЧЕСКИЙ ПОИСК И ОПРЕДЕЛЕНИЕ КАНАЛОВ ============

def get_channel_map():
    """Мгновенно собирает список доступных каналов без лишних внешних запросов"""
    load_discovered_channels()
    channels_list = []
    seen_ids = set()

    # 1. Сначала из веб-панели
    for key, c_info in CHANNELS_CONFIG.items():
        cid = str(c_info["id"]).strip()
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            channels_list.append({
                "id": cid,
                "title": c_info.get("name", f"Канал {cid}"),
                "username": ""
            })

    # 2. Из автообнаружения в Telegram
    for cid, info in DISCOVERED_CHANNELS.items():
        cid_str = str(cid).strip()
        if cid_str and cid_str not in seen_ids:
            seen_ids.add(cid_str)
            channels_list.append({
                "id": cid_str,
                "title": info.get("title", f"Канал {cid_str}"),
                "username": info.get("username", "")
            })

    # Присваиваем порядковый индекс 1, 2, 3...
    for idx, ch in enumerate(channels_list, 1):
        ch["index"] = idx

    return channels_list

def get_all_active_channels():
    """Собирает и проверяет все каналы из автопоиска Telegram и из настроек"""
    poll_tg_updates()
    channels_list = get_channel_map()

    bot_id = None
    if TG_TOKEN:
        me_resp = tg_api("getMe")
        if me_resp.get("ok"):
            bot_id = me_resp.get("result", {}).get("id")

    final_channels = []
    for ch in channels_list:
        cid = ch["id"]
        title = ch["title"]
        status = "unknown"
        if TG_TOKEN:
            chat_resp = tg_api("getChat", {"chat_id": cid})
            if chat_resp.get("ok"):
                res = chat_resp.get("result", {})
                title = res.get("title") or title
                ch["username"] = res.get("username") or ch["username"]
                if bot_id:
                    mem_resp = tg_api("getChatMember", {"chat_id": cid, "user_id": bot_id})
                    if mem_resp.get("ok"):
                        status = mem_resp.get("result", {}).get("status", "unknown")
            else:
                status = "error"

        ch["title"] = title
        ch["status"] = status
        final_channels.append(ch)

    return final_channels

def find_channel_by_input(input_str):
    """Быстрый и точный поиск канала по названию, индексу или ID"""
    if not input_str:
        return None, None

    clean_str = input_str.strip()
    lower_str = clean_str.lower()
    no_spaces = lower_str.replace(" ", "")

    if clean_str.startswith("-100") or (clean_str.startswith("-") and clean_str[1:].isdigit()):
        return clean_str, f"Канал {clean_str}"

    if clean_str.startswith("@"):
        return clean_str, clean_str

    channels = get_channel_map()

    if lower_str.isdigit():
        idx = int(lower_str)
        if 1 <= idx <= len(channels):
            ch = channels[idx - 1]
            return ch["id"], ch["title"]

    m = re.match(r'^(?:канал|channel|чат|chat)[\s_]*(\d+)$', lower_str)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(channels):
            ch = channels[idx - 1]
            return ch["id"], ch["title"]

    if lower_str in CHANNELS_MAP:
        cid = CHANNELS_MAP[lower_str]
        for ch in channels:
            if ch["id"] == cid:
                return cid, ch["title"]
        return cid, clean_str

    for ch in channels:
        ch_title_lower = ch["title"].lower()
        ch_user_lower = ch["username"].lower() if ch.get("username") else ""
        if lower_str == ch_title_lower or lower_str == ch_user_lower or lower_str == f"@{ch_user_lower}":
            return ch["id"], ch["title"]

    for ch in channels:
        ch_title_nospaces = ch["title"].lower().replace(" ", "")
        if no_spaces == ch_title_nospaces:
            return ch["id"], ch["title"]

    for ch in channels:
        if lower_str in ch["title"].lower():
            return ch["id"], ch["title"]

    return None, None

# ============ VK API ============

def vk_api(method, params=None):
    if params is None:
        params = {}
    url = f"https://api.vk.com/method/{method}"
    params.update({
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION
    })
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
        if "error" in data:
            err = data["error"]
            log.error(f"❌ VK API Ошибка {err.get('error_code')}: {err.get('error_msg')}")
            return None
        return data
    except Exception as e:
        log.error(f"❌ Запрос к VK API провален ({method}): {e}")
        return None

def send_message(peer_id, text, attachment=""):
    params = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randint(-2147483648, 2147483647)
    }
    if attachment:
        params["attachment"] = attachment
    return vk_api("messages.send", params)

def send_typing(peer_id):
    vk_api("messages.setActivity", {"peer_id": peer_id, "type": "typing"})

def get_vk_message_attachments(msg_id):
    """Извлекает прямые ссылки на фото и видео из ВК сообщения"""
    if not msg_id:
        return [], []
    resp = vk_api("messages.getById", {"message_ids": msg_id})
    photos = []
    videos = []
    if resp and "response" in resp and resp["response"]["items"]:
        msg_item = resp["response"]["items"][0]
        for att in msg_item.get("attachments", []):
            att_type = att.get("type")
            if att_type == "photo" and "photo" in att:
                sizes = att["photo"].get("sizes", [])
                if sizes:
                    best_size = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    photos.append(best_size.get("url"))
            elif att_type == "video" and "video" in att:
                img_sizes = att["video"].get("image", [])
                if img_sizes:
                    best_img = max(img_sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    photos.append(best_img.get("url"))
    return photos, videos

def get_user_id():
    resp = vk_api("users.get", {})
    if resp and "response" in resp and len(resp["response"]) > 0:
        user_id = resp["response"][0]["id"]
        first_name = resp["response"][0].get("first_name", "")
        last_name = resp["response"][0].get("last_name", "")
        log.info(f"✅ Пользователь ВК авторизован: {first_name} {last_name} (ID: {user_id})")
        return user_id
    log.error("❌ Не удалось получить профиль ВК. Проверь токен!")
    return None

# ============ TELEGRAM API ============

def tg_api(method, payload=None, token_override=None):
    token = token_override or TG_TOKEN
    if not token:
        return {"ok": False, "description": "Токен Telegram бота не задан!"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=25)
        return r.json()
    except Exception as e:
        log.error(f"❌ Telegram HTTP ошибка ({method}): {e}")
        return {"ok": False, "description": str(e)}

def check_tg_bot_admin_status():
    """Автоматическая проверка и вывод списка каналов Telegram, где бот админ"""
    if not TG_TOKEN:
        return "❌ Токен Telegram бота не настроен на сайте!"

    me_resp = tg_api("getMe")
    if not me_resp.get("ok"):
        return f"❌ Ошибка токена Telegram бота:\n{me_resp.get('description')}"

    bot_info = me_resp.get("result", {})
    bot_name = bot_info.get("first_name", "Bot")
    bot_user = bot_info.get("username", "bot")

    channels = get_all_active_channels()

    if not channels:
        return (
            f"🤖 *Бот Telegram:* {bot_name} (@{bot_user})\n\n"
            "⚠️ *Каналы пока не найдены!*\n\n"
            "💡 *Как подключить канал:*\n"
            "1. Добавьте бота в ваш Telegram-канал как АДМИНИСТРАТОРА.\n"
            "2. Опубликуйте любое сообщение в канале (или отправьте пост).\n"
            "3. Повторно напишите команду `каналы` в чат ВК!\n\n"
            "Также можно указать ID канала на веб-панели управления."
        )

    reports = []
    for ch in channels:
        idx = ch["index"]
        title = ch["title"]
        cid = ch["id"]
        st = ch["status"]
        uname = f" (@{ch['username']})" if ch.get("username") else ""

        if st in ["administrator", "creator"]:
            status_str = "👑 АДМИНИСТРАТОР (Готов к публикациям)"
        elif st == "member":
            status_str = "⚠️ УЧАСТНИК — Сделайте бота администратором канала!"
        else:
            status_str = f"⚙️ Статус: {st}"

        reports.append(
            f"{idx}️⃣ *{title}*{uname}\n"
            f"🆔 ID: `{cid}`\n"
            f"📌 Варианты отправки:\n"
            f"• `{idx}`\n`Мяу мяу`\n"
            f"• `{idx} - мяу мяу`\n"
            f"• `{idx}мяумяу`\n"
            f"• `{cid} - мяу мяу`\n"
            f"Статус: {status_str}"
        )

    report = (
        f"🤖 *Бот Telegram:* {bot_name} (@{bot_user})\n"
        f"📋 *Найдено подключенных каналов/чатов: {len(channels)}*\n\n" +
        "\n\n".join(reports) +
        "\n\n💬 *Способы отправки из ВК:*\n"
        "1) С переносом строки:\n`1`\n`Мяу мяу`\n\n"
        "2) Через тире в одну строчку:\n`1 - Мяу мяу`\n\n"
        "3) Слитно:\n`1мяумяу`\n\n"
        "4) С ID канала:\n`-100123456789 - Мяу мяу`"
    )
    return report

# ============ ЗАЩИТА ОТ СПАМА И ДУБЛЕЙ ============
LAST_TG_POST_TIME = {}
TG_POST_HASHES = {}
MIN_POST_INTERVAL = 2      # Секунд между постами
DUPLICATE_COOLDOWN = 180   # Кулдаун одинаковых постов (3 минуты)

def validate_anti_spam(chat_id, text, photos=None):
    now = time.time()
    content_key = f"{chat_id}:{text.strip()}:{len(photos or [])}"
    post_hash = hash(content_key)

    last_hash_time = TG_POST_HASHES.get(post_hash, 0)
    if now - last_hash_time < DUPLICATE_COOLDOWN:
        remaining = int(DUPLICATE_COOLDOWN - (now - last_hash_time))
        log.warning(f"🛡 [СПАМ-ФИЛЬТР] Повторный пост заблокирован для {chat_id}")
        return False, f"🛡 *ЗАЩИТА ОТ СПАМА:*\nЭтот пост уже недавно отправлялся в канал `{chat_id}`!\nПовторить можно через {remaining} сек."

    last_post = LAST_TG_POST_TIME.get(chat_id, 0)
    if now - last_post < MIN_POST_INTERVAL:
        wait = int(MIN_POST_INTERVAL - (now - last_post)) + 1
        return False, f"🛡 *ЗАЩИТА ОТ СПАМА:*\nСлишком частая отправка! Подождите {wait} сек."

    if len(TG_POST_HASHES) > 300:
        TG_POST_HASHES.clear()

    return True, post_hash

# ============ ПУБЛИКАЦИЯ В ТЕЛЕГРАМ ============

def send_tg_channel_post(chat_id, text, photos=None):
    if photos is None:
        photos = []

    log.info(f"🚀 Публикация в TG {chat_id} | Фото: {len(photos)}")

    is_safe, p_hash = validate_anti_spam(chat_id, text, photos)
    if not is_safe:
        return False, p_hash

    chat_info = tg_api("getChat", {"chat_id": chat_id})
    if not chat_info.get("ok"):
        err = chat_info.get("description", "Канал не найден")
        log.error(f"❌ Ошибка доступа к каналу {chat_id}: {err}")
        return False, f"❌ Ошибка канала `{chat_id}`:\n{err}\n\nПроверь, добавлен ли бот в канал как администратор!"

    channel_title = chat_info.get("result", {}).get("title", chat_id)

    # Выполняем отправку
    if len(photos) > 1:
        # Несколько фото -> MediaGroup
        media = []
        for idx, p_url in enumerate(photos[:10]):
            m_item = {"type": "photo", "media": p_url}
            if idx == 0 and text:
                m_item["caption"] = text[:1024]
                m_item["parse_mode"] = "HTML"
            media.append(m_item)
        res = tg_api("sendMediaGroup", {"chat_id": chat_id, "media": media})
        if not res.get("ok"):
            # Повтор без HTML
            for m in media:
                m.pop("parse_mode", None)
            res = tg_api("sendMediaGroup", {"chat_id": chat_id, "media": media})

    elif len(photos) == 1:
        # Одно фото
        payload = {
            "chat_id": chat_id,
            "photo": photos[0],
            "caption": text[:1024],
            "parse_mode": "HTML"
        }
        res = tg_api("sendPhoto", payload)
        if not res.get("ok"):
            payload.pop("parse_mode", None)
            res = tg_api("sendPhoto", payload)
    else:
        # Только текст
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        res = tg_api("sendMessage", payload)
        if not res.get("ok"):
            payload.pop("parse_mode", None)
            res = tg_api("sendMessage", payload)

    if res.get("ok"):
        now = time.time()
        LAST_TG_POST_TIME[chat_id] = now
        TG_POST_HASHES[p_hash] = now
        log.info(f"✅ Пост опубликован в '{channel_title}'!")
        return True, f"✅ *ПОСТ УСПЕШНО ОПУБЛИКОВАН!*\n\n📢 Канал: *{channel_title}*\n🆔 ID: `{chat_id}`\n🖼 Вложений: {len(photos)}"
    else:
        err = res.get("description", "Неизвестная ошибка")
        log.error(f"❌ Ошибка публикации: {err}")
        return False, f"❌ Ошибка отправки в Telegram:\n{err}"

# ============ УНИВЕРСАЛЬНЫЙ ПАРСИНГ ВВОДА ВК ============

def parse_vk_input(text):
    text_str = text.strip()
    if not text_str:
        return None

    channels = get_channel_map()

    # --- 1. Прямой ID (-100...) или Username (@channel) ---
    m_id = re.match(r'^((-100\d+)|(-\d+)|(@[a-zA-Z0-9_]+))[\s\-—–:]*(.*)$', text_str, re.DOTALL)
    if m_id:
        target_chat_id = m_id.group(1).strip()
        post_message = m_id.group(5).strip()
        target_title = None
        for ch in channels:
            if ch["id"] == target_chat_id or (ch["username"] and f"@{ch['username'].lower()}" == target_chat_id.lower()):
                target_title = ch["title"]
                break
        if not target_title:
            target_title = f"Канал {target_chat_id}"
        return {
            "action": "post",
            "chat_id": target_chat_id,
            "channel_title": target_title,
            "message": post_message
        }

    # --- 2. Поиск совпадения в начале сообщения (1, канал1, название) ---
    best_match = None

    for ch in channels:
        idx = str(ch["index"])
        title = ch["title"]
        title_lower = title.lower()
        username = ch.get("username", "").lower()

        prefixes = [
            f"канал {idx}", f"канал{idx}",
            f"channel {idx}", f"channel{idx}",
            f"чат {idx}", f"чат{idx}",
            idx
        ]
        if title_lower:
            prefixes.append(title_lower)
        if username:
            prefixes.append(f"@{username}")
            prefixes.append(username)

        for key, mapped_id in CHANNELS_MAP.items():
            if mapped_id == ch["id"]:
                prefixes.append(key)

        for pref in prefixes:
            pref_lower = pref.lower().strip()
            if not pref_lower:
                continue

            if text_str.lower().startswith(pref_lower):
                rem = text_str[len(pref_lower):]
                
                # Если префикс состоит только из цифр (например "1"),
                # проверяем, чтобы следующий символ не был еще одной цифрой (например "12")
                if pref_lower.isdigit() and rem and rem[0].isdigit():
                    continue

                rem_cleaned = re.sub(r'^[\s\-—–:]+', '', rem).strip()

                match_len = len(pref_lower)
                if best_match is None or match_len > best_match[0]:
                    best_match = (match_len, ch["id"], title, rem_cleaned)

    if best_match:
        _, chat_id, title, msg = best_match
        return {
            "action": "post",
            "chat_id": chat_id,
            "channel_title": title,
            "message": msg
        }

    # --- 3. Построчный разбор (первая строчка - канал) ---
    lines = text_str.split("\n")
    if len(lines) >= 1:
        first_line = lines[0].strip()
        cid, ctitle = find_channel_by_input(first_line)
        if cid:
            msg = "\n".join(lines[1:]).strip()
            if not msg and ("-" in first_line or "—" in first_line or "–" in first_line):
                parts = re.split(r'[\-—–]', first_line, maxsplit=1)
                if len(parts) == 2:
                    msg = parts[1].strip()
            return {
                "action": "post",
                "chat_id": cid,
                "channel_title": ctitle or f"Канал {cid}",
                "message": msg
            }

    return None

# ============ ОБРАБОТКА КОМАНД ============

def process_command(peer_id, text, msg_id=None):
    text_clean = text.strip()
    text_lower = text_clean.lower()

    # 1. Служебные команды помощи
    if text_lower in ["помощь", "help", "команды", "меню", "start", "старт"]:
        channels = get_channel_map()
        ch_list = []
        for ch in channels:
            ch_list.append(f"• *{ch['title']}* (пишите: `{ch['index']}`, `канал{ch['index']}`, или `{ch['title']}`)")

        channels_str = "\n".join(ch_list) if ch_list else "⚠️ Каналы еще не найдены. Напишите `каналы` для автопоиска."

        return (
            "📋 *ИНСТРУКЦИЯ ПО ПУБЛИКАЦИИ ПОСТОВ*\n\n"
            "Вы можете отправлять посты любым удобным способом:\n\n"
            "1️⃣ С переносом строки:\n"
            "1\n"
            "Мяу мяу\n\n"
            "2️⃣ Через тире в одну строчку:\n"
            "1 - Мяу мяу\n\n"
            "3️⃣ Слитно:\n"
            "1мяумяу\n\n"
            "4️⃣ По ID канала:\n"
            "-100123456789 - Мяу мяу\n\n"
            f"📋 *Доступные каналы:*\n{channels_str}\n\n"
            "⚙️ *Служебные команды:*\n"
            "• `каналы` / `админ` — проверка списка каналов\n"
            "• `стоп` / `старт` — пауза работы бота"
        )

    if text_lower in ["админ", "admin", "каналы", "channels", "статус"]:
        return check_tg_bot_admin_status()

    # 2. Проверка поста в Telegram
    parsed = parse_vk_input(text_clean)
    if parsed:
        photos, videos = get_vk_message_attachments(msg_id)

        if parsed["action"] == "post":
            ok, res_text = send_tg_channel_post(parsed["chat_id"], parsed["message"], photos=photos)
            return res_text

        elif parsed["action"] == "post_custom":
            ok, res_text = send_tg_channel_post(parsed["chat_id"], parsed["message"], photos=photos)
            return res_text

    # 3. Если канал не распознан — показываем четкую справку
    first_line = text_clean.split("\n")[0].strip()
    channels = get_channel_map()
    if channels:
        ch_list = [f"• `{ch['index']}` или `{ch['title']}`" for ch in channels]
        ch_str = "\n".join(ch_list)
    else:
        ch_str = "⚠️ Ни один канал пока не найден! Напишите `каналы` для запуска автопоиска."

    return (
        f"❌ *Канал '{first_line}' не найден!*\n\n"
        "💡 *Примеры правильного ввода:*\n"
        "• `1 - Мяу мяу`\n"
        "• `1мяумяу`\n"
        "• `1` (и со 2-й строчки текст)\n"
        "• `-100123456789 - Мяу мяу`\n\n"
        f"📋 *Доступные номера и названия каналов:*\n{ch_str}\n\n"
        "💡 Напишите `каналы`, чтобы посмотреть все доступные каналы!"
    )

# ============ LONG POLL ЦИКЛ СЛУШАНИЯ ВК ============

BOT_PAUSED = False

def listen_messages(user_id):
    global BOT_PAUSED

    resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
    if not resp or "response" not in resp:
        log.error("❌ Не удалось получить Long Poll сервер ВК. Повтор через 10 сек...")
        time.sleep(10)
        return listen_messages(user_id)

    server_data = resp["response"]
    ts = server_data["ts"]
    server = server_data["server"]
    key = server_data["key"]

    log.info(f"🚀 LongPoll запущен! Бот отслеживает сообщения от ID {user_id}")
    start_timestamp = int(time.time())

    while True:
        try:
            url = f"https://{server}?act=a_check&key={key}&ts={ts}&wait=25&mode=2&version=3"
            r = requests.get(url, timeout=35)
            data = r.json()

            if "failed" in data:
                resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
                if resp and "response" in resp:
                    server_data = resp["response"]
                    ts = server_data["ts"]
                    server = server_data["server"]
                    key = server_data["key"]
                time.sleep(2)
                continue

            ts = data["ts"]

            for update in data.get("updates", []):
                if update[0] == 4:  # Новое сообщение
                    msg_id = update[1]
                    flags = update[2]
                    peer_id = update[3]
                    ts_msg = update[4]
                    text = update[5]

                    # Пропускаем исходящие
                    if flags & 2:
                        continue

                    # Только сообщения от владельца
                    if peer_id != user_id:
                        continue

                    # Пропускаем старые сообщения до запуска
                    if ts_msg < start_timestamp - 10:
                        continue

                    # Игнорируем ответы самого бота
                    if any(text.startswith(prefix) for prefix in ["✅", "❌", "🛡", "🤖", "📋", "💡"]):
                        continue

                    text_lower = text.lower().strip()
                    if text_lower in ["стоп", "stop"]:
                        BOT_PAUSED = True
                        send_message(peer_id, "⏸ Работа бота приостановлена.")
                        continue
                    elif text_lower in ["старт", "start"] and BOT_PAUSED:
                        BOT_PAUSED = False
                        send_message(peer_id, "▶️ Бот возобновил работу!")
                        continue

                    if BOT_PAUSED:
                        continue

                    log.info(f"📩 ВК сообщение от пользователя: {text[:50]}...")
                    send_typing(peer_id)

                    reply_text = process_command(peer_id, text, msg_id=msg_id)
                    if reply_text:
                        send_message(peer_id, reply_text)

        except Exception as e:
            log.error(f"❌ Ошибка цикла LongPoll: {e}")
            time.sleep(5)

def start_tg_background_poller():
    """Фоновый поток постоянного автоопределения Telegram обновлений"""
    def poll_loop():
        while True:
            try:
                poll_tg_updates()
            except Exception as e:
                log.error(f"❌ Ошибка фона TG polling: {e}")
            time.sleep(3)
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

# ============ ВЕБ-СЕРВЕР И ИНТЕРФЕЙС НАСТРОЙКИ ============

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Панель управления VK -> TG Bot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
        }
        .card {
            background: #161b22; border: 1px solid #30363d; border-radius: 16px; padding: 32px; max-width: 540px; width: 100%;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        h1 { font-size: 24px; color: #58a6ff; margin-bottom: 8px; text-align: center; }
        p.desc { color: #8b949e; font-size: 13px; text-align: center; margin-bottom: 24px; }
        .section-title { font-size: 14px; font-weight: 600; color: #f0f6fc; margin: 18px 0 8px 0; text-transform: uppercase; letter-spacing: 0.5px; }
        label { display: block; font-size: 12px; color: #8b949e; margin-bottom: 4px; }
        input[type="text"] {
            width: 100%; padding: 12px 14px; background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
            color: #f0f6fc; font-size: 14px; font-family: monospace; margin-bottom: 12px; transition: border-color 0.2s;
        }
        input[type="text"]:focus { outline: none; border-color: #58a6ff; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        button {
            width: 100%; padding: 14px; background: #238636; border: none; border-radius: 8px; color: #fff;
            font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 16px; transition: background 0.2s;
        }
        button:hover { background: #2ea043; }
        .status { margin-top: 16px; padding: 12px; border-radius: 8px; font-size: 13px; display: none; }
        .status.ok { background: rgba(46,160,67,0.15); border: 1px solid #2ea043; color: #3fb950; display: block; }
        .status.err { background: rgba(248,81,73,0.15); border: 1px solid #f85149; color: #f85149; display: block; }
    </style>
</head>
<body>
    <div class="card">
        <h1>🚀 VK -> TG Автопостинг</h1>
        <p class="desc">Настрой каналы один раз и отправляй посты простыми сообщениями из ВК!</p>
        
        <form id="cfgForm">
            <div class="section-title">1. VK Авторизация</div>
            <label>Kate Mobile Токен / Ссылка</label>
            <input type="text" id="vk_token" placeholder="vk1.a.xxx... или ссылка Kate Mobile" required>

            <div class="section-title">2. Telegram Бот</div>
            <label>Токен Telegram Бота</label>
            <input type="text" id="tg_token" placeholder="8476739947:AAHP..." required>

            <div class="section-title">3. Привязка Каналов (Опционально)</div>
            <div class="grid-2">
                <div>
                    <label>Канал 1 (Название в ВК)</label>
                    <input type="text" id="ch1_name" value="Канал1">
                </div>
                <div>
                    <label>Канал 1 ID Telegram</label>
                    <input type="text" id="ch1_id" placeholder="-100xxxxxxxxx">
                </div>
            </div>

            <div class="grid-2">
                <div>
                    <label>Канал 2 (Название в ВК)</label>
                    <input type="text" id="ch2_name" value="Канал2">
                </div>
                <div>
                    <label>Канал 2 ID Telegram</label>
                    <input type="text" id="ch2_id" placeholder="-100yyyyyyyyy">
                </div>
            </div>

            <button type="submit">💾 Сохранить и Запустить</button>
        </form>

        <div id="status" class="status"></div>
    </div>

    <script>
        fetch('/get_config').then(r => r.json()).then(data => {
            if (data.ok && data.cfg) {
                if (data.cfg.vk_token) document.getElementById('vk_token').value = data.cfg.vk_token;
                if (data.cfg.tg_token) document.getElementById('tg_token').value = data.cfg.tg_token;
                if (data.cfg.ch1_name) document.getElementById('ch1_name').value = data.cfg.ch1_name;
                if (data.cfg.ch1_id) document.getElementById('ch1_id').value = data.cfg.ch1_id;
                if (data.cfg.ch2_name) document.getElementById('ch2_name').value = data.cfg.ch2_name;
                if (data.cfg.ch2_id) document.getElementById('ch2_id').value = data.cfg.ch2_id;
            }
        }).catch(e => console.log(e));

        document.getElementById('cfgForm').onsubmit = async function(e) {
            e.preventDefault();
            const status = document.getElementById('status');
            status.className = 'status';
            status.style.display = 'block';
            status.textContent = '⏳ Сохранение и проверка токенов...';

            const payload = {
                vk_token: document.getElementById('vk_token').value.trim(),
                tg_token: document.getElementById('tg_token').value.trim(),
                ch1_name: document.getElementById('ch1_name').value.trim(),
                ch1_id: document.getElementById('ch1_id').value.trim(),
                ch2_name: document.getElementById('ch2_name').value.trim(),
                ch2_id: document.getElementById('ch2_id').value.trim()
            };

            try {
                const resp = await fetch('/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const res = await resp.json();
                if (res.ok) {
                    status.className = 'status ok';
                    status.innerHTML = '✅ <b>Настройки успешно сохранены!</b><br>👤 ВК: ' + res.name + ' (ID: ' + res.user_id + ')<br>💬 Напишите "каналы" в личку ВК!';
                } else {
                    status.className = 'status err';
                    status.textContent = '❌ ' + res.error;
                }
            } catch(err) {
                status.className = 'status err';
                status.textContent = '❌ Ошибка сети: ' + err.message;
            }
        };
    </script>
</body>
</html>
"""

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/get_config":
            cfg = load_config()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "cfg": cfg}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global BOT_THREAD_STARTED
        if self.path == "/save":
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode('utf-8')
            data = json.loads(body)

            vk_tok = extract_vk_token(data.get("vk_token", ""))
            tg_tok = data.get("tg_token", "").strip()
            ch1_name = data.get("ch1_name", "Канал1")
            ch1_id = data.get("ch1_id", "").strip()
            ch2_name = data.get("ch2_name", "Канал2")
            ch2_id = data.get("ch2_id", "").strip()

            test_url = f"https://api.vk.com/method/users.get?access_token={vk_tok}&v=5.199"
            try:
                r = requests.get(test_url, timeout=10)
                vk_data = r.json()

                if "error" in vk_data:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": f"Ошибка VK Токена: {vk_data['error']['error_msg']}"}).encode("utf-8"))
                    return

                user = vk_data["response"][0]
                user_id = user["id"]
                name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()

                save_config_data(vk_tok, tg_tok, ch1_name, ch1_id, ch2_name, ch2_id, user_id)

                if not BOT_THREAD_STARTED:
                    BOT_THREAD_STARTED = True
                    start_tg_background_poller()
                    def start_bot():
                        listen_messages(user_id)
                    bot_thread = threading.Thread(target=start_bot, daemon=True)
                    bot_thread.start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user_id": user_id, "name": name}).encode("utf-8"))

            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.TCPServer(("", port), WebHandler) as httpd:
        log.info(f"🌐 Веб-интерфейс доступен на порту: {port}")
        httpd.serve_forever()

# ============ ОСНОВНОЙ ВХОД ============

if __name__ == "__main__":
    log.info("🚀 Запуск VK -> Telegram автопостинг бота...")
    cfg = load_config()

    if VK_TOKEN:
        user_id = get_user_id()
        if user_id:
            BOT_THREAD_STARTED = True
            start_tg_background_poller()
            def start_bot():
                listen_messages(user_id)
            bot_thread = threading.Thread(target=start_bot, daemon=True)
            bot_thread.start()

    start_web_server()
