
import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNELS = [
    "eyes_everywhere_ua",
    "monitorsmy",
]

CHECK_INTERVAL = 30
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
application = None


# Шукаємо лише явне текстове зазначення напрямку.
# Це НЕ перевіряє справжню траєкторію цілі.
DIRECTION_PATTERN = re.compile(
    r"(?:"
    r"(?:у|в)\s+напрям(?:ку|ок)\s+(?:на\s+)?запоріжжя"
    r"|(?:у|в)\s+бік\s+запоріжжя"
    r"|курс\s+на\s+запоріжжя"
    r"|на\s+запоріжжя"
    r")",
    re.IGNORECASE,
)

TARGET_PATTERNS = [
    (
        "УМПБ-5",
        re.compile(r"УМПБ[\s\-‐‑‒–—]*5", re.IGNORECASE),
    ),
    (
        "реактивний «Шахед»",
        re.compile(
            r"(?:реактивн\w*.{0,30}(?:шахед|герань)"
            r"|(?:шахед|герань).{0,30}реактивн\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "«Шахед»",
        re.compile(r"шахед\w*|shahed\w*", re.IGNORECASE),
    ),
    (
        "БпЛА",
        re.compile(
            r"\bБпЛА\b|безпілотник\w*|беспилотник\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "КАБ",
        re.compile(r"\bКАБ\w*\b", re.IGNORECASE),
    ),
    (
        "FPV",
        re.compile(r"\bFPV\b", re.IGNORECASE),
    ),
    (
        "балістика",
        re.compile(r"балістик\w*|баллистик\w*", re.IGNORECASE),
    ),
    (
        "РСЗВ",
        re.compile(r"\bРСЗВ\b|\bРСЗО\b", re.IGNORECASE),
    ),
    (
        "ракета",
        re.compile(r"ракет\w*", re.IGNORECASE),
    ),
    (
        "снаряд",
        re.compile(r"снаряд\w*", re.IGNORECASE),
    ),
    (
        "«Бандероль»",
        re.compile(r"бандерол\w*", re.IGNORECASE),
    ),
    (
        "«Молнія»",
        re.compile(r"молні\w*|молни\w*", re.IGNORECASE),
    ),
]

TARGET_WORD_PATTERN = re.compile(
    r"\bціл(?:ь|і|ей|ями|ях|лю)\b",
    re.IGNORECASE,
)


def detect_target(text):
    """Визначає згаданий тип за текстом, а не підтверджену загрозу."""
    for name, pattern in TARGET_PATTERNS:
        if pattern.search(text):
            return name

    if TARGET_WORD_PATTERN.search(text):
        return "невизначена ціль"

    return None


def direction_is_explicit(text):
    """Не вгадує напрямок за назвами інших населених пунктів."""
    return bool(DIRECTION_PATTERN.search(text))


def make_alert(text):
    # Спочатку потрібне явне текстове зазначення напрямку.
    if not direction_is_explicit(text):
        return None

    target = detect_target(text)
    if not target:
        return None

    if target == "УМПБ-5":
        headline = (
            "⚠️ У повідомленні згадується УМПБ-5 "
            "(можливе застосування реактивних КАБ)."
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
        "📍 У тексті повідомлення зазначено напрямок на Запоріжжя.\n\n"
        "⚠️ Автоматичний збіг тексту не підтверджує "
        "реальну траєкторію чи наявність загрози.\n"
        "📢 Перевіряйте офіційні сповіщення та "
        "дотримуйтеся вказівок щодо укриття."
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
                "Не вдалося надіслати повідомлення: %s",
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

                    # Позначаємо допис як переглянутий,
                    # навіть якщо він не пройшов фільтр.
                    seen_posts.add(unique_id)

                    if not first_run:
                        alert = make_alert(text)
                        if alert:
                            await notify_subscribers(alert)

                # Обмежуємо пам'ять про переглянуті дописи.
                seen_posts_list = list(seen_posts)
                if len(seen_posts_list) > MAX_SEEN_POSTS:
                    seen_posts.clear()
                    seen_posts.update(
                        seen_posts_list[-MAX_SEEN_POSTS:]
                    )

                save_state()

            first_run = False

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Помилка моніторингу")

        await asyncio.sleep(CHECK_INTERVAL)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    save_state()

    await update.effective_message.reply_text(
        "⚡ ЗАПОРІЖЖЯ | СИГНАЛ\n\n"
        "🟢 Сповіщення увімкнено.\n"
        "📡 Перевірка відкритих джерел кожні 30 секунд.\n\n"
        "/start — увімкнути сповіщення\n"
        "/stop — вимкнути сповіщення\n"
        "/status — стан бота\n\n"
        "⚠️ Бот не є офіційним джерелом. "
        "Автоматичні збіги можуть бути помилковими.\n"
        "🇺🇦 Бережіть себе!"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    save_state()

    await update.effective_message.reply_text(
        "🔕 Сповіщення вимкнено.\n"
        "Для повторного ввімкнення надішліть /start."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚡ ЗАПОРІЖЖЯ | СИГНАЛ\n\n"
        "🟢 Бот працює.\n"
        "🔄 Інтервал перевірки: 30 секунд.\n"
        f"📡 Джерел у списку: {len(CHANNELS)}.\n\n"
        "ℹ️ Робота бота не підтверджує наявність "
        "або відсутність загрози.\n"
        "Перевіряйте офіційні сповіщення."
    )


async def post_init(app: Application):
    global http_session, monitor_task, application

    application = app
    load_state()

    http_session = aiohttp.ClientSession(
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        }
    )

    monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Бот запущено")


async def post_shutdown(app: Application):
    global http_session, monitor_task

    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    if http_session:
        await http_session.close()

    save_state()
    logger.info("Бот зупинено")


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
