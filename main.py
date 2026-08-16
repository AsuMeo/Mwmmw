import os
import sys
import re
import json
import time
import random
import logging
import threading
import requests
from urllib.parse import quote, unquote

# ============ НАСТРОЙКИ ============
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_API_VERSION = "5.199"

# Парсим токен из полной ссылки
if VK_TOKEN and "access_token=" in VK_TOKEN:
    match = re.search(r'access_token=([^&\s]+)', VK_TOKEN)
    if match:
        VK_TOKEN = match.group(1)
        print(f"[+] VK Токен извлечён из ссылки")

# ============ СУПЕР ЛОГИ ============
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("vk-tg-bot")

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
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        if "error" in data:
            err = data["error"]
            log.error(f"❌ VK API error {err.get('error_code')}: {err.get('error_msg')}")
            return None
        return data
    except Exception as e:
        log.error(f"❌ Ошибка запроса VK API ({method}): {e}")
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
    """Извлекает прямые URL фото и медиафайлов из ВК сообщения по msg_id"""
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
                    # Выбираем фото наибольшего разрешения
                    best_size = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    photos.append(best_size.get("url"))
            elif att_type == "video" and "video" in att:
                # Берем превью видео
                img_sizes = att["video"].get("image", [])
                if img_sizes:
                    best_img = max(img_sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    photos.append(best_img.get("url"))
    return photos, videos

# ============ ПОЛУЧЕНИЕ USER ID ============

def get_user_id():
    resp = vk_api("users.get", {})
    if resp and "response" in resp and len(resp["response"]) > 0:
        user_id = resp["response"][0]["id"]
        first_name = resp["response"][0].get("first_name", "")
        last_name = resp["response"][0].get("last_name", "")
        log.info(f"✅ VK Токен успешно проверен: {first_name} {last_name} (ID: {user_id})")
        return user_id
    log.error("❌ Не удалось определить VK ID. Проверь VK_TOKEN!")
    return None

# ============ ТЕЛЕГРАМ АПИ И АВТО-ОБНАРУЖЕНИЕ КАНАЛОВ ============

def tg_api(token, method, payload=None):
    """Вызов Telegram Bot API с обработкой ошибок"""
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=25)
        return r.json()
    except Exception as e:
        log.error(f"❌ Telegram HTTP error ({method}): {e}")
        return {"ok": False, "description": str(e)}

def check_tg_bot_admin_status(token, target_chat_id=None):
    """
    Проверяет, в каких каналах/чатах бот является администратором.
    Опрашивает getMe и getUpdates Telegram API.
    """
    log.info("🔍 Проверка прав и доступных каналов Telegram бота...")
    me_resp = tg_api(token, "getMe")
    if not me_resp.get("ok"):
        err_msg = me_resp.get("description", "Неверный токен")
        log.error(f"❌ Telegram token check failed: {err_msg}")
        return False, f"❌ Ошибка токена Telegram:\n{err_msg}"

    bot_info = me_resp.get("result", {})
    bot_id = bot_info.get("id")
    bot_name = bot_info.get("first_name", "Bot")
    bot_user = bot_info.get("username", "bot")

    chats_to_check = set()
    if target_chat_id:
        chats_to_check.add(str(target_chat_id).strip())

    # Поиск каналов через getUpdates
    upd_resp = tg_api(token, "getUpdates", {"limit": 100, "allowed_updates": ["message", "channel_post", "my_chat_member"]})
    if upd_resp.get("ok"):
        for update in upd_resp.get("result", []):
            for key in ["message", "channel_post", "edited_channel_post", "my_chat_member"]:
                if key in update:
                    chat = update[key].get("chat")
                    if chat and "id" in chat:
                        chats_to_check.add(str(chat["id"]))

    found_reports = []

    for cid in chats_to_check:
        chat_resp = tg_api(token, "getChat", {"chat_id": cid})
        if chat_resp.get("ok"):
            c_data = chat_resp.get("result", {})
            title = c_data.get("title", c_data.get("username", cid))
            ctype = c_data.get("type", "канал/чат")
            
            # Проверяем статус бота
            mem_resp = tg_api(token, "getChatMember", {"chat_id": cid, "user_id": bot_id})
            if mem_resp.get("ok"):
                m_data = mem_resp.get("result", {})
                status = m_data.get("status", "unknown")
                can_post = m_data.get("can_post_messages", True)
                
                if status in ["administrator", "creator"]:
                    post_str = "✅ Да (Есть права публикации)" if can_post else "⚠️ Админ (без права постов)"
                    found_reports.append(f"📢 *{title}*\n🆔 ID: `{cid}`\n⚙️ Тип: {ctype}\n👑 Статус: АДМИНИСТРАТОР\n✍️ Публикация: {post_str}")
                else:
                    found_reports.append(f"📌 *{title}*\n🆔 ID: `{cid}`\n⚙️ Статус: {status} (Не админ!)")

    report = f"🤖 *Информация о боте Telegram:*\n Имя: *{bot_name}*\n Юзернейм: @{bot_user}\n ID бота: `{bot_id}`\n\n"
    if found_reports:
        report += "📋 *Каналы и чаты бота:*\n\n" + "\n\n".join(found_reports)
    else:
        report += ("⚠️ *Бот не нашел каналы автоматически через историю сообщений.*\n\n"
                   "💡 *Как проверить конкретный канал:*\n"
                   "Отправь команду:\n`админ\n<БОТ_ТОКЕН>\n<ИД_КАНАЛА>`\n"
                   "или просто сделай пост в канал!")

    return True, report

# ============ ЗАЩИТА ОТ СПАМА (КРИТИЧЕСКИ ВАЖНО) ============
LAST_TG_POST_TIME = {}  # chat_id -> timestamp (Защита от частоты постов)
TG_POST_HASHES = {}     # hash -> timestamp (Защита от дублей постов)
MIN_POST_INTERVAL = 3   # Минимальный интервал между постами в 1 канал (сек)
DUPLICATE_COOLDOWN = 300 # Кулдаун одинаковых сообщений (5 минут)

def validate_anti_spam(chat_id, text, photos=None):
    """
    Мощная система защиты канала от спама и блокировок Telegram:
    1. Защита от частых запросов (Rate Limit)
    2. Защита от флуда дубликатами
    3. Защита от пустых или гигантских сообщений
    """
    now = time.time()

    # 1. Защита от дублей по тексту и фото
    content_key = f"{chat_id}:{text.strip()}:{len(photos or [])}"
    post_hash = hash(content_key)
    last_hash_time = TG_POST_HASHES.get(post_hash, 0)
    if now - last_hash_time < DUPLICATE_COOLDOWN:
        remaining = int(DUPLICATE_COOLDOWN - (now - last_hash_time))
        log.warning(f"🛡 [СПАМ-ФИЛЬТР] Заблокирован дубликат поста в {chat_id}. Осталось {remaining}с кулдауна.")
        return False, f"🛡 *ЗАЩИТА ОТ СПАМА ЗАБЛОКИРОВАЛА ДУБЛИКАТ!*\n\nЭтот пост уже отправлялся в канал `{chat_id}` недавно.\nПовторить можно через {remaining} сек.", None

    # 2. Защита от частоты (Rate Limiting)
    last_post = LAST_TG_POST_TIME.get(chat_id, 0)
    if now - last_post < MIN_POST_INTERVAL:
        wait = int(MIN_POST_INTERVAL - (now - last_post)) + 1
        log.warning(f"🛡 [СПАМ-ФИЛЬТР] Слишком частая отправка в {chat_id}. Задержка {wait}s.")
        return False, f"🛡 *ЗАЩИТА ОТ СПАМА:* Слишком частая публикация в канал `{chat_id}`!\nПодождите {wait} сек. перед следующим постом.", None

    # Очистка старой памяти каждые 500 записей
    if len(TG_POST_HASHES) > 500:
        TG_POST_HASHES.clear()

    return True, "OK", post_hash

# ============ ОТПРАВКА ПОСТА В ТЕЛЕГРАМ ============

def send_tg_channel_post(chat_id, token, text, photos=None):
    """Отправка поста в канал Telegram с поддержкой текста и медиа"""
    if photos is None:
        photos = []

    log.info(f"🚀 [TG POST] Начинаем публикацию в {chat_id} | Фото: {len(photos)} шт.")

    # Проверка антиспама
    is_safe, spam_msg, p_hash = validate_anti_spam(chat_id, text, photos)
    if not is_safe:
        return False, spam_msg

    # Проверяем, существует ли канал и админ ли бот
    chat_info = tg_api(token, "getChat", {"chat_id": chat_id})
    if not chat_info.get("ok"):
        err = chat_info.get("description", "Канал не найден или бот не добавлен")
        log.error(f"❌ [TG ERROR] Ошибка канала {chat_id}: {err}")
        return False, f"❌ Ошибка Telegram (Канал {chat_id}):\n{err}\n\nУбедись, что бот добавлен в канал и сделан администратором!"

    channel_title = chat_info.get("result", {}).get("title", chat_id)

    # Публикация
    success = False
    error_desc = ""

    if photos:
        # Отправляем первое фото с подписью
        photo_url = photos[0]
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": text[:1024],
            "parse_mode": "HTML"
        }
        res = tg_api(token, "sendPhoto", payload)
        if not res.get("ok"):
            # Фолбэк без HTML форматирования при ошибке разбора спецсимволов
            payload.pop("parse_mode", None)
            res = tg_api(token, "sendPhoto", payload)

        if res.get("ok"):
            success = True
        else:
            error_desc = res.get("description", "Unknown photo error")
    else:
        # Текстовое сообщение
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        res = tg_api(token, "sendMessage", payload)
        if not res.get("ok"):
            # Фолбэк без HTML
            payload.pop("parse_mode", None)
            res = tg_api(token, "sendMessage", payload)

        if res.get("ok"):
            success = True
        else:
            error_desc = res.get("description", "Unknown text error")

    if success:
        now = time.time()
        LAST_TG_POST_TIME[chat_id] = now
        if p_hash:
            TG_POST_HASHES[p_hash] = now

        log.info(f"✅ [TG SUCCESS] Пост успешно опубликован в '{channel_title}' ({chat_id})!")
        return True, f"✅ *ПОСТ УСПЕШНО ОПУБЛИКОВАН!*\n\n📢 Канал: *{channel_title}*\n🆔 ID: `{chat_id}`\n🖼 Вложений: {len(photos)}\n🛡 Антиспам: Активен"
    else:
        log.error(f"❌ [TG POST FAIL] {error_desc}")
        return False, f"❌ Ошибка публикации в Telegram:\n{error_desc}"

# ============ ПАРСИНГ ТГ-ПОСТОВ И КОМАНД ============

def parse_tg_input(text):
    """
    Парсит сообщение из чата ВК.
    Формат 1 (Пост):
    <chat_id>
    <bot_token>
    <текст поста>

    Формат 2 (Команда проверки админа):
    админ
    <bot_token>
    [chat_id]
    """
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        return None

    first_line = lines[0].lower()

    # Проверка команды "админ" / "каналы"
    if first_line in ["админ", "admin", "каналы", "channels", "тг_админ", "tg_admin"]:
        token = lines[1] if len(lines) > 1 else ""
        chat_id = lines[2] if len(lines) > 2 else None
        return {"action": "check_admin", "token": token, "chat_id": chat_id}

    if len(lines) < 3:
        return None

    chat_id = lines[0]
    token = lines[1]
    message_text = "\n".join(lines[2:])

    # Валидация формата токена и ID
    if ":" not in token or len(token) < 20:
        return None

    if not (chat_id.startswith("-100") or chat_id.startswith("@") or chat_id.lstrip("-").isdigit()):
        return None

    return {
        "action": "post",
        "chat_id": chat_id,
        "token": token,
        "message": message_text
    }

# ============ ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ПОИСК, КУРСЫ, ПОГОДА) ============

def search_duckduckgo(query):
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html",
            "Accept-Language": "ru-RU,ru;q=0.9"
        }
        r = requests.post(url, data={"q": query, "kl": "ru-ru"}, headers=headers, timeout=20)
        results = []
        snippets = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', r.text)
        snippets += re.findall(r'<a class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', r.text)
        for link, title in snippets[:5]:
            if "duckduckgo.com/l/?uddg=" in link:
                link = unquote(link.split("uddg=")[-1])
            title_clean = re.sub(r'<[^>]+>', '', title)
            results.append(f"📌 {title_clean}\n🔗 {link}")
        return "\n\n".join(results) if results else "❌ Ничего не нашёл"
    except Exception as e:
        return f"❌ Ошибка поиска: {e}"

def search_wikipedia(query):
    try:
        url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'))}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return f"📖 *{data.get('title', query)}*\n\n{data.get('extract', 'Нет описания')}\n\n🔗 {data.get('content_urls', {}).get('desktop', {}).get('page', '')}"
        return search_duckduckgo(f"википедия {query}")
    except Exception as e:
        return f"❌ Ошибка Википедии: {e}"

def get_cat_image():
    try:
        r = requests.get("https://api.thecatapi.com/v1/images/search", timeout=10)
        return r.json()[0]["url"]
    except:
        return None

def get_dog_image():
    try:
        r = requests.get("https://dog.ceo/api/breeds/image/random", timeout=10)
        return r.json().get("message")
    except:
        return None

def get_weather(city):
    try:
        url = f"https://wttr.in/{quote(city)}?format=3&lang=ru"
        r = requests.get(url, timeout=15)
        return f"🌤 Погода в {city}:\n{r.text.strip()}" if r.status_code == 200 else "❌ Город не найден"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_currency():
    try:
        r = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
        data = r.json()
        usd = data["Valute"]["USD"]
        eur = data["Valute"]["EUR"]
        return f"💰 Курсы ЦБ РФ:\n🇺🇸 USD: {usd['Value']:.2f} ₽\n🇪🇺 EUR: {eur['Value']:.2f} ₽"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_joke():
    jokes = [
        "Почему программисты путают Хэллоуин и Рождество? Потому что 31 OCT = 25 DEC",
        "— Доктор, я себя чувствую как JSON... — Ну расскажите... — Я не могу, у меня нет schema.",
        "Какой язык программирования самый закрытый? Java — потому что у неё всё private.",
        "Программист заходит в бар, заказывает 1 пиво, заказывает 10 пив, заказывает 0 пив...",
    ]
    return f"😂 {random.choice(jokes)}"

# ============ ОБРАБОТКА КОМАНД ============

def process_command(peer_id, text, msg_id=None):
    text_lower = text.lower().strip()

    # 1. Проверка Telegram формата
    tg_parsed = parse_tg_input(text)
    if tg_parsed:
        if tg_parsed["action"] == "check_admin":
            token = tg_parsed["token"]
            if not token:
                return "❌ Укажи токен бота! Пример:\nадмин\n8476739947:AAHP..."
            ok, report = check_tg_bot_admin_status(token, tg_parsed.get("chat_id"))
            return report

        elif tg_parsed["action"] == "post":
            photos, videos = get_vk_message_attachments(msg_id)
            ok, result_msg = send_tg_channel_post(
                chat_id=tg_parsed["chat_id"],
                token=tg_parsed["token"],
                text=tg_parsed["message"],
                photos=photos
            )
            return result_msg

    # 2. Команда "админ" в одну строку
    if text_lower.startswith("админ ") or text_lower.startswith("каналы "):
        parts = text.split()
        token = parts[1] if len(parts) > 1 else ""
        chat_id = parts[2] if len(parts) > 2 else None
        if not token:
            return "❌ Введи: `админ <ТОКЕН_БОТА>`"
        ok, report = check_tg_bot_admin_status(token, chat_id)
        return report

    # 3. Справка
    if text_lower in ["помощь", "help", "команды", "?", "хелп", "меню"]:
        return ("📋 *Инструкция и команды бота:*\n\n"
                "📢 *ОТПРАВКА ПОСТА В ТЕЛЕГРАМ КАНАЛ:*\n"
                "Напиши в чат в формате:\n"
                "`<ИД_КАНАЛА>`\n"
                "`<ТОКЕН_БОТА>`\n"
                "`<ТЕКСТ ПОСТА>`\n"
                "*(Можно прикреплять фото/видео к сообщению в ВК!)*\n\n"
                "Пример:\n"
                "`-1003402995613`\n"
                "`8476739947:AAHP7pyTa9Mpt_KhEioZ...`\n"
                "`Привет, мяу`\n\n"
                "🔍 *ПРОВЕРКА АДМИН-ПРАВ В ТЕЛЕГРАМ:*\n"
                "`админ <ТОКЕН_БОТА>` — покажет каналы, где бот админ\n\n"
                "🔍 `поиск <запрос>` — поиск в Google/DDG\n"
                "📖 `вики <запрос>` — Википедия\n"
                "🐱 `котик` — котики\n"
                "🐕 `песик` — собачки\n"
                "🌤 `погода <город>` — погода\n"
                "💰 `курс` — курсы валют\n"
                "😂 `шутка` — анекдоты\n"
                "⏸ `стоп` / ▶️ `старт` — пауза бота")

    if text_lower.startswith("поиск ") or text_lower.startswith("search "):
        q = text[7:].strip()
        return f"🔍 *Ищу:* {q}\n\n{search_duckduckgo(q)}" if q else "❌ Укажи запрос"

    if text_lower.startswith("вики ") or text_lower.startswith("wiki "):
        q = text[5:].strip()
        return search_wikipedia(q) if q else "❌ Укажи запрос"

    if text_lower in ["котик", "кот", "cat", "киса"]:
        url = get_cat_image()
        if url:
            upload_and_send_photo(peer_id, url, "🐱 Вот тебе котик!")
            return None
        return "❌ Ошибка загрузки котика"

    if text_lower in ["песик", "собака", "dog", "пёс"]:
        url = get_dog_image()
        if url:
            upload_and_send_photo(peer_id, url, "🐕 Вот тебе песик!")
            return None
        return "❌ Ошибка загрузки песика"

    if text_lower.startswith("погода "):
        return get_weather(text[7:].strip())

    if text_lower in ["курс", "валюта", "usd", "eur"]:
        return get_currency()

    if text_lower in ["шутка", "анекдот"]:
        return get_joke()

    return f"🔍 *Поиск в сети:* {text}\n\n{search_duckduckgo(text)}"

# ============ ОТПРАВКА ФОТО В ВК ============

def upload_and_send_photo(peer_id, photo_url, caption=""):
    try:
        upload_server = vk_api("photos.getMessagesUploadServer", {"peer_id": peer_id})
        if not upload_server:
            return False
        upload_url = upload_server["response"]["upload_url"]
        img_data = requests.get(photo_url, timeout=20).content
        files = {"photo": ("image.jpg", img_data)}
        upload_resp = requests.post(upload_url, files=files, timeout=30).json()
        saved = vk_api("photos.saveMessagesPhoto", {
            "photo": upload_resp["photo"],
            "server": upload_resp["server"],
            "hash": upload_resp["hash"]
        })
        if not saved or "response" not in saved:
            return False
        photo = saved["response"][0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        send_message(peer_id, caption, attachment)
        return True
    except Exception as e:
        log.error(f"❌ VK Photo upload error: {e}")
        return False

# ============ LONG POLL И ОСНОВНОЙ ЦИКЛ ============

START_TIME = int(time.time())
PROCESSED_MSGS = set()
BOT_PAUSED = False

def get_long_poll_server():
    resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
    if resp and "response" in resp:
        return resp["response"]
    return None

def listen_messages(user_id):
    server_data = get_long_poll_server()
    if not server_data:
        log.error("❌ Не удалось получить Long Poll сервер. Повтор через 10 сек...")
        time.sleep(10)
        return listen_messages(user_id)

    ts = server_data["ts"]
    server = server_data["server"]
    key = server_data["key"]

    log.info(f"🚀 БОТ УСПЕШНО ЗАПУЩЕН! Ожидание сообщений от пользователя ID={user_id}")
    log.info("🛡 Защита от спама и фильтрация дублей ВК/ТГ активирована.")

    first_run = True

    while True:
        try:
            url = f"https://{server}?act=a_check&key={key}&ts={ts}&wait=25&mode=2&version=3"
            r = requests.get(url, timeout=35)
            data = r.json()

            if "failed" in data:
                if data["failed"] == 1:
                    ts = data["ts"]
                    continue
                else:
                    server_data = get_long_poll_server()
                    if not server_data:
                        time.sleep(5)
                        continue
                    ts = server_data["ts"]
                    server = server_data["server"]
                    key = server_data["key"]
                    continue

            ts = data["ts"]

            if first_run:
                first_run = False
                old_count = len(data.get("updates", []))
                if old_count > 0:
                    log.info(f"🗑 Пропущено {old_count} старых сообщений (Защита от спама при старте)")
                continue

            for update in data.get("updates", []):
                if update[0] == 4:  # Новое сообщение
                    msg_id = update[1]
                    flags = update[2]
                    peer_id = update[3]
                    ts_msg = update[4]
                    text = update[5]
                    text_lower = text.lower().strip()

                    # Пропускаем исходящие сообщения
                    if flags & 2:
                        continue

                    # Реакция только на сообщения от владельца токена
                    if peer_id != user_id:
                        continue

                    # Проверка времени (игнорируем сообщения до запуска)
                    if ts_msg < START_TIME - 30:
                        continue

                    # Игнорируем свои же служебные ответы по префиксам
                    if any(text.startswith(p) for p in ["✅", "❌", "🛡", "🤖", "📋", "🔍", "📖", "🐱", "🐕", "🌤", "💰", "😂"]):
                        continue

                    global BOT_PAUSED
                    if text_lower in ["стоп", "stop"]:
                        BOT_PAUSED = True
                        send_message(peer_id, "⏸ Бот приостановлен. Напиши 'старт' для запуска.")
                        continue
                    elif text_lower in ["старт", "start"]:
                        BOT_PAUSED = False
                        send_message(peer_id, "▶️ Бот возобновил работу!")
                        continue

                    if BOT_PAUSED:
                        continue

                    log.info(f"📩 Новое сообщение в ВК: {text[:60]}...")
                    send_typing(peer_id)

                    reply_text = process_command(peer_id, text, msg_id=msg_id)
                    if reply_text:
                        send_message(peer_id, reply_text)
                        log.info("✅ Ответ отправлен в ВК чат.")

        except Exception as e:
            log.error(f"❌ Ошибка в цикле LongPoll: {e}")
            time.sleep(5)

# ============ ВЕБ-СЕРВЕР ДЛЯ ВВОДА ТОКЕНА (БЕЗ ИЗМЕНЕНИЙ) ============

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

CONFIG_FILE = "/tmp/vk_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VK & TG Browser Bot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0f0f23;
            color: #fff;
            font-family: 'Segoe UI', system-ui, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: #1a1a2e;
            border-radius: 20px;
            padding: 40px;
            max-width: 500px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        }
        h1 {
            font-size: 28px;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle { color: #888; margin-bottom: 30px; font-size: 14px; }
        label { display: block; margin-bottom: 8px; color: #aaa; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; }
        input[type="text"] {
            width: 100%; padding: 14px 16px; background: #0f0f23; border: 2px solid #333;
            border-radius: 12px; color: #fff; font-size: 14px; font-family: monospace; transition: border-color 0.3s;
        }
        input[type="text"]:focus { outline: none; border-color: #667eea; }
        .hint { color: #666; font-size: 12px; margin-top: 6px; margin-bottom: 20px; }
        button {
            width: 100%; padding: 16px; background: linear-gradient(135deg, #667eea, #764ba2);
            border: none; border-radius: 12px; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer;
        }
        .status { margin-top: 20px; padding: 14px; border-radius: 10px; font-size: 14px; display: none; }
        .status.ok { background: rgba(34,197,94,0.15); color: #22c55e; display: block; }
        .status.err { background: rgba(239,68,68,0.15); color: #ef4444; display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔥 VK & TG Bot</h1>
        <p class="subtitle">Постинг в Telegram-каналы и браузер через ВК</p>
        <form id="tokenForm">
            <label>Kate Mobile VK Token</label>
            <input type="text" id="token" placeholder="vk1.a.xxx... или полная ссылка" required>
            <p class="hint">Вставьте токен или ссылку Kate Mobile</p>
            <button type="submit">🚀 Запустить бота</button>
        </form>
        <div id="status" class="status"></div>
    </div>
    <script>
        document.getElementById('tokenForm').onsubmit = async function(e) {
            e.preventDefault();
            const token = document.getElementById('token').value.trim();
            const status = document.getElementById('status');
            status.className = 'status';
            status.style.display = 'block';
            status.textContent = '⏳ Проверяю VK токен...';
            try {
                const resp = await fetch('/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: token})
                });
                const data = await resp.json();
                if (data.ok) {
                    status.className = 'status ok';
                    status.innerHTML = '✅ Бот успешно запущен!<br>👤 ' + data.name + ' (ID: ' + data.user_id + ')<br>💬 Напишите "помощь" в чат с самим собой в ВК';
                } else {
                    status.className = 'status err';
                    status.textContent = '❌ ' + data.error;
                }
            } catch(err) {
                status.className = 'status err';
                status.textContent = '❌ Ошибка: ' + err.message;
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
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global VK_TOKEN
        if self.path == "/save":
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode('utf-8')
            data = json.loads(body)
            token = data.get("token", "").strip()

            if "access_token=" in token:
                match = re.search(r'access_token=([^&\s]+)', token)
                if match:
                    token = match.group(1)

            test_url = f"https://api.vk.com/method/users.get?access_token={token}&v=5.199"
            try:
                r = requests.get(test_url, timeout=10)
                vk_data = r.json()

                if "error" in vk_data:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": vk_data["error"]["error_msg"]}).encode())
                    return

                user = vk_data["response"][0]
                user_id = user["id"]
                name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()

                VK_TOKEN = token
                save_config({"token": token, "user_id": user_id})

                def start_bot():
                    listen_messages(user_id)

                bot_thread = threading.Thread(target=start_bot, daemon=True)
                bot_thread.start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user_id": user_id, "name": name}).encode())

            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.TCPServer(("", port), WebHandler) as httpd:
        log.info(f"🌐 Веб-интерфейс доступен на порту: {port}")
        httpd.serve_forever()

# ============ ЗАПУСК СЕРВЕРА ============

if __name__ == "__main__":
    log.info("🚀 Запуск VK-TG Browser Bot...")

    cfg = load_config()
    if cfg.get("token"):
        VK_TOKEN = cfg["token"]
        log.info("[+] VK Токен загружен из локального конфига")

        user_id = get_user_id()
        if user_id:
            def start_bot():
                listen_messages(user_id)
            bot_thread = threading.Thread(target=start_bot, daemon=True)
            bot_thread.start()

    start_web_server()
