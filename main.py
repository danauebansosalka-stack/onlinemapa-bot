
import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from aiohttp import web
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNELS = [
    "eyes_everywhere_ua",
    "monitorsmy",
]

CHECK_INTERVAL = 30
PORT = int(os.environ.get("PORT", "10000"))
STATE_FILE = Path("bot_state.json")
MAX_SEEN_POSTS = 2000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

subscribers = set()
seen_posts = set()
http_session = None
monitor_task = None
web_runner = None
application = None
started_at = None


TARGET_PATTERNS = [
    ("УМПБ-5", r"УМПБ[\s\-‐‑‒–—]*5"),
    (
        "реактивний «Шахед»",
        r"(?:реактивн\w*.{0,30}(?:шахед|герань)|"
        r"(?:шахед|герань).{0,30}реактивн\w*)",
    ),
    ("«Шахед»", r"шахед\w*|shahed\w*"),
    ("БпЛА", r"\bБпЛА\b|безпілотник\w*|беспилотник\w*"),
    ("КАБ", r"\bКАБ\w*\b"),
    ("FPV", r"\bFPV\b"),
    ("балістика", r"балістик\w*|баллистик\w*"),
    ("РСЗВ", r"\bРСЗВ\b|\bРСЗО\b"),
    ("ракета", r"ракет\w*"),
    ("снаряд", r"снаряд\w*"),
    ("«Бандероль»", r"бандерол\w*"),
    ("«Молнія»", r"молні\w*|молни\w*"),
]

COMPILED_TARGETS = [
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in TARGET_PATTERNS
]

TARGET_WORD_PATTERN = re.compile(
    r"\bціл(?:ь|і|ей|ями|ях|лю)\b",
    re.IGNORECASE,
)

# Це лише перевірка явної фрази в тексті.
# Вона не визначає справжню траєкторію.
DIRECTION_PATTERN = re.compile(
    r"(?:"
    r"(?:у|в)\s+напрям(?:ку|ок)\s+(?:на\s+)?запоріжжя"
    r"|(?:у|в)\s+бік\s+запоріжжя"
    r"|курс\s+на\s+запоріжжя"
    r"|на\s+запоріжжя"
    r")",
    re.IGNORECASE,
)


def load_state():
    global subscribers, seen_posts

    try:
        if STATE_FILE.exists():
            data = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
            subscribers = set(data.get("subscribers", []))
            seen_posts = set(data.get("seen_posts", []))
    except Exception:
        logger.exception("Не вдалося завантажити стан")


def save_state():
    try:
        data = {
            "subscribers": list(subscribers),
            "seen_posts": list(seen_posts)[-MAX_SEEN_POSTS:],
        }
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Не вдалося зберегти стан")


def detect_target(text):
    for name, pattern in COMPILED_TARGETS:
        if pattern.search(text):
            return name

    if TARGET_WORD_PATTERN.search(text):
        return "невизначена ціль"

    return None


def make_alert(text):
    # Не надсилаємо сповіщення без явної фрази
    # про напрямок на Запоріжжя.
    if not DIRECTION_PATTERN.search(text):
        return None

    target = detect_target(text)
    if not target:
        return None

    if target == "УМПБ-5":
        headline = (
            "⚠️ У повідомленні згадується можливе "
            "застосування УМПБ-5 (реактивних КАБ)."
        )
    elif target == "невизначена ціль":
        headline = (
            "⚠️ У повідомленні згадується ціль, "
            "але її тип не визначено."
        )
    else:
        headline = f"⚠️ У повідомленні згадується: {target}."

    return (
        "⚡ ЗАПОРІЖЖЯ | СИГНАЛ\n\n"
        f"{headline}\n"
        "📍 У тексті згадується напрямок на Запоріжжя.\n\n"
        "⚠️ Це автоматичний збіг тексту, а не підтвердження "
        "реальної траєкторії чи загрози.\n"
        "📢 Перевіряйте офіційні сповіщення та "
        "дотримуйтеся вказівок щодо укриття."
    )


async def fetch_channel(channel):
    url = f"https://t.me/s/{channel}"

    try:
        async with http_session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                logger.warning(
                    "Канал %s: HTTP %s",
                    channel,
                    response.status,
                )
                return []

            html = await response.text()

    except Exception:
        logger.exception("Помилка читання каналу %s", channel)
        return []

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for wrap in soup.select(".tgme_widget_message_wrap"):
        message = wrap.select_one(".tgme_widget_message")
        text_node = wrap.select_one(".tgme_widget_message_text")

        if not message or not text_node:
            continue

        post_id = message.get("data-post")
        text = text_node.get_text(" ", strip=True)

        if post_id and text:
            posts.append((post_id, text))

    return posts


async def notify_subscribers(message):
    for chat_id in list(subscribers):
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
            )
        except Exception:
            logger.exception(
                "Помилка надсилання користувачу %s",
                chat_id,
            )

        await asyncio.sleep(0.05)


async def monitor_loop():
    first_run = not seen_posts

    while True:
        try:
            for channel in CHANNELS:
                posts = await fetch_channel(channel)

                for post_id, text in posts:
                    unique_id = f"{channel}:{post_id}"

                    if unique_id in seen_posts:
                        continue

                    seen_posts.add(unique_id)

                    if not first_run:
                        alert = make_alert(text)
                        if alert:
                            await notify_subscribers(alert)

                if len(seen_posts) > MAX_SEEN_POSTS:
                    seen_posts_copy = list(seen_posts)
                    seen_posts.clear()
                    seen_posts.update(
                        seen_posts_copy[-MAX_SEEN_POSTS:]
                    )

                save_state()

            first_run = False

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Помилка циклу моніторингу")

        await asyncio.sleep(CHECK_INTERVAL)


async def home(request):
    return web.Response(
        text="ЗАПОРІЖЖЯ | СИГНАЛ — HTTP OK",
        content_type="text/plain",
    )


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "ЗАПОРІЖЖЯ | СИГНАЛ",
        "monitor_interval_seconds": CHECK_INTERVAL,
    })


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    subscribers.add(update.effective_chat.id)
    save_state()

    await update.effective_message.reply_text(
        "⚡ ЗАПОРІЖЖЯ | СИГНАЛ\n\n"
        "🟢 Сповіщення увімкнено.\n"
        "📡 Моніторинг відкритих джерел.\n"
        "🔄 Перевірка кожні 30 секунд.\n\n"
        "/start — увімкнути сповіщення\n"
        "/stop — вимкнути сповіщення\n"
        "/status — стан бота\n\n"
        "⚠️ Бот не є офіційним джерелом. "
        "Автоматичні збіги можуть бути помилковими.\n"
        "🇺🇦 Бережіть себе!"
    )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    subscribers.discard(update.effective_chat.id)
    save_state()

    await update.effective_message.reply_text(
        "🔕 Сповіщення вимкнено.\n"
        "Для повторного ввімкнення надішліть /start."
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "⚡ ЗАПОРІЖЖЯ | СИГНАЛ\n\n"
        "🟢 Бот працює.\n"
        "🌐 HTTP-сервер увімкнено.\n"
        f"🔄 Інтервал перевірки: {CHECK_INTERVAL} секунд.\n"
        f"📡 Джерел у списку: {len(CHANNELS)}.\n\n"
        "⚠️ Статус бота не підтверджує наявність "
        "або відсутність загрози.\n"
        "Перевіряйте офіційні сповіщення."
    )


async def post_init(app: Application):
    global http_session, monitor_task, web_runner, application

    application = app
    load_state()

    http_session = aiohttp.ClientSession(
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        }
    )

    http_app = web.Application()
    http_app.router.add_get("/", home)
    http_app.router.add_get("/health", health)

    web_runner = web.AppRunner(http_app)
    await web_runner.setup()

    site = web.TCPSite(web_runner, "0.0.0.0", PORT)
    await site.start()

    monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Бот та HTTP-сервер запущені на порту %s", PORT)


async def post_shutdown(app: Application):
    global http_session, monitor_task, web_runner

    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    if http_session:
        await http_session.close()

    if web_runner:
        await web_runner.cleanup()

    save_state()
    logger.info("Бот та HTTP-сервер зупинені")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задано BOT_TOKEN у змінних середовища Render."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

