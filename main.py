import asyncio
import logging
import sys
import os
import math
import html
import yt_dlp
import aiosqlite
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web
from ytmusicapi import YTMusic
from aiogram.exceptions import TelegramNetworkError
from cachetools import TTLCache

# Загрузка переменных окружения
load_dotenv()

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Список каналов для обязательной подписки
REQUIRED_CHANNELS = [
    {"id": "@xSp1der42", "url": "https://t.me/xSp1der42", "name": "🕷 Канал xSp1der42"},
    {"id": "@RiffyOff", "url": "https://t.me/RiffyOff", "name": "🎸 Канал RiffyOff"},
    {"id": "@neon9_news", "url": "https://t.me/neon9_news", "name": "📰 Канал Neon9 News"}
]

DB_NAME = "music_db.sqlite"
PROXY = None 
SEARCH_LIMIT = 100  
MAX_DURATION = 900  

MAX_CONCURRENT_DOWNLOADS = 3
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Пути
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

if BASE_DIR not in os.environ["PATH"]:
    os.environ["PATH"] += os.pathsep + BASE_DIR

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

ytmusic = YTMusic()
bot_username = ""  # Закешируем юзернейм бота для ускорения ответов

USERS_DATA = TTLCache(maxsize=10000, ttl=3600)

# ==========================================
# БАЗА ДАННЫХ
# ==========================================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                video_id TEXT PRIMARY KEY, title TEXT, artist TEXT, telegram_file_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, artist TEXT, date TEXT
            )
        """)
        await db.commit()

async def get_cached_track(video_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT title, artist, telegram_file_id FROM tracks WHERE video_id = ?", (video_id,)) as cursor:
            return await cursor.fetchone()

async def cache_track(video_id, title, artist, telegram_file_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO tracks (video_id, title, artist, telegram_file_id) VALUES (?, ?, ?, ?)", 
                         (video_id, title, artist, telegram_file_id))
        await db.commit()

async def register_user(user_id, username):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            if await cursor.fetchone() is None:
                date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await db.execute("INSERT INTO users (user_id, username, first_seen) VALUES (?, ?, ?)", (user_id, username, date_now))
                await db.commit()

async def log_download(user_id, title, artist):
    async with aiosqlite.connect(DB_NAME) as db:
        date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("INSERT INTO history (user_id, title, artist, date) VALUES (?, ?, ?, ?)", (user_id, title, artist, date_now))
        await db.commit()

async def get_full_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM tracks") as c: cached_tracks = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM history") as c: total_downloads = (await c.fetchone())[0]
        async with db.execute("SELECT username, first_seen FROM users ORDER BY rowid DESC LIMIT 5") as c: last_users = await c.fetchall()
        async with db.execute("SELECT artist, title, COUNT(*) as cnt FROM history GROUP BY artist, title ORDER BY cnt DESC LIMIT 5") as c:
            top_tracks = await c.fetchall()
    return {"users": total_users, "cache": cached_tracks, "downloads": total_downloads, "last_users": last_users, "top_tracks": top_tracks}

def safe_html(text: str) -> str:
    return html.escape(str(text))

def safe_remove_file(filepath: str):
    try:
        if filepath and os.path.exists(filepath): os.remove(filepath)
    except Exception as e: logger.error(f"Failed to remove file {filepath}: {e}")

# ==========================================
# ПОИСК И СКАЧИВАНИЕ
# ==========================================

async def search_music(query: str, mode: str):
    loop = asyncio.get_event_loop()
    try:
        search_filter = mode if mode in ['songs', 'videos'] else None
        results = await loop.run_in_executor(None, lambda: ytmusic.search(query, filter=search_filter, limit=SEARCH_LIMIT))
        parsed_results = []
        for track in results:
            if 'videoId' not in track: continue
            title = f"[Video] {track.get('title', 'Unknown')}" if track.get('resultType', '') == 'video' else track.get('title', 'Unknown')
            artist_names = ", ".join([a['name'] for a in track.get('artists', [])]) if track.get('artists', []) else "Unknown"
            parsed_results.append({'id': track['videoId'], 'title': title, 'artist': artist_names, 'duration': track.get('duration', '')})
        return parsed_results
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

async def get_track_info(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        track = await loop.run_in_executor(None, lambda: ytmusic.get_song(video_id))
        return track['videoDetails']['title'], track['videoDetails']['author']
    except: return "Unknown Track", "Unknown Artist"

def _run_yt_dlp_audio(opts, url):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
        return True
    except yt_dlp.utils.DownloadError as e:
        if 'слишком длинное' in str(e).lower(): return 'TOO_LONG'
        raise e

async def download_track_local(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{video_id}")
    ydl_opts = {
        'format': 'bestaudio/best', 'outtmpl': out_tmpl + '.%(ext)s', 'ffmpeg_location': BASE_DIR,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'match_filter': lambda info, *_, **__: 'слишком длинное' if (info.get('duration') or 0) > MAX_DURATION else None,
        'extractor_args': {'youtube': {'player_client': ['android']}},  # Обход ошибки 403 от YouTube
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True, 'geo_bypass': True,
        'source_address': '0.0.0.0', 'socket_timeout': 15, 'retries': 5, 'fragment_retries': 5,
    }
    if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _run_yt_dlp_audio(ydl_opts, url))
        if result == 'TOO_LONG': return 'TOO_LONG'
        final_path = out_tmpl + ".mp3"
        return final_path if os.path.exists(final_path) else None
    except Exception as e:
        logger.error(f"Audio DL Error: {e}")
        return None

async def handle_video_url(message: types.Message, url: str):
    uid = message.from_user.id
    msg = await message.answer("⏳ <b>Анализирую ссылку и добавляю в очередь...</b>", parse_mode="HTML")
    async with download_semaphore:
        await msg.edit_text("⬇️ <b>Скачиваю видео (стабильное качество)...</b>", parse_mode="HTML")
        timestamp = int(datetime.now().timestamp())
        filename_base = os.path.join(DOWNLOAD_DIR, f"vid_{uid}_{timestamp}")
        file_path, title = None, "Video"
        loop = asyncio.get_event_loop()
        try:
            ydl_opts = {
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]/best', 'outtmpl': f'{filename_base}.%(ext)s',
                'ffmpeg_location': BASE_DIR, 'merge_output_format': 'mp4', 'quiet': True, 'no_warnings': True, 
                'nocheckcertificate': True, 'geo_bypass': True,
                'match_filter': lambda info, *_, **__: 'слишком длинное' if (info.get('duration') or 0) > MAX_DURATION else None,
                'extractor_args': {'youtube': {'player_client': ['android']}},
            }
            if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE
            def _dl_video():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        # Всегда возвращаем .mp4, так как включен merge_output_format
                        return f"{filename_base}.mp4", info.get('title', 'Video')
                except yt_dlp.utils.DownloadError as e:
                    if 'слишком длинное' in str(e).lower(): return 'TOO_LONG', ''
                    raise e
            file_path, title = await loop.run_in_executor(None, _dl_video)
            if file_path == 'TOO_LONG': return await msg.edit_text("❌ <b>Видео слишком длинное!</b> (до 15 минут).", parse_mode="HTML")
        except Exception as e:
            await msg.edit_text("❌ <b>Ошибка скачивания.</b> Возможно, видео удалено или защищено.", parse_mode="HTML")
            safe_remove_file(f"{filename_base}.mp4")
            return

        try:
            if file_path and os.path.exists(file_path):
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if file_size_mb == 0: await msg.edit_text("❌ <b>Ошибка: Скачанный файл пуст.</b>", parse_mode="HTML")
                elif file_size_mb > 49.5: await msg.edit_text(f"❌ <b>Видео слишком большое ({file_size_mb:.1f} МБ)!</b> Лимит - 50 МБ.", parse_mode="HTML")
                else:
                    await msg.edit_text("📤 <b>Отправляю видео в Telegram...</b>", parse_mode="HTML")
                    await message.answer_video(video=FSInputFile(file_path), caption=f"🎬 <b>{safe_html(title)}</b>\n🤖 @{bot_username}", parse_mode="HTML", request_timeout=300)
                    await msg.delete()
            else: await msg.edit_text("❌ <b>Не удалось скачать видео.</b>", parse_mode="HTML")
        except Exception as e: await msg.edit_text("❌ <b>Ошибка отправки.</b> Сервер Telegram отклонил файл.", parse_mode="HTML")
        finally: safe_remove_file(file_path)

# ==========================================
# ПРОВЕРКА ПОДПИСКИ И КЛАВИАТУРЫ
# ==========================================

async def check_subscription(user_id: int) -> bool:
    try:
        for channel in REQUIRED_CHANNELS:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if member.status not in ["creator", "administrator", "member"]: return False
        return True
    except Exception: return False

def get_sub_keyboard():
    builder = InlineKeyboardBuilder()
    for channel in REQUIRED_CHANNELS: builder.button(text=f"🔗 {channel['name']}", url=channel["url"])
    builder.button(text="✅ Проверить подписку", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

session = AiohttpSession(timeout=3600, proxy=PROXY)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

def get_category_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎵 Официальные треки (Студийные)", callback_data="cat_songs")
    builder.button(text="🎧 Ремиксы / Bass / Клипы", callback_data="cat_videos")
    builder.button(text="🌎 Искать ВЕЗДЕ (Глобальный поиск)", callback_data="cat_general")
    builder.adjust(1)
    return builder.as_markup()

def get_results_keyboard(results, page: int):
    builder = InlineKeyboardBuilder()
    ITEMS_PER_PAGE = 5
    start, end = page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE
    items = results[start:end]
    for track in items:
        dur_text = f" ({track.get('duration')})" if track.get('duration') else ""
        text = f"{track['artist']} — {track['title']}{dur_text}"
        builder.button(text=text[:60] + ("..." if len(text) > 60 else ""), callback_data=f"dl_{track['id']}")
    builder.adjust(1)
    row = []
    total_pages = math.ceil(len(results) / ITEMS_PER_PAGE)
    row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}") if page > 0 else InlineKeyboardButton(text="✖️", callback_data="ignore"))
    row.append(InlineKeyboardButton(text=f"· {page+1}/{total_pages} ·", callback_data="ignore"))
    row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}") if end < len(results) else InlineKeyboardButton(text="✖️", callback_data="ignore"))
    builder.row(*row)
    return builder.as_markup()

# ==========================================
# ОБРАБОТЧИКИ
# ==========================================

@dp.callback_query(F.data == "ignore")
async def ignore_handler(cb: CallbackQuery):
    await cb.answer()

@dp.message(CommandStart())
async def start(message: types.Message):
    await register_user(message.from_user.id, message.from_user.username or "NoUsername")
    if not await check_subscription(message.from_user.id):
        return await message.answer("👋 <b>Привет!</b>\n\nЧтобы пользоваться ботом, подпишись на все каналы:", reply_markup=get_sub_keyboard(), parse_mode="HTML")
    await message.answer("👋 <b>Music & Video Bot</b>\n\n🎵 <b>Для музыки:</b> Напиши название трека.\n🎬 <b>Для видео:</b> Отправь мне ссылку на YouTube/Instagram.\n\n🚀 <i>Жду твой запрос:</i>", parse_mode="HTML")

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return 
    stats = await get_full_stats()
    text = f"📊 <b>СТАТИСТИКА</b>\n\n👥 Люди: <b>{stats['users']}</b>\n💾 Кэш (Аудио БД): <b>{stats['cache']}</b>\n📥 Скачиваний: <b>{stats['downloads']}</b>\n"
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(cb: CallbackQuery):
    if await check_subscription(cb.from_user.id):
        await cb.answer("✅ Подписка подтверждена!")
        await cb.message.delete()
        await cb.message.answer("✅ <b>Ок!</b> Пиши название песни или кидай ссылку:", parse_mode="HTML")
    else: await cb.answer("❌ Вы не подписаны на все каналы!", show_alert=True)

@dp.message(F.text)
async def query_handler(message: types.Message):
    await register_user(message.from_user.id, message.from_user.username)
    if not await check_subscription(message.from_user.id):
        return await message.answer("🛑 Подпишись на все каналы!", reply_markup=get_sub_keyboard())
    text = message.text.strip()
    if any(domain in text.lower() for domain in ['youtube.com', 'youtu.be', 'instagram.com']) and ("http" in text):
        return await handle_video_url(message, text)
    USERS_DATA[message.from_user.id] = {"query": text, "results": [], "page": 0, "mode": "songs"}
    await message.answer(f"📂 Где искать <b>«{safe_html(text)}»</b>?", reply_markup=get_category_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cat_"))
async def category_handler(cb: CallbackQuery):
    await cb.answer("🔎 Ищу...")
    uid = cb.from_user.id
    if uid not in USERS_DATA or "query" not in USERS_DATA[uid]: return await cb.message.edit_text("⚠️ Запрос устарел. Напиши название снова.")
    query = USERS_DATA[uid]["query"]
    mode = "songs" if cb.data == "cat_songs" else "videos" if cb.data == "cat_videos" else "general"
    mode_text = "🎵 Официальные треки" if mode == "songs" else "🎧 Ремиксы и Видео" if mode == "videos" else "🌎 Везде"
    USERS_DATA[uid]["mode"] = mode
    await cb.message.edit_text(f"🔎 Ищу <b>«{safe_html(query)}»</b> в категории: {mode_text}...", parse_mode="HTML")
    tracks = await search_music(query, mode)
    if not tracks: return await cb.message.edit_text(f"😔 Ничего не найдено ({mode_text}). Попробуй другую категорию.")
    USERS_DATA[uid]["results"] = tracks
    USERS_DATA[uid]["page"] = 0
    await cb.message.edit_text(f"🎧 Результаты ({mode_text}):", reply_markup=get_results_keyboard(tracks, 0), parse_mode="HTML")

@dp.callback_query(F.data.startswith("page_"))
async def page_handler(cb: CallbackQuery):
    await cb.answer()
    page = int(cb.data.split("_")[1])
    uid = cb.from_user.id
    if uid in USERS_DATA and "results" in USERS_DATA[uid]:
        USERS_DATA[uid]["page"] = page
        await cb.message.edit_reply_markup(reply_markup=get_results_keyboard(USERS_DATA[uid]["results"], page))
    else: await cb.message.answer("⚠️ Поиск устарел.")

@dp.callback_query(F.data.startswith("dl_"))
async def download_handler(cb: CallbackQuery):
    if not await check_subscription(cb.from_user.id): return await cb.answer("❌ Подпишись на все каналы!", show_alert=True)
    await cb.answer("⏳ Добавляю в очередь...")
    video_id = cb.data[3:] 
    uid = cb.from_user.id
    title, artist, found_in_search = "Track", "Artist", False
    if uid in USERS_DATA and "results" in USERS_DATA[uid]:
        for t in USERS_DATA[uid]["results"]:
            if t['id'] == video_id:
                title, artist, found_in_search = t['title'], t['artist'], True
                break
    search_mode = USERS_DATA.get(uid, {}).get("mode", "songs")
    
    if search_mode == "songs":
        cached = await get_cached_track(video_id)
        if cached and cached[2]:
            await cb.message.answer_audio(cached[2], caption=f"🎧 {safe_html(cached[1] or artist)} — {safe_html(cached[0] or title)}\n🤖 @{bot_username}")
            await log_download(uid, cached[0] or title, cached[1] or artist)
            return

    msg = await cb.message.answer("⏳ <b>Ожидание очереди...</b>", parse_mode="HTML")
    if not found_in_search: title, artist = await get_track_info(video_id)
    async with download_semaphore:
        await msg.edit_text(f"⬇️ <b>Загружаю:</b> {safe_html(artist)} - {safe_html(title)}...", parse_mode="HTML")
        file_path = await download_track_local(video_id)

    if file_path == 'TOO_LONG': return await msg.edit_text("❌ <b>Трек слишком длинный (Больше 15 минут)!</b>", parse_mode="HTML")
    if file_path and os.path.exists(file_path):
        await msg.edit_text("📤 Отправляю файл...")
        try:
            sent = await cb.message.answer_audio(FSInputFile(file_path), title=title, performer=artist, caption=f"🎧 {safe_html(artist)} — {safe_html(title)}\n🤖 @{bot_username}")
            if search_mode == "songs": await cache_track(video_id, title, artist, sent.audio.file_id)
            await log_download(uid, title, artist)
            await msg.delete()
        except Exception: await msg.edit_text("❌ Ошибка отправки в Telegram.")
        finally: safe_remove_file(file_path)
    else: await msg.edit_text("❌ <b>Не удалось скачать трек.</b>", parse_mode="HTML")

# ==========================================
# ЗАПУСК И ВЕБ-СЕРВЕР (ДЛЯ МОНИТОРИНГА UPTIMEROBOT)
# ==========================================

async def dummy_web_server():
    """Этот сервер нужен, чтобы Render видел открытый порт и не убивал бота."""
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render передает свой порт в переменную окружения PORT.
    # Если мы запускаем локально, берем 10000.
    port = int(os.environ.get("PORT", 10000)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port} для пинга мониторинга")

async def main():
    global bot_username
    await init_db()
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    
    # Запускаем наш мини-сервер как фоновую задачу
    asyncio.create_task(dummy_web_server())
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print(f"✅ БОТ ЗАПУЩЕН | Admin: {ADMIN_ID} | Username: @{bot_username}")
        await dp.start_polling(bot)
    except TelegramNetworkError:
        print("\n❌ ОШИБКА: НЕТ ПОДКЛЮЧЕНИЯ К СЕРВЕРАМ TELEGRAM ❌\nВключите VPN или прокси.\n")
        
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Стоп.")