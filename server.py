import os
import time
import threading
import requests
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# === GLOBAL STATE ===
VK_TOKEN = ""
VK_API = "https://api.vk.com/method"
API_VERSION = "5.199"
OWNER_ID = None
LAST_MSG_ID = 0
BOT_RUNNING = False
BOT_THREAD = None

# === HTML PAGE для ввода токена ===
HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VK Bot</title>
<style>
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e;
    color: #eee;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    margin: 0;
    padding: 20px;
}
.container {
    background: #16213e;
    border-radius: 16px;
    padding: 32px;
    max-width: 480px;
    width: 100%;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
h1 { margin: 0 0 8px 0; font-size: 24px; color: #e94560; }
.sub { color: #888; font-size: 14px; margin-bottom: 24px; }
label { display: block; font-size: 12px; color: #aaa; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; }
input[type="text"] {
    width: 100%;
    padding: 12px 16px;
    border: 2px solid #0f3460;
    border-radius: 10px;
    background: #0a0a1a;
    color: #fff;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
}
input[type="text"]:focus { border-color: #e94560; }
input[type="text"]::placeholder { color: #555; }
.btn {
    width: 100%;
    padding: 14px;
    margin-top: 20px;
    border: none;
    border-radius: 10px;
    background: #e94560;
    color: #fff;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s, transform 0.1s;
}
.btn:hover { background: #ff6b81; }
.btn:active { transform: scale(0.98); }
.btn:disabled { background: #555; cursor: not-allowed; }
.status {
    margin-top: 20px;
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 14px;
    display: none;
}
.status.ok { background: #1a472a; color: #7ee787; display: block; }
.status.err { background: #4a1a1a; color: #ff7b7b; display: block; }
.status.info { background: #1a2a4a; color: #7eb8ff; display: block; }
.logs {
    margin-top: 16px;
    padding: 12px;
    background: #0a0a1a;
    border-radius: 10px;
    font-family: monospace;
    font-size: 12px;
    color: #aaa;
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
}
.hint {
    margin-top: 16px;
    font-size: 12px;
    color: #666;
    line-height: 1.5;
}
.hint a { color: #e94560; }
</style>
</head>
<body>
<div class="container">
<h1>🤖 VK Bot</h1>
<div class="sub">Бот для чтения сообщений VK через Railway</div>

<label>Access Token (Kate Mobile)</label>
<input type="text" id="token" placeholder="Вставь полную ссылку или токен...">

<button class="btn" id="startBtn" onclick="startBot()">▶️ Запустить бота</button>
<button class="btn" id="stopBtn" onclick="stopBot()" style="display:none; background:#555;">⏹ Остановить</button>

<div class="status" id="status"></div>
<div class="logs" id="logs" style="display:none;"></div>

<div class="hint">
<b>Как получить токен:</b><br>
1. Открой <a href="https://vkhost.github.io" target="_blank">vkhost.github.io</a><br>
2. Выбери <b>Kate Mobile</b><br>
3. Разреши доступ → скопируй <b>полную ссылку</b> из адресной строки<br>
4. Вставь сюда — я сам извлеку токен
</div>
</div>

<script>
let pollInterval = null;

function setStatus(msg, type) {
    const s = document.getElementById("status");
    s.textContent = msg;
    s.className = "status " + type;
}

function addLog(msg) {
    const l = document.getElementById("logs");
    l.style.display = "block";
    l.textContent += msg + "\n";
    l.scrollTop = l.scrollHeight;
}

async function startBot() {
    const token = document.getElementById("token").value.trim();
    if (!token) { setStatus("Введи токен!", "err"); return; }

    setStatus("Запускаю...", "info");
    document.getElementById("startBtn").disabled = true;

    const res = await fetch("/api/start", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({token: token})
    });
    const data = await res.json();

    if (data.ok) {
        setStatus("✅ Бот запущен! Пиши в VK — бот отвечает.", "ok");
        document.getElementById("startBtn").style.display = "none";
        document.getElementById("stopBtn").style.display = "block";
        addLog("Бот запущен. Ожидаю сообщения...");
        pollLogs();
    } else {
        setStatus("❌ " + data.error, "err");
        document.getElementById("startBtn").disabled = false;
    }
}

async function stopBot() {
    await fetch("/api/stop", {method: "POST"});
    setStatus("Бот остановлен.", "info");
    document.getElementById("startBtn").style.display = "block";
    document.getElementById("startBtn").disabled = false;
    document.getElementById("stopBtn").style.display = "none";
    if (pollInterval) clearInterval(pollInterval);
}

async function pollLogs() {
    pollInterval = setInterval(async () => {
        const res = await fetch("/api/logs");
        const data = await res.json();
        if (data.logs) {
            const l = document.getElementById("logs");
            l.textContent = data.logs;
            l.scrollTop = l.scrollHeight;
        }
    }, 2000);
}
</script>
</body>
</html>"""

LOGS = []

def log(msg):
    print(msg)
    LOGS.append(msg)
    if len(LOGS) > 200:
        LOGS.pop(0)

# === VK API ===
def vk(method, params=None):
    p = params or {}
    p["access_token"] = VK_TOKEN
    p["v"] = API_VERSION
    try:
        r = requests.get(f"{VK_API}/{method}", params=p, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def extract_token(raw):
    """Извлекает токен из полной ссылки или возвращает как есть"""
    raw = raw.strip()
    if "access_token=" in raw:
        start = raw.find("access_token=") + len("access_token=")
        end = raw.find("&", start)
        if end == -1:
            end = len(raw)
        return raw[start:end]
    return raw

def get_owner_id():
    global OWNER_ID
    res = vk("users.get")
    if "response" in res:
        OWNER_ID = res["response"][0]["id"]
        log(f"[BOT] Owner ID: {OWNER_ID}")
        return True
    else:
        log(f"[BOT] Error get owner: {res}")
        return False

def send_message(peer_id, text):
    res = vk("messages.send", {
        "peer_id": peer_id,
        "message": text,
        "random_id": int(time.time() * 1000)
    })
    log(f"[BOT] Sent to {peer_id}: {text}")
    return res

def process_messages():
    global LAST_MSG_ID
    res = vk("messages.getConversations", {"count": 20, "filter": "unread"})
    if "response" not in res:
        if "error" in res:
            log(f"[BOT] API error: {res['error']}")
        return

    items = res["response"]["items"]
    for conv in items:
        msg = conv["last_message"]
        msg_id = msg["id"]
        peer_id = msg["peer_id"]
        text = msg.get("text", "").lower().strip()
        from_id = msg["from_id"]

        if from_id == OWNER_ID:
            continue
        if msg_id <= LAST_MSG_ID:
            continue
        LAST_MSG_ID = max(LAST_MSG_ID, msg_id)

        log(f"[BOT] New msg from {from_id}: {text}")

        if "кот" in text or "кошк" in text or "cat" in text:
            send_message(peer_id, "🐱 Мяу! Вот тебе котик: https://cataas.com/cat")
        elif "привет" in text:
            send_message(peer_id, "👋 Привет! Я бот на Railway. Напиши \"котик\" — пришлю котика!")
        elif "погода" in text:
            send_message(peer_id, "☀️ Погода отличная, ведь у тебя теперь безлимит на VK! 😎")
        elif text:
            send_message(peer_id, f"📨 Ты написал: \"{msg.get('text', '')}\". Попробуй написать \"котик\" 😉")

def bot_loop():
    global BOT_RUNNING
    if not get_owner_id():
        BOT_RUNNING = False
        log("[BOT] Failed to start — invalid token?")
        return

    log("[BOT] Started polling...")
    while BOT_RUNNING:
        try:
            process_messages()
        except Exception as e:
            log(f"[BOT] Error: {e}")
        time.sleep(3)
    log("[BOT] Stopped.")

# === HTTP HANDLER ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

        elif path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"logs": "\n".join(LOGS[-100:])}).encode())

        elif path == "/api/status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"running": BOT_RUNNING}).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global VK_TOKEN, BOT_RUNNING, BOT_THREAD, LAST_MSG_ID
        parsed = urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode()

        if path == "/api/start":
            data = json.loads(body)
            raw_token = data.get("token", "")
            VK_TOKEN = extract_token(raw_token)

            if not VK_TOKEN:
                self._json({"ok": False, "error": "Токен не найден"})
                return

            # Тестируем токен
            test = vk("users.get")
            if "error" in test:
                self._json({"ok": False, "error": f"Невалидный токен: {test['error']}"})
                return

            BOT_RUNNING = True
            LAST_MSG_ID = 0
            LOGS.clear()
            BOT_THREAD = threading.Thread(target=bot_loop, daemon=True)
            BOT_THREAD.start()
            self._json({"ok": True})
            return

        elif path == "/api/stop":
            BOT_RUNNING = False
            self._json({"ok": True})
            return

        self.send_response(404)
        self.end_headers()

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass

def start_server():
    port = int(os.environ.get("PORT", 8080))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    log(f"[SERVER] Running on port {port}")
    srv.serve_forever()

if __name__ == "__main__":
    start_server()
