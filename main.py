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
from aiogram.exceptions import TelegramNetworkError
from cachetools import TTLCache

# Загрузка переменных окружения
load_dotenv()

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

REQUIRED_CHANNELS = [
    {"id": "@xSp1der42", "url": "https://t.me/xSp1der42", "name": "🕷 Канал xSp1der42"}
]

DB_NAME = "music_db.sqlite"
PROXY = None 
MAX_DURATION = 900  

MAX_CONCURRENT_DOWNLOADS = 3
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

if BASE_DIR not in os.environ["PATH"]:
    os.environ["PATH"] += os.pathsep + BASE_DIR

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

bot_username = ""
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
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, artist TEXT, date TEXT)")
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
    return {"users": total_users, "cache": cached_tracks, "downloads": total_downloads}

def safe_html(text: str) -> str: return html.escape(str(text))

def safe_remove_file(filepath: str):
    try:
        if filepath and os.path.exists(filepath): os.remove(filepath)
    except Exception as e: logger.error(f"Failed to remove file: {e}")

# ==========================================
# ПОИСК МУЗЫКИ (SOUNDCLOUD)
# ==========================================

async def search_soundcloud(query: str):
    """Ищет официальные треки в SoundCloud (обходит блокировки)"""
    loop = asyncio.get_event_loop()
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }
    
    def _search():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # scsearch15 - ищет 15 результатов на SoundCloud
            return ydl.extract_info(f"scsearch15:{query}", download=False)
            
    try:
        info = await loop.run_in_executor(None, _search)
        results = []
        for entry in info.get('entries', []):
            if not entry.get('url'): continue
            
            # Чистим название (иногда SC отдает "Артист - Название")
            artist = entry.get('uploader', 'Unknown')
            title = entry.get('title', 'Unknown')
            if title.lower().startswith(f"{artist.lower()} - "):
                title = title[len(artist)+3:]
            elif title.lower().startswith(f"{artist.lower()} — "):
                title = title[len(artist)+3:]
            
            # Форматируем длительность
            dur = entry.get('duration')
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
            
            results.append({
                'id': entry.get('id', str(hash(entry['url']))),
                'url': entry['url'],
                'title': title,
                'artist': artist,
                'duration': dur_str
            })
        return results
    except Exception as e:
        logger.error(f"SoundCloud Search Error: {e}")
        return []

async def download_soundcloud_track(track_url: str, track_id: str):
    """Скачивает трек из SoundCloud напрямую"""
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{track_id}")
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_tmpl + '.%(ext)s',
        'ffmpeg_location': BASE_DIR,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
        'source_address': '0.0.0.0',
    }
    
    loop = asyncio.get_event_loop()
    try:
        def _dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([track_url])
                
        await loop.run_in_executor(None, _dl)
        final_path = out_tmpl + ".mp3"
        return final_path if os.path.exists(final_path) else None
    except Exception as e:
        logger.error(f"SoundCloud Download Error: {e}")
        return None

# ==========================================
# СКАЧИВАНИЕ ВИДЕО (Instagram, TikTok, YT)
# ==========================================

async def handle_video_url(message: types.Message, url: str):
    uid = message.from_user.id
    msg = await message.answer("⏳ <b>Анализирую ссылку на видео...</b>", parse_mode="HTML")
    async with download_semaphore:
        await msg.edit_text("⬇️ <b>Скачиваю видео...</b>", parse_mode="HTML")
        timestamp = int(datetime.now().timestamp())
        filename_base = os.path.join(DOWNLOAD_DIR, f"vid_{uid}_{timestamp}")
        file_path, title = None, "Video"
        loop = asyncio.get_event_loop()
        try:
            ydl_opts = {
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]/best', 'outtmpl': f'{filename_base}.%(ext)s',
                'ffmpeg_location': BASE_DIR, 'merge_output_format': 'mp4', 'quiet': True,
                'match_filter': lambda info, *_, **__: 'слишком длинное' if (info.get('duration') or 0) > MAX_DURATION else None,
                'extractor_args': {'youtube': {'player_client': ['android']}},
            }
            if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE
            def _dl_video():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        return f"{filename_base}.mp4", info.get('title', 'Video')
                except yt_dlp.utils.DownloadError as e:
                    if 'слишком длинное' in str(e).lower(): return 'TOO_LONG', ''
                    raise e
            file_path, title = await loop.run_in_executor(None, _dl_video)
            if file_path == 'TOO_LONG': return await msg.edit_text("❌ <b>Видео слишком длинное!</b>", parse_mode="HTML")
        except Exception:
            await msg.edit_text("❌ <b>Ошибка скачивания видео (возможно, защита или приватный профиль).</b>", parse_mode="HTML")
            safe_remove_file(f"{filename_base}.mp4")
            return

        try:
            if file_path and os.path.exists(file_path):
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if file_size_mb > 49.5: await msg.edit_text(f"❌ <b>Видео слишком большое!</b>", parse_mode="HTML")
                else:
                    await msg.edit_text("📤 <b>Отправляю видео...</b>", parse_mode="HTML")
                    await message.answer_video(video=FSInputFile(file_path), caption=f"🎬 <b>{safe_html(title)}</b>\n🤖 @{bot_username}", parse_mode="HTML")
                    await msg.delete()
            else: await msg.edit_text("❌ <b>Не удалось скачать видео.</b>", parse_mode="HTML")
        except Exception: await msg.edit_text("❌ <b>Ошибка отправки файла в Telegram.</b>", parse_mode="HTML")
        finally: safe_remove_file(file_path)

# ==========================================
# ИНТЕРФЕЙС И КЛАВИАТУРЫ
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

def get_results_keyboard(results, page: int):
    builder = InlineKeyboardBuilder()
    ITEMS_PER_PAGE = 5
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items = results[start:end]
    
    for i, track in enumerate(items):
        actual_index = start + i
        dur_text = f" ({track.get('duration')})" if track.get('duration') else ""
        text = f"{track['artist']} — {track['title']}{dur_text}"
        builder.button(text=text[:60] + ("..." if len(text) > 60 else ""), callback_data=f"dl_{actual_index}")
        
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
        return await message.answer("👋 <b>Привет!</b>\n\nЧтобы пользоваться ботом, подпишись на канал:", reply_markup=get_sub_keyboard(), parse_mode="HTML")
    await message.answer("👋 <b>Music & Video Bot</b>\n\n🎵 <b>Для музыки:</b> Напиши название трека.\n🎬 <b>Для видео:</b> Отправь мне ссылку на YouTube/Instagram/TikTok.\n\n🚀 <i>Жду твой запрос:</i>", parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(cb: CallbackQuery):
    if await check_subscription(cb.from_user.id):
        await cb.answer("✅ Подписка подтверждена!")
        await cb.message.delete()
        await cb.message.answer("✅ <b>Ок!</b> Пиши название песни или кидай ссылку:", parse_mode="HTML")
    else: await cb.answer("❌ Вы не подписаны на канал!", show_alert=True)

@dp.message(F.text)
async def query_handler(message: types.Message):
    await register_user(message.from_user.id, message.from_user.username)
    if not await check_subscription(message.from_user.id):
        return await message.answer("🛑 Подпишись на канал!", reply_markup=get_sub_keyboard())
        
    text = message.text.strip()
    
    # Видео скачиваем как раньше (по ссылкам)
    if any(domain in text.lower() for domain in ['youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com']) and ("http" in text):
        return await handle_video_url(message, text)
        
    # Поиск официальной музыки через SoundCloud
    uid = message.from_user.id
    msg = await message.answer(f"🔎 Ищу <b>«{safe_html(text)}»</b> в официальных базах...", parse_mode="HTML")
    
    tracks = await search_soundcloud(text)
    
    if not tracks: 
        return await msg.edit_text(f"😔 По запросу «{safe_html(text)}» ничего не найдено. Попробуйте написать иначе.")
        
    USERS_DATA[uid] = {"query": text, "results": tracks, "page": 0}
    await msg.edit_text(f"🎧 <b>Официальные треки (SoundCloud):</b>", reply_markup=get_results_keyboard(tracks, 0), parse_mode="HTML")

@dp.callback_query(F.data.startswith("page_"))
async def page_handler(cb: CallbackQuery):
    await cb.answer()
    page = int(cb.data.split("_")[1])
    uid = cb.from_user.id
    if uid in USERS_DATA and "results" in USERS_DATA[uid]:
        USERS_DATA[uid]["page"] = page
        await cb.message.edit_reply_markup(reply_markup=get_results_keyboard(USERS_DATA[uid]["results"], page))
    else: await cb.message.answer("⚠️ Поиск устарел. Напишите название трека заново.")

@dp.callback_query(F.data.startswith("dl_"))
async def download_handler(cb: CallbackQuery):
    if not await check_subscription(cb.from_user.id): return await cb.answer("❌ Подпишись на канал!", show_alert=True)
    
    uid = cb.from_user.id
    if uid not in USERS_DATA or "results" not in USERS_DATA[uid]:
        return await cb.answer("⚠️ Ошибка: поиск устарел. Напишите название заново.", show_alert=True)
        
    await cb.answer("⏳ Скачиваю...")
    track_index = int(cb.data[3:])
    
    try:
        track = USERS_DATA[uid]["results"][track_index]
    except IndexError:
        return await cb.answer("⚠️ Ошибка: трек не найден.", show_alert=True)
        
    track_id = track['id']
    title = track['title']
    artist = track['artist']
    mp3_url = track['url']
                
    # Проверка кеша БД
    cached = await get_cached_track(track_id)
    if cached and cached[2]:
        await cb.message.answer_audio(cached[2], caption=f"🎧 {safe_html(artist)} — {safe_html(title)}\n🤖 @{bot_username}")
        await log_download(uid, title, artist)
        return

    msg = await cb.message.answer("⚡ <b>Загрузка файла...</b>", parse_mode="HTML")
    
    async with download_semaphore:
        file_path = await download_soundcloud_track(mp3_url, track_id)

    if file_path and os.path.exists(file_path):
        await msg.edit_text("📤 Отправляю файл...")
        try:
            sent = await cb.message.answer_audio(FSInputFile(file_path), title=title, performer=artist, caption=f"🎧 {safe_html(artist)} — {safe_html(title)}\n🤖 @{bot_username}")
            await cache_track(track_id, title, artist, sent.audio.file_id)
            await log_download(uid, title, artist)
            await msg.delete()
        except Exception: await msg.edit_text("❌ Ошибка отправки в Telegram.")
        finally: safe_remove_file(file_path)
    else: 
        await msg.edit_text("❌ <b>Не удалось скачать трек. Попробуйте другой из списка.</b>", parse_mode="HTML")

# ==========================================
# ЗАПУСК И ВЕБ-СЕРВЕР
# ==========================================

async def dummy_web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port}")

async def main():
    global bot_username
    await init_db()
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    
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