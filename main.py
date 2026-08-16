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
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

# ============ ВАЛИДАЦИЯ И НАСТРОЙКИ ============
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_API_VERSION = "5.199"

# Извлекаем токен из ссылки, если передана полная ссылка Kate Mobile
if VK_TOKEN and "access_token=" in VK_TOKEN:
    match = re.search(r'access_token=([^&\s]+)', VK_TOKEN)
    if match:
        VK_TOKEN = match.group(1)

# ============ СУПЕР ЛОГИ ============
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("VK-TG-Bot Engine")

# ============ VK API ХЕЛПЕРЫ ============

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
            log.error(f"[VK API ERROR] Code {err.get('error_code')}: {err.get('error_msg')}")
            return None
        return data
    except Exception as e:
        log.error(f"[VK API EXCEPTION] {e}")
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

def get_user_id():
    resp = vk_api("users.get", {})
    if resp and "response" in resp and len(resp["response"]) > 0:
        user_id = resp["response"][0]["id"]
        first_name = resp["response"][0].get("first_name", "")
        last_name = resp["response"][0].get("last_name", "")
        log.info(f"✅ [VK AUTH] Авторизован: {first_name} {last_name} (ID: {user_id})")
        return user_id
    log.error("❌ [VK AUTH] Ошибка получения профиля! Проверьте VK_TOKEN.")
    return None

# ============ ИЗВЛЕЧЕНИЕ ВЛОЖЕНИЙ (ФОТО/ВИДЕО/ДОКИ С ВК) ============

def extract_vk_attachments(msg_id):
    """Извлекает прямые ссылки на медиафайлы из сообщения ВК"""
    res = vk_api("messages.getById", {"message_ids": msg_id})
    if not res or "response" not in res or not res["response"].get("items"):
        return []

    msg_item = res["response"]["items"][0]
    attachments = msg_item.get("attachments", [])
    extracted = []

    for att in attachments:
        att_type = att.get("type")
        if att_type == "photo":
            sizes = att["photo"].get("sizes", [])
            if sizes:
                best_size = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                extracted.append({"type": "photo", "url": best_size.get("url")})
                log.info(f"[ATTACHMENT] Найдено фото: {best_size.get('url')[:60]}...")
        elif att_type == "video":
            video_info = att["video"]
            owner_id = video_info.get("owner_id")
            vid_id = video_info.get("id")
            access_key = video_info.get("access_key", "")
            v_res = vk_api("video.get", {"videos": f"{owner_id}_{vid_id}_{access_key}"})
            if v_res and "response" in v_res and v_res["response"].get("items"):
                v_item = v_res["response"]["items"][0]
                files = v_item.get("files", {})
                v_url = files.get("mp4_1080") or files.get("mp4_720") or files.get("mp4_480") or files.get("mp4_360") or files.get("mp4_240")
                if v_url:
                    extracted.append({"type": "video", "url": v_url})
                    log.info(f"[ATTACHMENT] Найдено видео MP4")
                elif v_item.get("player"):
                    extracted.append({"type": "video_player", "url": v_item.get("player")})
        elif att_type == "doc":
            doc = att["doc"]
            doc_url = doc.get("url")
            ext = doc.get("ext", "").lower()
            if ext in ["jpg", "jpeg", "png", "webp"]:
                extracted.append({"type": "photo", "url": doc_url})
            elif ext in ["mp4", "gif", "mov", "avi"]:
                extracted.append({"type": "video", "url": doc_url})
            else:
                extracted.append({"type": "doc", "url": doc_url, "title": doc.get("title", "document")})
            log.info(f"[ATTACHMENT] Найден документ ({ext})")

    return extracted

# ============ ПОИСК И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def search_duckduckgo(query):
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html",
            "Accept-Language": "ru-RU,ru;q=0.9"
        }
        r = requests.post(url, data={"q": query, "kl": "ru-ru"}, headers=headers, timeout=15)
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
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return f"📖 *{data.get('title', query)}*\n\n{data.get('extract', 'Нет описания')}\n\n🔗 {data.get('content_urls', {}).get('desktop', {}).get('page', '')}"
        return search_duckduckgo(f"википедия {query}")
    except Exception as e:
        return f"❌ Ошибка: {e}"

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
        r = requests.get(url, timeout=10)
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
        "Программист заходит в бар, заказывает 1 пиво, заказывает 10 пив, заказывает 0 пив... Бармен плачет.",
    ]
    return f"😂 {random.choice(jokes)}"

def get_news():
    try:
        r = requests.get("https://meduza.io/rss/all", timeout=10)
        items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>', r.text, re.DOTALL)
        results = [f"📰 {re.sub(r'<[^>]+>', '', title)}\n🔗 {link}" for title, link in items[:5]]
        return "\n\n".join(results)
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_fact():
    facts = ["Медузы не имеют мозга, сердца и костей.", "Осьминоги имеют три сердца.", "Бананы — это ягоды, а клубника — нет."]
    return f"🧠 {random.choice(facts)}"

def translate_text(text, target_lang="en"):
    try:
        r = requests.post("https://libretranslate.de/translate", data={"q": text, "source": "auto", "target": target_lang, "format": "text"}, timeout=10)
        return f"🔄 Перевод:\n{text}\n\n➡️ {r.json().get('translatedText', 'Ошибка')}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_ip_info():
    try:
        r = requests.get("https://ipinfo.io/json", timeout=10)
        data = r.json()
        return f"🌐 IP: {data.get('ip', 'N/A')}\n📍 Город: {data.get('city', 'N/A')}\n🏳️ Страна: {data.get('country', 'N/A')}\n🏢 Провайдер: {data.get('org', 'N/A')}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def upload_and_send_photo(peer_id, photo_url, caption=""):
    try:
        upload_server = vk_api("photos.getMessagesUploadServer", {"peer_id": peer_id})
        if not upload_server:
            return False
        upload_url = upload_server["response"]["upload_url"]
        img_data = requests.get(photo_url, timeout=20).content
        files = {"photo": ("image.jpg", img_data)}
        upload_resp = requests.post(upload_url, files=files, timeout=25).json()
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
        log.error(f"[VK PHOTO ERROR] {e}")
        return False

# ============ ЗАЩИТА ОТ СПАМА И ФЛУДА ============

START_TIME = int(time.time())
PROCESSED_MSGS = set()
TG_PROCESSED_HASHES = set()
TG_LAST_POST_TIME = {}  # {chat_id: timestamp}
BOT_PAUSED = False

def is_spam_request(peer_id, text):
    msg_id = hash(f"{peer_id}:{text}:{int(time.time() / 15)}")
    if msg_id in PROCESSED_MSGS:
        log.warning(f"⚠️ [SPAM GUARD] Обнаружен дубликат запроса, пропускаем: {text[:30]}")
        return True
    PROCESSED_MSGS.add(msg_id)
    if len(PROCESSED_MSGS) > 1000:
        PROCESSED_MSGS.clear()
    return False

def can_post_to_tg_channel(chat_id):
    """Защита от частого спама в один и тот же Telegram канал (минимум 3 секунды паузы)"""
    now = time.time()
    last_time = TG_LAST_POST_TIME.get(chat_id, 0)
    if now - last_time < 3.0:
        log.warning(f"⚠️ [SPAM GUARD] Защита канала {chat_id}: частая отправка! Задержка 3с.")
        return False
    TG_LAST_POST_TIME[chat_id] = now
    return True

# ============ TELEGRAM ПАРСИНГ И КАНАЛЫ ============

def parse_tg_post(text):
    """
    Универсальный парсер. Поддерживает форматы:
    1) Канал_ID\nТокен\nСообщение
    2) Токен\nКанал_ID\nСообщение
    """
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if len(lines) < 2:
        return None

    line0 = lines[0]
    line1 = lines[1]

    token = None
    chat_id = None
    text_start_idx = 2

    # Регулярка для Telegram Bot Token
    token_pattern = re.compile(r'^\d{8,12}:[A-Za-z0-9_-]{30,50}$')

    if token_pattern.match(line0):
        token = line0
        chat_id = line1
    elif token_pattern.match(line1):
        token = line1
        chat_id = line0
    elif ":" in line0 and re.match(r'^\d+:', line0):
        token = line0
        chat_id = line1
    elif ":" in line1 and re.match(r'^\d+:', line1):
        token = line1
        chat_id = line0

    if not token or not chat_id:
        return None

    # Валидация chat_id (@username, -100xxx, -xxx или цифры)
    if not (chat_id.startswith("-") or chat_id.startswith("@") or chat_id.isdigit()):
        return None

    message = "\n".join(lines[text_start_idx:]).strip() if len(lines) > text_start_idx else ""

    return {
        "chat_id": chat_id,
        "token": token,
        "message": message
    }

def check_bot_channels(token, specific_chat_id=None):
    """
    Автоматически определяет каналы и чаты, где Telegram бот является админом/участником
    """
    bot_url = f"https://api.telegram.org/bot{token}"

    try:
        r = requests.get(f"{bot_url}/getMe", timeout=10)
        me_data = r.json()
        if not me_data.get("ok"):
            return f"❌ Ошибка токена Telegram! TG вернул: {me_data.get('description')}"

        bot_info = me_data["result"]
        bot_username = bot_info.get("username", "bot")
        bot_id = bot_info.get("id")
        bot_name = bot_info.get("first_name", "TG Bot")
    except Exception as e:
        return f"❌ Ошибка подключения к Telegram API: {e}"

    discovered = {}

    # 1. Проверяем точечно переданный chat_id
    if specific_chat_id:
        try:
            r_chat = requests.get(f"{bot_url}/getChat", params={"chat_id": specific_chat_id}, timeout=10)
            c_data = r_chat.json()
            if c_data.get("ok"):
                c_info = c_data["result"]
                cid = str(c_info.get("id"))
                title = c_info.get("title") or c_info.get("username") or "Канал"

                r_mem = requests.get(f"{bot_url}/getChatMember", params={"chat_id": cid, "user_id": bot_id}, timeout=10)
                mem_data = r_mem.json()
                status = "unknown"
                can_post = "Неизвестно"
                if mem_data.get("ok"):
                    m_info = mem_data["result"]
                    status = m_info.get("status")
                    if status in ["administrator", "creator"]:
                        can_post = "ДА ✅" if m_info.get("can_post_messages", True) else "НЕТ ❌"

                discovered[cid] = {
                    "title": title,
                    "type": c_info.get("type", "channel"),
                    "status": status,
                    "can_post": can_post,
                    "username": c_info.get("username")
                }
        except Exception as e:
            log.error(f"[DISCOVERY ERROR] {e}")

    # 2. Получаем обновленные данные из getUpdates
    try:
        r_up = requests.get(f"{bot_url}/getUpdates", params={"limit": 100, "allowed_updates": ["my_chat_member", "channel_post", "message"]}, timeout=10)
        up_data = r_up.json()
        if up_data.get("ok"):
            for update in up_data.get("result", []):
                chat = None
                status = "administrator"
                can_post = "ДА ✅"

                if "my_chat_member" in update:
                    mcm = update["my_chat_member"]
                    chat = mcm.get("chat")
                    new_mem = mcm.get("new_chat_member", {})
                    status = new_mem.get("status", "administrator")
                    if "can_post_messages" in new_mem:
                        can_post = "ДА ✅" if new_mem.get("can_post_messages") else "НЕТ ❌"
                elif "channel_post" in update:
                    chat = update["channel_post"].get("chat")
                elif "message" in update:
                    chat = update["message"].get("chat")

                if chat and chat.get("type") in ["channel", "supergroup", "group"]:
                    cid = str(chat.get("id"))
                    title = chat.get("title") or chat.get("username") or f"Чат {cid}"
                    username = chat.get("username")
                    if cid not in discovered:
                        discovered[cid] = {
                            "title": title,
                            "type": chat.get("type"),
                            "status": status,
                            "can_post": can_post,
                            "username": username
                        }
    except Exception as e:
        log.error(f"[DISCOVERY UPDATES ERROR] {e}")

    # Формируем красивый отчет
    report = f"🤖 Информация о боте Telegram:\n"
    report += f"Имя: {bot_name}\n"
    report += f"Юзернейм: @{bot_username}\n"
    report += f"ID Ботa: {bot_id}\n\n"

    if discovered:
        report += f"📢 Найденные каналы/чаты ({len(discovered)}):\n"
        for cid, info in discovered.items():
            status_str = "👑 АДМИНИСТРАТОР" if info['status'] in ['administrator', 'creator'] else f"Участник ({info['status']})"
            uname_str = f" (@{info['username']})" if info.get('username') else ""
            report += f"────────────────\n"
            report += f"📌 {info['title']}{uname_str}\n"
            report += f"🆔 ID Канала: {cid}\n"
            report += f"🛡 Статус бота: {status_str}\n"
            report += f"✍ Публикация постов: {info['can_post']}\n"
    else:
        report += "⚠️ Напрямую каналы в логе обновлений не найдены.\n"
        report += "💡 Как легко привязать канал:\n"
        report += f"1. Добавь бота @{bot_username} в свой канал АДМИНИСТРАТОРОМ.\n"
        report += "2. Напиши любое тестовое сообщение в канале.\n"
        report += f"3. Или напиши в VK команду: каналы {token} <ID_вашего_канала>\n"

    report += "\n\n📤 Формат для отправки поста из VK в ТГ:\n"
    report += "ID_КАНАЛА\n"
    report += "ТОКЕН_БОТА\n"
    report += "Текст поста (можно прикрепить фото/видео!)"

    return report

def send_tg_post_full(chat_id, token, message, attachments=None):
    """
    Отправляет пост в Telegram (текст + вложенные фото/видео) с полной защитой от спама.
    """
    bot_url = f"https://api.telegram.org/bot{token}"

    if not can_post_to_tg_channel(chat_id):
        return False, "⚠️ Защита от спама: подождите 3 секунды перед следующей отправкой!"

    # Проверка на повторный идентичный пост
    post_hash = hash(f"{chat_id}:{message}:{len(attachments or [])}")
    if post_hash in TG_PROCESSED_HASHES:
        log.warning(f"[SPAM BLOCK] Блокировка дубликата поста для {chat_id}")
        return False, "⚠️ Защита от спама: этот пост уже был отправлен ранее!"

    TG_PROCESSED_HASHES.add(post_hash)
    if len(TG_PROCESSED_HASHES) > 500:
        TG_PROCESSED_HASHES.clear()

    try:
        # 1. ТЕКСТОВЫЙ ПОСТ (без вложений)
        if not attachments:
            url = f"{bot_url}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }
            log.info(f"[TG POST] Отправка текста в {chat_id}: {message[:40]}...")
            r = requests.post(url, json=payload, timeout=25)
            data = r.json()

            if data.get("ok"):
                log.info(f"✅ [TG SUCCESS] Пост опубликован в {chat_id}")
                return True, f"✅ Пост успешно опубликован в канал {chat_id}!\n\n📝 {message[:100]}"

            # Повтор без разметки HTML, если ошибка формата
            if "can't parse entities" in str(data.get("description", "")).lower():
                payload.pop("parse_mode", None)
                r = requests.post(url, json=payload, timeout=25)
                if r.json().get("ok"):
                    return True, f"✅ Пост опубликован в {chat_id} (в обычном режиме без HTML)!"

            log.error(f"❌ [TG ERROR] {data.get('description')}")
            return False, f"❌ Ошибка Telegram API:\n{data.get('description')}"

        # 2. ПОСТ С ВЛОЖЕНИЯМИ (ФОТО / ВИДЕО / ДОКИ)
        log.info(f"[TG POST] Отправка поста с {len(attachments)} медиафайлами в {chat_id}")
        has_sent = False
        last_err = ""

        for idx, att in enumerate(attachments):
            att_type = att.get("type")
            url = att.get("url")
            caption = message if idx == 0 else ""  # Прикрепляем текст сообщения к первому файлу

            if not url:
                continue

            # Скачиваем файл из VK для гарантированной отправки
            file_res = requests.get(url, timeout=30)
            if file_res.status_code != 200:
                log.error(f"[TG ERROR] Не удалось скачать медиафайл по ссылке")
                continue

            content_bytes = file_res.content

            if att_type == "photo":
                tg_endpoint = f"{bot_url}/sendPhoto"
                files = {"photo": ("image.jpg", content_bytes)}
                data_payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            elif att_type == "video":
                tg_endpoint = f"{bot_url}/sendVideo"
                files = {"video": ("video.mp4", content_bytes)}
                data_payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            else:
                tg_endpoint = f"{bot_url}/sendDocument"
                files = {"document": (att.get("title", "file.bin"), content_bytes)}
                data_payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}

            r = requests.post(tg_endpoint, data=data_payload, files=files, timeout=45)
            res_data = r.json()

            if res_data.get("ok"):
                has_sent = True
                log.info(f"✅ [TG MEDIA SUCCESS] Вложение #{idx+1} отправлено!")
            else:
                # Повтор без HTML если форматирование вызвало ошибку
                if "can't parse entities" in str(res_data.get("description", "")).lower():
                    data_payload.pop("parse_mode", None)
                    r_retry = requests.post(tg_endpoint, data=data_payload, files=files, timeout=45)
                    if r_retry.json().get("ok"):
                        has_sent = True
                        continue
                last_err = res_data.get("description", "Unknown error")
                log.error(f"❌ [TG MEDIA ERROR] {last_err}")

        if has_sent:
            return True, f"✅ Пост с медиафайлами успешно опубликован в канал {chat_id}!"
        else:
            return False, f"❌ Не удалось отправить медиафайлы в TG:\n{last_err}"

    except Exception as e:
        log.error(f"❌ [TG EXCEPTION] {e}")
        return False, f"❌ Исключение при отправке в Telegram: {e}"

# ============ ОБРАБОТКА КОМАНД VK ============

def process_command(peer_id, text):
    text_lower = text.lower().strip()

    # Проверка команды получения администрируемых каналов
    if text_lower.startswith("канал") or text_lower.startswith("каналы") or text_lower.startswith("админ") or text_lower.startswith("/channels"):
        parts = text.split()
        token = None
        chat_id = None
        for part in parts[1:]:
            part_clean = part.strip()
            if ":" in part_clean and len(part_clean) > 20:
                token = part_clean
            elif part_clean.startswith("-") or part_clean.startswith("@") or part_clean.isdigit():
                chat_id = part_clean

        if not token:
            return ("📢 *Команда проверки ТГ каналов*\n\n"
                    "Отправь команду вместе с токеном бота:\n"
                    "`каналы ТОКЕН_БОТА`\n\n"
                    "Пример:\n"
                    "каналы 8476739947:AAHP7pyTa9Mpt_KhEioZ48sx1-cSwOz83_4\n\n"
                    "Бот покажет список каналов, где он админ, и их ID!")

        log.info(f"[DISCOVERY COMMAND] Проверка каналов для токена {token[:10]}...")
        return check_bot_channels(token, chat_id)

    if text_lower in ["помощь", "help", "команды", "?", "хелп", "меню"]:
        return ("📋 *Команды бота:*\n\n"
                "📢 `каналы <ТОКЕН_БОТА>` — узнать где бот админ и его ID каналов!\n\n"
                "📤 *Отправка поста в ТГ канал:*\n"
                "Отправь 3 строки в чат:\n"
                "-1003402995613\n"
                "8476739947:AAHP7pyTa9Mpt...\n"
                "Привет, новый пост!\n"
                "(Также можно прикрепить фото или видео!)\n\n"
                "🔍 `поиск <запрос>` — поиск в интернете\n"
                "📖 `вики <запрос>` — поиск в Википедии\n"
                "🐱 `котик` — случайный котик\n"
                "🐕 `песик` — случайный песик\n"
                "🌤 `погода <город>` — погода\n"
                "💰 `курс` — курсы валют\n"
                "😂 `шутка` — случайная шутка\n"
                "📰 `новости` — последние новости\n"
                "🧠 `факт` — случайный факт\n"
                "🔄 `перевод <текст>` — перевод на английский\n"
                "🌐 `ip` — информация о IP\n"
                "\n⏸ `стоп` — приостановить бота\n"
                "▶️ `старт` — возобновить работу")

    if text_lower.startswith("поиск ") or text_lower.startswith("search "):
        query = text[7:].strip()
        return f"🔍 Ищу: *{query}*...\n\n{search_duckduckgo(query)}" if query else "❌ Укажите запрос"

    if text_lower.startswith("вики ") or text_lower.startswith("wiki "):
        query = text[5:].strip()
        return search_wikipedia(query) if query else "❌ Укажите запрос"

    if text_lower in ["котик", "кот", "кошка", "cat", "киса"]:
        cat_url = get_cat_image()
        if cat_url:
            upload_and_send_photo(peer_id, cat_url, "🐱 Вот тебе котик!")
            return None
        return "❌ Не удалось найти котика"

    if text_lower in ["песик", "собака", "dog", "пёс", "щенок"]:
        dog_url = get_dog_image()
        if dog_url:
            upload_and_send_photo(peer_id, dog_url, "🐕 Вот тебе песик!")
            return None
        return "❌ Не удалось найти песика"

    if text_lower.startswith("погода "):
        city = text[7:].strip()
        return get_weather(city) if city else "❌ Укажите город"

    if text_lower in ["курс", "валюта", "usd", "eur", "доллар", "евро"]:
        return get_currency()

    if text_lower in ["шутка", "анекдот", "joke", "смешно"]:
        return get_joke()

    if text_lower in ["новости", "news", "новость"]:
        return get_news()

    if text_lower in ["факт", "fact", "интересно"]:
        return get_fact()

    if text_lower.startswith("перевод ") or text_lower.startswith("translate "):
        to_translate = text[8:].strip()
        return translate_text(to_translate) if to_translate else "❌ Укажите текст"

    if text_lower in ["ip", "айпи", "мой ip", "интернет"]:
        return get_ip_info()

    return f"🔍 Ищу: *{text}*...\n\n{search_duckduckgo(text)}"

# ============ LONG POLL ЦИКЛ СЛУШАНИЯ ============

def get_long_poll_server():
    resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
    if resp and "response" in resp:
        return resp["response"]
    return None

def listen_messages(user_id):
    server_data = get_long_poll_server()
    if not server_data:
        log.error("❌ Не удалось получить Long Poll сервер ВК. Повтор через 10сек.")
        time.sleep(10)
        return listen_messages(user_id)

    ts = server_data["ts"]
    server = server_data["server"]
    key = server_data["key"]

    log.info(f"🚀 [BOT STARTED] Бот запущен! Ожидание сообщений от ID={user_id}")
    log.info(f"⏱ Временная метка запуска: {START_TIME}")

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

            # При запуске пропускаем накопившийся старый флуд
            if first_run:
                first_run = False
                old_count = len(data.get("updates", []))
                if old_count > 0:
                    log.info(f"🗑 [SPAM GUARD] Пропущено {old_count} старых сообщений при старте")
                continue

            for update in data.get("updates", []):
                if update[0] == 4:  # Новое сообщение
                    msg_id = update[1]
                    flags = update[2]
                    peer_id = update[3]
                    ts_msg = update[4]
                    text = update[5]
                    text_lower = text.lower().strip()

                    # Исходящие сообщения бота пропускаем
                    if flags & 2:
                        continue

                    # Отвечаем только на сообщения в чате с самим собой
                    if peer_id != user_id:
                        continue

                    # ЗАЩИТА 1: Сообщения до запуска бота
                    if ts_msg < START_TIME - 30:
                        continue

                    # ЗАЩИТА 2: Дубликаты сообщений
                    if is_spam_request(peer_id, text):
                        continue

                    # ЗАЩИТА 3: Игнорируем ответы бота по спецсимволам
                    if text.startswith("🔍") or text.startswith("📋") or text.startswith("🐱") or text.startswith("🐕") or text.startswith("🌤") or text.startswith("💰") or text.startswith("😂") or text.startswith("📰") or text.startswith("🧠") or text.startswith("🔄") or text.startswith("🌐") or text.startswith("📖") or text.startswith("❌") or text.startswith("✅") or text.startswith("🤖") or text.startswith("📢") or text.startswith("⚠️"):
                        continue

                    log.info(f"📩 [NEW VK MSG] {text[:60]}")

                    global BOT_PAUSED
                    if text_lower in ["стоп", "stop"]:
                        BOT_PAUSED = True
                        send_message(peer_id, "⏸ Бот приостановлен. Напишите 'старт', чтобы возобновить.")
                        continue
                    elif text_lower in ["старт", "start"]:
                        BOT_PAUSED = False
                        send_message(peer_id, "▶️ Бот возобновил работу!")
                        continue

                    if BOT_PAUSED:
                        continue

                    send_typing(peer_id)

                    # 1. Проверяем, является ли сообщение запросом на отправку поста в Telegram
                    parsed_tg = parse_tg_post(text)
                    if parsed_tg:
                        log.info(f"🎯 [TG POST DETECTED] Канал: {parsed_tg['chat_id']}, Токен: {parsed_tg['token'][:10]}...")
                        # Извлекаем вложенные фото/видео из сообщения ВК
                        vk_attachments = extract_vk_attachments(msg_id)
                        ok, response_msg = send_tg_post_full(
                            chat_id=parsed_tg["chat_id"],
                            token=parsed_tg["token"],
                            message=parsed_tg["message"],
                            attachments=vk_attachments
                        )
                        send_message(peer_id, response_msg)
                        continue

                    # 2. Обработка стандартных команд
                    result = process_command(peer_id, text)
                    if result:
                        send_message(peer_id, result)
                        log.info(f"✅ [VK RESPONSE SENT]")

        except Exception as e:
            log.error(f"❌ [LOOP EXCEPTION] {e}")
            time.sleep(5)

# ============ ВЕБ-СЕРВЕР ============

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
    <title>VK Browser & TG Poster Bot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0f0f23; color: #fff; font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
        .container { background: #1a1a2e; border-radius: 20px; padding: 40px; max-width: 500px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
        h1 { font-size: 26px; margin-bottom: 10px; background: linear-gradient(135deg, #667eea, #764ba2); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .subtitle { color: #888; margin-bottom: 25px; font-size: 14px; }
        label { display: block; margin-bottom: 8px; color: #aaa; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
        input[type="text"] { width: 100%; padding: 14px 16px; background: #0f0f23; border: 2px solid #333; border-radius: 12px; color: #fff; font-size: 14px; font-family: monospace; transition: border-color 0.3s; }
        input[type="text"]:focus { outline: none; border-color: #667eea; }
        button { width: 100%; padding: 16px; margin-top: 15px; background: linear-gradient(135deg, #667eea, #764ba2); border: none; border-radius: 12px; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer; }
        .status { margin-top: 20px; padding: 14px; border-radius: 10px; font-size: 14px; display: none; }
        .status.ok { background: rgba(34,197,94,0.15); color: #22c55e; display: block; }
        .status.err { background: rgba(239,68,68,0.15); color: #ef4444; display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔥 VK Browser & TG Poster</h1>
        <p class="subtitle">Управление публикациями в TG через VK</p>
        <form id="tokenForm">
            <label>VK Token (Kate Mobile)</label>
            <input type="text" id="token" placeholder="vk1.a.xxx... или ссылка" required>
            <button type="submit">🚀 Запустить бота</button>
        </form>
        <div id="status" class="status"></div>
    </div>
    <script>
        document.getElementById('tokenForm').onsubmit = async function(e) {
            e.preventDefault();
            const token = document.getElementById('token').value.trim();
            const status = document.getElementById('status');
            status.className = 'status'; status.style.display = 'block'; status.textContent = '⏳ Проверяю токен...';
            try {
                const resp = await fetch('/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: token})
                });
                const data = await resp.json();
                if (data.ok) {
                    status.className = 'status ok';
                    status.innerHTML = '✅ Бот успешно запущен!<br>👤 ' + data.name + ' (ID: ' + data.user_id + ')';
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
        log.info(f"🌐 [WEB SERVER] Веб-интерфейс запущен на порту {port}")
        httpd.serve_forever()

# ============ ТОЧКА ВХОДА ============

if __name__ == "__main__":
    log.info("🚀 Запуск супер-обновленного VK & TG Poster Bot...")

    cfg = load_config()
    if cfg.get("token"):
        VK_TOKEN = cfg["token"]
        log.info("[+] Загружен токен VK из конфигурации")

        user_id = get_user_id()
        if user_id:
            def start_bot():
                listen_messages(user_id)
            bot_thread = threading.Thread(target=start_bot, daemon=True)
            bot_thread.start()

    start_web_server()
