
import os
import re
import asyncio
import logging
import aiohttp

from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_URL = "https://t.me/s/eyes_everywhere_ua"
POLL_SECONDS = 30

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

subscribers = set()
seen_ids = set()
initialized = False

THREAT_PATTERNS = [
    ("Реактивный шахед", r"реактивн\w*\s+шахед\w*"),
    ("Шахед", r"\bшахед\w*\b|\bshahed\w*\b"),
    ("БПЛА", r"\bбпла\b|\bбеспилотник\w*\b"),
    ("FPV", r"\bfpv\b"),
    ("КАБ", r"\bкаб(?:ы|ов|ами)?\b"),
    ("Баллистика", r"\bбаллистик\w*\b"),
    ("РСЗО", r"\bрсзо\b"),
    ("Снаряд", r"\bснаряд\w*\b"),
]

AREA_PATTERNS = [
    ("Кічкас", r"кичкас|кичман|кічкас|кічман"),
    ("Заводской", r"заводск\w*|заводськ\w*|\bзаводи\b"),
]


def find_matches(text, patterns):
    normalized = text.casefold().replace("ё", "е")
    return [
        label
        for label, pattern in patterns
        if re.search(pattern, normalized, re.IGNORECASE)
    ]


async def fetch_posts():
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PublicFeedMonitor/1.0)"
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(CHANNEL_URL) as response:
            response.raise_for_status()
            html = await response.text()

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for item in soup.select(".tgme_widget_message"):
        post_id = item.get("data-post")
        text_element = item.select_one(
            ".tgme_widget_message_text"
        )

        if not post_id or not text_element:
            continue

        text = text_element.get_text(" ", strip=True)
        if text:
            posts.append((post_id, text))

    return posts


def make_alert(threat, areas, post_id):
    area_text = ", ".join(areas)
    post_url = f"https://t.me/{post_id}"

    return (
        "🔴 УВЕДОМЛЕНИЕ О ВОЗДУШНОЙ УГРОЗЕ\n\n"
        f"Тип: {threat}\n"
        f"Упомянутый район: {area_text}\n\n"
        f"Источник: {post_url}\n\n"
        "Совпадение найдено в тексте одной публикации.\n"
        "Направление и реальное местоположение цели "
        "не определяются.\n"
        "Сверяйтесь с официальными оповещениями."
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    subscribers.add(user.id)
    await message.reply_text(
        "✅ Бот запущен.\n\n"
        "Источник: публичная лента канала.\n"
        f"Проверка примерно каждые {POLL_SECONDS} секунд.\n"
        "Уведомление отправляется, если в одной публикации "
        "есть тип угрозы и упоминание Кичкаса или Заводского.\n\n"
        "Команда проверки: /status\n"
        "Полнота и своевременность уведомлений не гарантируются."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "Статус: бот отвечает на команды.\n"
        f"Источник: {CHANNEL_URL}\n"
        f"Интервал проверки: {POLL_SECONDS} секунд.\n"
        f"Подписчиков в текущем запуске: {len(subscribers)}\n"
        "Публичная лента может быть недоступна."
    )


async def send_alert(application, text):
    for user_id in list(subscribers):
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=text,
            )
        except Exception:
            logger.exception(
                "Не удалось отправить уведомление пользователю %s",
                user_id,
            )


async def monitor_channel(application):
    global initialized

    while True:
        try:
            posts = await fetch_posts()

            if not posts:
                logger.info("В публичной ленте нет публикаций.")
                await asyncio.sleep(POLL_SECONDS)
                continue

            if not initialized:
                for post_id, _ in posts:
                    seen_ids.add(post_id)

                initialized = True
                logger.info(
                    "Начальная загрузка завершена. Публикаций: %s",
                    len(seen_ids),
                )
                await asyncio.sleep(POLL_SECONDS)
                continue

            new_posts = [
                (post_id, text)
                for post_id, text in posts
                if post_id not in seen_ids
            ]

            for post_id, text in new_posts:
                seen_ids.add(post_id)
                threats = find_matches(text, THREAT_PATTERNS)
                areas = find_matches(text, AREA_PATTERNS)

                if threats and areas:
                    await send_alert(
                        application,
                        make_alert(threats[0], areas, post_id),
                    )

            if len(seen_ids) > 1000:
                seen_ids.clear()
                for post_id, _ in posts:
                    seen_ids.add(post_id)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Ошибка проверки ленты. Повторная попытка позже."
            )

        await asyncio.sleep(POLL_SECONDS)


async def post_init(application):
    application.bot_data["monitor_task"] = asyncio.create_task(
        monitor_channel(application)
    )


async def post_shutdown(application):
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Добавьте токен в Environment Variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))

    logger.info("Запуск Telegram-бота.")
    application.run_polling()


if __name__ == "__main__":
    main()
