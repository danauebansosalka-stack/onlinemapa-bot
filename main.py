

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

KEYWORD_PATTERNS = [
    ("КАБ", r"\bкаб\w*\b"),
    ("Веселянка", r"веселянк\w*"),
    ("Малокатеринівка", r"малокатеринівк\w*"),
    ("Кушугум", r"кушугум\w*"),
    ("Балабино", r"балабин\w*"),
    ("Кічкас", r"кічкас|кичкас|кічман|кичман"),
    ("Заводской", r"заводськ\w*|заводск\w*"),
    ("Шахед", r"\bшахед\w*\b"),
    ("БПЛА", r"\bбпла\b|беспилотник\w*"),
    ("FPV", r"\bfpv\b"),
    ("Баллистика", r"баллистик\w*"),
    ("РСЗО", r"\bрсзо\b"),
    ("Снаряд", r"снаряд\w*"),
]


def find_matches(text):
    normalized = text.casefold().replace("ё", "е")
    return [
        label
        for label, pattern in KEYWORD_PATTERNS
        if re.search(pattern, normalized, re.IGNORECASE)
    ]


async def fetch_posts():
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": "Mozilla/5.0"}

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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    subscribers.add(user.id)
    await message.reply_text(
        "Бот запущен.\n"
        "Отслеживаются ключевые слова в публичной ленте.\n"
        f"Интервал проверки: {POLL_SECONDS} секунд.\n"
        "Совпадение слов не подтверждает наличие угрозы."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "Бот работает, если сервис запущен.\n"
        f"Интервал проверки: {POLL_SECONDS} секунд.\n"
        f"Подписчиков в текущем запуске: {len(subscribers)}"
    )


async def monitor_channel(application):
    global initialized

    while True:
        try:
            posts = await fetch_posts()

            if not initialized:
                seen_ids.update(post_id for post_id, _ in posts)
                initialized = True
                logger.info("Начальная загрузка завершена.")
            else:
                for post_id, text in posts:
                    if post_id in seen_ids:
                        continue

                    seen_ids.add(post_id)
                    matches = find_matches(text)

                    if matches:
                        notification = (
                            "📢 Найдены ключевые слова\n\n"
                            f"Совпадения: {', '.join(matches)}\n\n"
                            f"Текст публикации:\n{text[:3000]}\n\n"
                            "Совпадение текста не подтверждает угрозу."
                        )

                        for user_id in list(subscribers):
                            try:
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=notification,
                                )
                            except Exception:
                                logger.exception(
                                    "Ошибка отправки уведомления."
                                )

            if len(seen_ids) > 1000:
                seen_ids.clear()
                seen_ids.update(post_id for post_id, _ in posts)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка чтения публичной ленты.")

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
            "Не задан BOT_TOKEN в Environment Variables."
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
