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

BOT_TOKEN = os.getenv("8489737407:AAFTBXVFTyLoOlgL_BKXvmLo2YkIvUAVLZs")

CHANNEL_URL = "https://t.me/s/eyes_everywhere_ua"

POLL_SECONDS = 30

logging.basicConfig(

    format="%(asctime)s | %(levelname)s | %(message)s",

    level=logging.INFO,

)

logger = logging.getLogger(__name__)

# Подписчики, нажавшие /start.

# Список хранится в памяти и может сброситься после перезапуска.

subscribers = set()

# ID уже обработанных публикаций.

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

        if re.search(pattern, normalized, flags=re.IGNORECASE)

    ]

async def fetch_posts():

    timeout = aiohttp.ClientTimeout(total=20)

    headers = {

        "User-Agent": (

            "Mozilla/5.0 (compatible; PublicFeedMonitor/1.0)"

        )

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

def make_alert(text, threat, areas):

    area_text = ", ".join(areas)

    return (

        "🔴 ПОВІТРЯНА ЗАГРОЗА\n\n"

        f"Тип: {threat}\n"

        f"Район, згаданий у публікації: {area_text}\n\n"

        "Напрямок автоматично не визначається.\n"

        "Звіряйтеся з офіційними оповіщеннями."

    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not update.effective_message:

        return

    subscribers.add(user.id)

    await update.effective_message.reply_text(

        "✅ Бот запущен.\n\n"

        "Проверяю публичную ленту канала на новые публикации.\n"

        "Уведомления отправляются, если в одной публикации "

        "есть тип угрозы и упоминание Кичкаса или Заводского.\n\n"

        "Проверка примерно каждые 30 секунд. "

        "Доставка и полнота уведомлений не гарантируются.\n\n"

        "Команда проверки: /status"

    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (

        "Статус: бот работает, если этот ответ получен.\n"

        f"Источник: {CHANNEL_URL}\n"

        f"Интервал проверки: {POLL_SECONDS} секунд.\n"

        f"Подписчиков в текущем запуске: {len(subscribers)}\n"

        "Публичная лента может быть недоступна."

    )

    if update.effective_message:

        await update.effective_message.reply_text(message)

async def send_alert(application, text):

    failed = []

    for user_id in list(subscribers):

        try:

            await application.bot.send_message(

                chat_id=user_id,

                text=text,

            )

        except Exception as error:

            logger.warning(

                "Не удалось отправить уведомление пользователю %s: %s",

                user_id,

                error,

            )

            failed.append(user_id)

    # Не удаляем подписчиков автоматически:

    # временная ошибка доставки не обязательно означает блокировку бота.

async def monitor_channel(application):

    global initialized

    while True:

        try:

            posts = await fetch_posts()

            if not posts:

                logger.info("В публичной ленте пока нет публикаций.")

                await asyncio.sleep(POLL_SECONDS)

                continue

            # При первом запуске запоминаем существующие публикации,

            # чтобы не рассылать старые сообщения.

            if not initialized:

                for post_id, _ in posts:

                    seen_ids.add(post_id)

                initialized = True

                logger.info(

                    "Инициализация завершена. Запомнено публикаций: %s",

                    len(seen_ids),

                )

                await asyncio.sleep(POLL_SECONDS)

                continue

            new_posts = [

                (post_id, text)

                for post_id, text in posts

                if post_id not in seen_ids

            ]

            # Публичная лента обычно возвращает старые сообщения

            # раньше новых, поэтому обрабатываем по порядку.

            for post_id, text in new_posts:

                seen_ids.add(post_id)

                threats = find_matches(text, THREAT_PATTERNS)

                areas = find_matches(text, AREA_PATTERNS)

                # Не объединяем разные публикации и не угадываем,

                # относятся ли они к одной цели.

                if threats and areas:

                    alert = make_alert(

                        text,

                        threats[0],

                        areas,

                    )

                    await send_alert(application, alert)

            # Ограничиваем размер памяти.

            if len(seen_ids) > 1000:

                seen_ids.clear()

                for post_id, _ in posts:

                    seen_ids.add(post_id)

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(

                "Ошибка проверки публичной ленты. "

                "Повторная попытка будет позже."

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

            "Не задан BOT_TOKEN. Добавьте его в Environment Variables."

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
