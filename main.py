import os
import sys
import re
import json
import time
import random
import logging
import requests
from urllib.parse import quote, unquote

# ============ НАСТРОЙКИ ============
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_API_VERSION = "5.199"

# Парсим токен из полной ссылки (Kate Mobile)
# Формат: https://oauth.vk.com/blank.html#access_token=ТОКЕН&expires_in=0&user_id=123
if "access_token=" in VK_TOKEN:
    match = re.search(r'access_token=([^&\s]+)', VK_TOKEN)
    if match:
        VK_TOKEN = match.group(1)
        print(f"[+] Токен извлечён из ссылки")

if not VK_TOKEN:
    print("[!] Укажи VK_TOKEN в переменных окружения!")
    sys.exit(1)

# ============ ЛОГИ ============
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("vk-browser")

# ============ VK API ============

def vk_api(method, params):
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
            log.error(f"VK API error {err.get('error_code')}: {err.get('error_msg')}")
            return None
        return data
    except Exception as e:
        log.error(f"VK API request error: {e}")
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

# ============ ПОЛУЧЕНИЕ USER ID ============

def get_user_id():
    """Определяет ID владельца токена через API"""
    resp = vk_api("users.get", {})
    if resp and "response" in resp and len(resp["response"]) > 0:
        user_id = resp["response"][0]["id"]
        first_name = resp["response"][0].get("first_name", "")
        last_name = resp["response"][0].get("last_name", "")
        log.info(f"[+] Токен принадлежит: {first_name} {last_name} (ID: {user_id})")
        return user_id
    log.error("[!] Не удалось определить ID. Проверь токен!")
    return None

# ============ ПОИСК В ИНТЕРНЕТЕ ============

def search_duckduckgo(query):
    """Поиск через DuckDuckGo HTML версию"""
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"
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
    """Поиск в Википедии"""
    try:
        url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'))}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            title = data.get("title", query)
            extract = data.get("extract", "Нет описания")
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
            return f"📖 *{title}*\n\n{extract}\n\n🔗 {page_url}"
        return search_duckduckgo(f"википедия {query}")
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ КОТИКИ / ПЕСИКИ ============

def get_cat_image():
    apis = [
        "https://api.thecatapi.com/v1/images/search",
        "https://api.cataas.com/cat?json=true",
    ]
    for api in apis:
        try:
            r = requests.get(api, timeout=10)
            data = r.json()
            if isinstance(data, list) and "url" in data[0]:
                return data[0]["url"]
            if "url" in data:
                return data["url"]
        except:
            continue
    return None

def get_dog_image():
    try:
        r = requests.get("https://dog.ceo/api/breeds/image/random", timeout=10)
        return r.json().get("message")
    except:
        return None

# ============ ПОГОДА ============

def get_weather(city):
    try:
        url = f"https://wttr.in/{quote(city)}?format=3&lang=ru"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return f"🌤 Погода в {city}:\n{r.text.strip()}"
        return "❌ Город не найден"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ КУРС ВАЛЮТ ============

def get_currency():
    try:
        r = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
        data = r.json()
        usd = data["Valute"]["USD"]
        eur = data["Valute"]["EUR"]
        return (f"💰 Курсы ЦБ РФ:\n"
                f"🇺🇸 USD: {usd['Value']:.2f} ₽\n"
                f"🇪🇺 EUR: {eur['Value']:.2f} ₽")
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ ШУТКИ ============

def get_joke():
    try:
        r = requests.get("https://v2.jokeapi.dev/joke/Any?lang=ru&format=txt", timeout=10)
        if r.status_code == 200:
            return f"😂 {r.text.strip()}"
    except:
        pass

    jokes = [
        "Почему программисты путают Хэллоуин и Рождество? Потому что 31 OCT = 25 DEC",
        "— Доктор, я себя чувствую как JSON... — Ну расскажите... — Я не могу, у меня нет schema.",
        "Какой язык программирования самый закрытый? Java — потому что у неё всё private.",
        "Программист заходит в бар, заказывает 1 пиво, заказывает 10 пив, заказывает 0 пив... Бармен плачет.",
    ]
    return f"😂 {random.choice(jokes)}"

# ============ НОВОСТИ ============

def get_news():
    try:
        r = requests.get("https://meduza.io/rss/all", timeout=15)
        items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>', r.text, re.DOTALL)
        results = []
        for title, link in items[:5]:
            title_clean = re.sub(r'<[^>]+>', '', title)
            results.append(f"📰 {title_clean}\n🔗 {link}")
        return "\n\n".join(results)
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ ФАКТ ============

def get_fact():
    try:
        r = requests.get("https://uselessfacts.jsph.pl/random.json?language=ru", timeout=10)
        data = r.json()
        return f"🧠 {data.get('text', 'Факт не найден')}"
    except:
        facts = [
            "Медузы не имеют мозга, сердца и костей.",
            "Осьминоги имеют три сердца.",
            "Бананы — это ягоды, а клубника — нет.",
            "В Австралии больше верблюдов, чем в Египте.",
        ]
        return f"🧠 {random.choice(facts)}"

# ============ ПЕРЕВОД ============

def translate_text(text, target_lang="en"):
    try:
        url = "https://libretranslate.de/translate"
        payload = {
            "q": text,
            "source": "auto",
            "target": target_lang,
            "format": "text"
        }
        r = requests.post(url, data=payload, timeout=15)
        data = r.json()
        return f"🔄 Перевод:\n{text}\n\n➡️ {data.get('translatedText', 'Ошибка')}"
    except Exception as e:
        return f"❌ Ошибка перевода: {e}"

# ============ IP / ИНФО ============

def get_ip_info():
    try:
        r = requests.get("https://ipinfo.io/json", timeout=10)
        data = r.json()
        return (f"🌐 IP: {data.get('ip', 'N/A')}\n"
                f"📍 Город: {data.get('city', 'N/A')}\n"
                f"🏳️ Страна: {data.get('country', 'N/A')}\n"
                f"🏢 Провайдер: {data.get('org', 'N/A')}")
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ ОТПРАВКА ФОТО ============

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
        log.error(f"Photo upload error: {e}")
        return False

# ============ ОБРАБОТКА КОМАНД ============

def process_command(peer_id, text):
    text_lower = text.lower().strip()

    if text_lower in ["помощь", "help", "команды", "?", "хелп", "меню"]:
        return ("📋 *Команды бота:*\n\n"
                "🔍 `поиск <запрос>` — поиск в интернете\n"
                "📖 `вики <запрос>` — поиск в Википедии\n"
                "🐱 `котик` — случайный котик\n"
                "🐕 `песик` — случайная собака\n"
                "🌤 `погода <город>` — погода\n"
                "💰 `курс` — курсы валют\n"
                "😂 `шутка` — случайная шутка\n"
                "📰 `новости` — последние новости\n"
                "🧠 `факт` — случайный факт\n"
                "🔄 `перевод <текст>` — перевод на английский\n"
                "🌐 `ip` — информация о IP\n"
                "\n💡 Любой другой текст — поиск в интернете")

    if text_lower.startswith("поиск ") or text_lower.startswith("search "):
        query = text[7:].strip()
        if query:
            return f"🔍 Ищу: *{query}*...\n\n{search_duckduckgo(query)}"
        return "❌ Укажи запрос для поиска"

    if text_lower.startswith("вики ") or text_lower.startswith("wiki "):
        query = text[5:].strip()
        if query:
            return search_wikipedia(query)
        return "❌ Укажи запрос"

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
        if city:
            return get_weather(city)
        return "❌ Укажи город"

    if text_lower in ["курс", "валюта", "usd", "eur", "доллар", "евро"]:
        return get_currency()

    if text_lower in ["шутка", "анекдот", "joke", "смешно", "ржака"]:
        return get_joke()

    if text_lower in ["новости", "news", "новость"]:
        return get_news()

    if text_lower in ["факт", "fact", "интересно"]:
        return get_fact()

    if text_lower.startswith("перевод ") or text_lower.startswith("translate "):
        to_translate = text[8:].strip()
        if to_translate:
            return translate_text(to_translate)
        return "❌ Укажи текст для перевода"

    if text_lower in ["ip", "айпи", "мой ip", "интернет"]:
        return get_ip_info()

    return f"🔍 Ищу: *{text}*...\n\n{search_duckduckgo(text)}"

# ============ LONG POLL ============

def get_long_poll_server():
    resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
    if resp and "response" in resp:
        return resp["response"]
    return None

def listen_messages(user_id):
    server_data = get_long_poll_server()
    if not server_data:
        log.error("Не удалось получить Long Poll сервер")
        time.sleep(10)
        return listen_messages(user_id)

    ts = server_data["ts"]
    server = server_data["server"]
    key = server_data["key"]

    log.info(f"✅ Бот запущен! Жду сообщений от ID={user_id}")
    log.info("Отправь 'помощь' в чат с самим собой")

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

            for update in data.get("updates", []):
                if update[0] == 4:
                    flags = update[2]
                    peer_id = update[3]
                    text = update[5]

                    if flags & 2:
                        continue

                    if peer_id != user_id:
                        continue

                    log.info(f"📩 Запрос: {text[:50]}")
                    send_typing(peer_id)

                    result = process_command(peer_id, text)

                    if result:
                        send_message(peer_id, result)
                        log.info(f"✅ Ответ отправлен")
                    else:
                        log.info("✅ Фото отправлено")

        except Exception as e:
            log.error(f"Ошибка в цикле: {e}")
            time.sleep(5)

# ============ ЗАПУСК ============

if __name__ == "__main__":
    log.info("🚀 Запуск VK Browser Bot...")
    log.info("[Kate Mobile Token Mode]")

    # Автоопределение ID
    USER_ID = get_user_id()
    if not USER_ID:
        sys.exit(1)

    listen_messages(USER_ID)
