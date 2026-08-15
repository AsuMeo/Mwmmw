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
        print(f"[+] Токен извлечён из ссылки")

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
    resp = vk_api("users.get", {})
    if resp and "response" in resp and len(resp["response"]) > 0:
        user_id = resp["response"][0]["id"]
        first_name = resp["response"][0].get("first_name", "")
        last_name = resp["response"][0].get("last_name", "")
        log.info(f"[+] Токен принадлежит: {first_name} {last_name} (ID: {user_id})")
        return user_id
    log.error("[!] Не удалось определить ID. Проверь токен!")
    return None

# ============ ПОИСК ============

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
        return f"❌ Ошибка: {e}"

def search_wikipedia(query):
    try:
        url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'))}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return f"📖 *{data.get('title', query)}*\n\n{data.get('extract', 'Нет описания')}\n\n🔗 {data.get('content_urls', {}).get('desktop', {}).get('page', '')}"
        return search_duckduckgo(f"википедия {query}")
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ============ КОТИКИ / ПЕСИКИ ============

def get_cat_image():
    try:
        r = requests.get("https://api.thecatapi.com/v1/images/search", timeout=10)
        data = r.json()
        return data[0]["url"] if data else None
    except:
        return None

def get_dog_image():
    try:
        r = requests.get("https://dog.ceo/api/breeds/image/random", timeout=10)
        return r.json().get("message")
    except:
        return None

# ============ ПОГОДА / ВАЛЮТА / ШУТКИ / НОВОСТИ / ФАКТ / ПЕРЕВОД / IP ============

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

def get_news():
    try:
        r = requests.get("https://meduza.io/rss/all", timeout=15)
        items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>', r.text, re.DOTALL)
        results = []
        for title, link in items[:5]:
            results.append(f"📰 {re.sub(r'<[^>]+>', '', title)}\n🔗 {link}")
        return "\n\n".join(results)
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_fact():
    try:
        r = requests.get("https://uselessfacts.jsph.pl/random.json?language=ru", timeout=10)
        return f"🧠 {r.json().get('text', 'Факт не найден')}"
    except:
        facts = ["Медузы не имеют мозга, сердца и костей.", "Осьминоги имеют три сердца.", "Бананы — это ягоды, а клубника — нет."]
        return f"🧠 {random.choice(facts)}"

def translate_text(text, target_lang="en"):
    try:
        r = requests.post("https://libretranslate.de/translate", data={"q": text, "source": "auto", "target": target_lang, "format": "text"}, timeout=15)
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
        return f"🔍 Ищу: *{query}*...\n\n{search_duckduckgo(query)}" if query else "❌ Укажи запрос"

    if text_lower.startswith("вики ") or text_lower.startswith("wiki "):
        query = text[5:].strip()
        return search_wikipedia(query) if query else "❌ Укажи запрос"

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
        return get_weather(city) if city else "❌ Укажи город"

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
        return translate_text(to_translate) if to_translate else "❌ Укажи текст"

    if text_lower in ["ip", "айпи", "мой ip", "интернет"]:
        return get_ip_info()

    return f"🔍 Ищу: *{text}*...\n\n{search_duckduckgo(text)}"

# ============ LONG POLL ============

def get_long_poll_server():
    resp = vk_api("messages.getLongPollServer", {"lp_version": 3})
    if resp and "response" in resp:
        return resp["response"]
    return None

# ============ ЗАЩИТА ОТ СПАМА ============
START_TIME = int(time.time())
PROCESSED_MSGS = set()  # Хеши обработанных сообщений

def msg_hash(peer_id, text, ts_approx):
    """Уникальный хеш сообщения для защиты от дублей"""
    return hash(f"{peer_id}:{text}:{ts_approx}")

def is_spam_risk(peer_id, text):
    """Проверяем, не спамим ли мы"""
    # Проверяем, не отвечали ли уже на это
    msg_id = msg_hash(peer_id, text, int(time.time() / 10))
    if msg_id in PROCESSED_MSGS:
        log.warning(f"⚠️ Дубль сообщения, пропускаем: {text[:30]}")
        return True
    PROCESSED_MSGS.add(msg_id)
    # Ограничиваем размер памяти
    if len(PROCESSED_MSGS) > 1000:
        PROCESSED_MSGS.clear()
    return False

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
    log.info(f"⏱ Время запуска: {START_TIME}")
    log.info("💡 Отправь 'помощь' в чат с самим собой")

    # Пропускаем первую порцию старых сообщений
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

            # Первый запуск — пропускаем ВСЕ старые сообщения
            if first_run:
                first_run = False
                old_count = len(data.get("updates", []))
                if old_count > 0:
                    log.info(f"🗑 Пропущено {old_count} старых сообщений (защита от спама)")
                continue

            for update in data.get("updates", []):
                if update[0] == 4:  # Новое сообщение
                    flags = update[2]
                    peer_id = update[3]
                    ts_msg = update[4]  # Временная метка сообщения
                    text = update[5]

                    # ИСХОДЯЩИЕ — пропускаем (это наши ответы)
                    if flags & 2:
                        continue

                    # Только чат с собой
                    if peer_id != user_id:
                        continue

                    # ЗАЩИТА 1: Сообщение старше запуска бота
                    if ts_msg < START_TIME - 60:  # Допуск 60 сек на рассинхрон
                        log.info(f"🗑 Старое сообщение пропущено ({ts_msg} < {START_TIME}): {text[:30]}")
                        continue

                    # ЗАЩИТА 2: Дубли
                    if is_spam_risk(peer_id, text):
                        continue

                    # ЗАЩИТА 3: Не отвечаем на свои же сообщения (по тексту)
                    if text.startswith("🔍") or text.startswith("📋") or text.startswith("🐱") or text.startswith("🐕") or text.startswith("🌤") or text.startswith("💰") or text.startswith("😂") or text.startswith("📰") or text.startswith("🧠") or text.startswith("🔄") or text.startswith("🌐") or text.startswith("📖") or text.startswith("❌"):
                        log.info(f"🗑 Это наше сообщение, пропускаем: {text[:30]}")
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

# ============ ВЕБ-СЕРВЕР ДЛЯ ВВОДА ТОКЕНА ============

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

CONFIG_FILE = "/tmp/vk_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
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
    <title>VK Browser Bot</title>
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
        .subtitle {
            color: #888;
            margin-bottom: 30px;
            font-size: 14px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #aaa;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        input[type="text"] {
            width: 100%;
            padding: 14px 16px;
            background: #0f0f23;
            border: 2px solid #333;
            border-radius: 12px;
            color: #fff;
            font-size: 14px;
            font-family: monospace;
            transition: border-color 0.3s;
        }
        input[type="text"]:focus {
            outline: none;
            border-color: #667eea;
        }
        .hint {
            color: #666;
            font-size: 12px;
            margin-top: 6px;
            margin-bottom: 20px;
        }
        button {
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            border: none;
            border-radius: 12px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(102,126,234,0.4);
        }
        .status {
            margin-top: 20px;
            padding: 14px;
            border-radius: 10px;
            font-size: 14px;
            display: none;
        }
        .status.ok { background: rgba(34,197,94,0.15); color: #22c55e; display: block; }
        .status.err { background: rgba(239,68,68,0.15); color: #ef4444; display: block; }
        .steps {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #333;
        }
        .steps h3 {
            font-size: 14px;
            color: #888;
            margin-bottom: 12px;
        }
        .steps ol {
            padding-left: 18px;
            color: #aaa;
            font-size: 13px;
            line-height: 1.8;
        }
        .steps li { margin-bottom: 4px; }
        .token-example {
            background: #0f0f23;
            padding: 10px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 11px;
            color: #667eea;
            word-break: break-all;
            margin-top: 8px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔥 VK Browser Bot</h1>
        <p class="subtitle">Безлимитный интернет через ВК</p>

        <form id="tokenForm">
            <label>Kate Mobile Token</label>
            <input type="text" id="token" placeholder="vk1.a.xxx... или полная ссылка" required>
            <p class="hint">Можно вставить полную ссылку из Kate Mobile</p>

            <button type="submit">🚀 Запустить бота</button>
        </form>

        <div id="status" class="status"></div>

        <div class="steps">
            <h3>📱 Как получить токен:</h3>
            <ol>
                <li>Открой Kate Mobile</li>
                <li>Настройки → Другое → Копировать ссылку для токена</li>
                <li>Вставь сюда полную ссылку</li>
            </ol>
            <div class="token-example">https://oauth.vk.com/blank.html#access_token=vk1.a.xxx...&expires_in=0&user_id=123</div>
        </div>
    </div>

    <script>
        document.getElementById('tokenForm').onsubmit = async function(e) {
            e.preventDefault();
            const token = document.getElementById('token').value.trim();
            const status = document.getElementById('status');

            status.className = 'status';
            status.style.display = 'block';
            status.textContent = '⏳ Проверяю токен...';

            try {
                const resp = await fetch('/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: token})
                });
                const data = await resp.json();

                if (data.ok) {
                    status.className = 'status ok';
                    status.innerHTML = '✅ Бот запущен!<br>👤 ' + data.name + ' (ID: ' + data.user_id + ')<br>💬 Напиши "помощь" в чат с самим собой в ВК';
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

            # Парсим из ссылки
            if "access_token=" in token:
                match = re.search(r'access_token=([^&\s]+)', token)
                if match:
                    token = match.group(1)

            # Проверяем токен
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

                # Сохраняем
                VK_TOKEN = token
                save_config({"token": token, "user_id": user_id})

                # Запускаем бота в фоне
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
        log.info(f"🌐 Веб-интерфейс: http://localhost:{port}")
        httpd.serve_forever()

# ============ ЗАПУСК ============

if __name__ == "__main__":
    log.info("🚀 VK Browser Bot запускается...")

    # Проверяем сохранённый конфиг
    cfg = load_config()
    if cfg.get("token"):
        VK_TOKEN = cfg["token"]
        log.info("[+] Токен загружен из конфига")

        # Проверяем и запускаем
        user_id = get_user_id()
        if user_id:
            def start_bot():
                listen_messages(user_id)
            bot_thread = threading.Thread(target=start_bot, daemon=True)
            bot_thread.start()

    # Запускаем веб-сервер (основной поток)
    start_web_server()
