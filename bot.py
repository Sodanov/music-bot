import truststore

truststore.inject_into_ssl()  # доверяем системным сертификатам macOS/Windows
import soundcloud_search
import asyncio
import html
import logging
import secrets

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from flask import Flask
from threading import Thread
import os

web = Flask(__name__)

@web.route("/")
def home():
    return "Bot is alive"

def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

Thread(target=run_web, daemon=True).start()
from cachetools import TTLCache

import db
import providers
from admin import router as admin_router
from config import BOT_TOKEN, PAGE_SIZE, USER_COOLDOWN_SEC
from youtube_downloader import YouTubeDownloader

async def animate_status(status):
    dots = ["", ".", "..", "..."]
    i = 0

    while True:
        await asyncio.sleep(1)
        await status.edit_text(f"🔎 Ищу музыку{dots[i]}")
        i = (i + 1) % len(dots)

log = logging.getLogger(__name__)
router = Router()

# Максимальная длительность трека — 5 минут
MAX_DURATION_SEC = 5 * 60

# token -> (запрос, список треков); callback_data ограничена 64 байтами,
# поэтому результаты держим в памяти, а в кнопках — короткий токен
results_cache: TTLCache = TTLCache(maxsize=2000, ttl=3600)
cooldown: TTLCache = TTLCache(maxsize=10000, ttl=USER_COOLDOWN_SEC)


class StatsMiddleware(BaseMiddleware):
    """Записывает в базу каждого, кто пишет боту или нажимает кнопки (для /stats)."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            await db.touch_user(user.id, user.username, user.first_name)
        return await handler(event, data)


def fmt_duration(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def track_label(t, limit: int = 60) -> str:
    """Текст кнопки: длительность, название и исполнитель."""
    src = getattr(t, "source", None)
    mark = "☁️ " if src == "soundcloud" else ""
    prefix = f"{fmt_duration(t.duration)} | " if t.duration else ""
    text = f"{mark}{prefix}{t.title} — {t.artist}".strip(" —")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


async def send_ad(message: Message):
    """Реклама отдельным сообщением после выдачи. Сбой рекламы не должен ломать поиск."""
    try:
        ad = await db.get_ad()
        if not (ad["enabled"] and ad["text"]):
            return
        markup = None
        if ad["button_text"] and ad["url"]:
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=ad["button_text"], url=ad["url"])]
            ])
        await message.answer(f"📢 <i>Реклама</i>\n{html.escape(ad['text'])}", reply_markup=markup)
    except Exception:
        log.warning("Не удалось показать рекламу", exc_info=True)


async def render(token: str, page: int):
    query, tracks = results_cache[token]
    pages = max(1, -(-len(tracks) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = tracks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"🔎 {html.escape(query)}"]

    # каждый трек — отдельная кнопка: «время | название — исполнитель»
    rows = [
        [InlineKeyboardButton(text=track_label(t), callback_data=f"t:{token}:{page * PAGE_SIZE + i}")]
        for i, t in enumerate(chunk)
    ]

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="‹", callback_data=f"p:{token}:{max(page - 1, 0)}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton(text="›", callback_data=f"p:{token}:{min(page + 1, pages - 1)}"),
        ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def do_search(
    message: Message,
    query: str,
    session: aiohttp.ClientSession,
):
    uid = message.from_user.id
    if uid in cooldown:
        return await message.reply("Не так быстро 🙂")

    query = query.strip()
    if len(query) < 2:
        return await message.reply(
            "Напишите название трека, например: "
            "<code>Imagine Dragons Believer</code>"
        )

    # Кулдаун включаем только когда запрос принят.
    cooldown[uid] = True
    status = await message.reply("🔎 Ищу музыку...")
    q = query[:100]
    await db.log_event(uid, "search", query=q)

    animation = asyncio.create_task(animate_status(status))

    try:
        # SoundCloud и остальные каталоги ищем одновременно.
        catalog, soundcloud = await asyncio.gather(
            providers.search_all(session, q),
            soundcloud_search.search(q, limit=20),
            return_exceptions=True,
        )
    finally:
        animation.cancel()
        try:
            await animation
        except asyncio.CancelledError:
            pass

    tracks = []


    if isinstance(catalog, Exception):
        log.warning("Ошибка поиска в каталогах: %s", catalog)
    else:
        tracks.extend(
            t for t in catalog
            if not t.duration or t.duration <= MAX_DURATION_SEC
        )

    if isinstance(soundcloud, Exception):
        log.warning("Ошибка поиска в SoundCloud: %s", soundcloud)
    else:
        from types import SimpleNamespace

        sc_tracks = [
            SimpleNamespace(
                title=item["title"],
                artist=item["artist"],
                duration=item["duration_sec"],
                url=item["url"],
                source="soundcloud",
            )
            for item in soundcloud
        ]
        sc_tracks = [
            t for t in sc_tracks
            if 60 <= t.duration <= MAX_DURATION_SEC
        ]
        # Сначала показываем до 10 результатов SoundCloud.
        tracks = sc_tracks[:10] + tracks

    if not tracks:
        return await status.edit_text(
            "Ничего не нашёл 😔 Попробуйте другой запрос."
        )

    token = secrets.token_urlsafe(5)
    results_cache[token] = (query, tracks)
    text, markup = await render(token, 0)
    await status.edit_text(text, reply_markup=markup)

    # Реклама отправляется один раз после выдачи.
    await send_ad(message)


# ---------- команды и сообщения ----------

@router.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(
        "🎵 <b>Музыкальный бот</b>\n\n"
        "В личных сообщениях просто напишите название трека или исполнителя.\n"
        "В группах используйте команду: <code>/music название</code>\n\n"
        "Ищу музыку в SoundCloud и других доступных каталогах."
    )


@router.message(Command("music", "search", "m"))
async def cmd_music(m: Message, command: CommandObject, session: aiohttp.ClientSession):
    if not command.args:
        return await m.reply("Использование: <code>/music название трека</code>")
    await do_search(m, command.args, session)


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"), StateFilter(None))
async def private_text(m: Message, session: aiohttp.ClientSession):
    await do_search(m, m.text, session)


# ---------- кнопки ----------

@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def on_page(cb: CallbackQuery):
    _, token, page = cb.data.split(":")
    if token not in results_cache:
        return await cb.answer("Результаты устарели — повторите поиск", show_alert=True)
    text, markup = await render(token, int(page))
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("t:"))
async def on_track(
    cb: CallbackQuery,
    bot: Bot,
    session: aiohttp.ClientSession,
):
    _, token, idx = cb.data.split(":")
    entry = results_cache.get(token)
    if not entry:
        return await cb.answer("Результаты устарели — повторите поиск", show_alert=True)
    track = entry[1][int(idx)]
    await cb.answer("⏳ Загружаю…")

    chat_id = cb.message.chat.id

    kwargs = dict(title=track.title[:64], performer=track.artist[:64])
    if track.duration:
        kwargs["duration"] = track.duration

    # SoundCloud скачиваем через yt-dlp, но поиск YouTube отключён.
    if getattr(track, "source", None) == "soundcloud":
        await bot.send_chat_action(chat_id, "upload_voice")
        data = await soundcloud_downloader.download_track(track.url)
        if not data:
            return await bot.send_message(
                chat_id,
                "Не удалось скачать этот трек с SoundCloud 😔 Попробуйте другой."
            )
        filename = f"{track.artist} - {track.title}.mp3"
        await bot.send_audio(
            chat_id, BufferedInputFile(data, filename=filename), **kwargs
        )
        await db.log_event(cb.from_user.id, "download", source="soundcloud")
        return

    # --- трек из каталога ---
    url = await providers.resolve_url(session, track)
    if not url:
        return await bot.send_message(chat_id, "Не удалось найти аудиофайл для этого трека 😔")

    try:
        await bot.send_audio(chat_id, url, **kwargs)
    except TelegramBadRequest:
        # Telegram не смог забрать файл по ссылке — скачиваем и загружаем сами
        data = await providers.download(session, url)
        if not data:
            return await bot.send_message(chat_id, "Файл недоступен или слишком большой (лимит 50 МБ).")
        filename = f"{track.artist} - {track.title}.mp3"
        await bot.send_audio(chat_id, BufferedInputFile(data, filename=filename), **kwargs)

    await db.log_event(cb.from_user.id, "download", source="catalog")


async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init()

    # Используется только для скачивания найденных треков SoundCloud.
    # Поиск YouTube из бота удалён.
    global soundcloud_downloader
    soundcloud_downloader = YouTubeDownloader()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # учёт пользователей: срабатывает на все сообщения и нажатия кнопок
    stats_mw = StatsMiddleware()
    dp.message.outer_middleware(stats_mw)
    dp.callback_query.outer_middleware(stats_mw)

    dp.include_router(admin_router)  # админка первой, чтобы FSM-состояния не перехватывались поиском
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="music", description="Найти трек: /music название"),
        BotCommand(command="help", description="Помощь"),
    ])

    async with aiohttp.ClientSession(headers={"User-Agent": "MusicSearchBot/0.2"}) as session:
        await dp.start_polling(bot, session=session)


if __name__ == "__main__":
    asyncio.run(main())
