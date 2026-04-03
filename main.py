import asyncio
import logging
import sqlite3
import sys
import os
import math
import yt_dlp
import aiohttp  
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from ytmusicapi import YTMusic
from aiogram.exceptions import TelegramNetworkError

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================

# Токен твоего бота
BOT_TOKEN = "8259649575:AAEGDaHHh3W3-6hCXSE6CD5rgd5YaJ1OMP0"

# ID Админа для команд /stats
ADMIN_ID = 5153531676 

# Настройки канала для обязательной подписки
CHANNEL_ID = "@neon9_news"
CHANNEL_URL = "https://t.me/neon9_news"

DB_NAME = "music_db.sqlite"

# === ПРОКСИ ДЛЯ ОБХОДА БЛОКИРОВОК ===
# Если бот не может подключиться (ошибка 121 / 10060), значит провайдер блокирует Telegram.
# Включите VPN на ПК! Если VPN нет, впишите сюда прокси, например: PROXY = "http://188.255.240.116:8080"
PROXY = None 

# Лимит поиска
SEARCH_LIMIT = 100  

# === ЛИМИТЫ НА СКАЧИВАНИЕ ===
MAX_CONCURRENT_DOWNLOADS = 3
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Максимальная длительность (15 минут)
MAX_DURATION = 900  

# Пути
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

if BASE_DIR not in os.environ["PATH"]:
    os.environ["PATH"] += os.pathsep + BASE_DIR

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

ytmusic = YTMusic()
USERS_DATA = {}

# ==========================================
# БАЗА ДАННЫХ
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            telegram_file_id TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            artist TEXT,
            date TEXT
        )
    """)
    
    conn.commit()
    conn.close()

def get_cached_track(video_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT title, artist, telegram_file_id FROM tracks WHERE video_id = ?", (video_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def cache_track(video_id, title, artist, telegram_file_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO tracks (video_id, title, artist, telegram_file_id)
        VALUES (?, ?, ?, ?)
    """, (video_id, title, artist, telegram_file_id))
    conn.commit()
    conn.close()

def register_user(user_id, username):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone() is None:
        date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO users (user_id, username, first_seen) VALUES (?, ?, ?)", 
                       (user_id, username, date_now))
        conn.commit()
    conn.close()

def log_download(user_id, title, artist):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO history (user_id, title, artist, date) VALUES (?, ?, ?, ?)",
                   (user_id, title, artist, date_now))
    conn.commit()
    conn.close()

def get_full_stats():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tracks")
    cached_tracks = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM history")
    total_downloads = cursor.fetchone()[0]
    cursor.execute("SELECT username, first_seen FROM users ORDER BY rowid DESC LIMIT 5")
    last_users = cursor.fetchall()
    cursor.execute("""
        SELECT artist, title, COUNT(*) as cnt 
        FROM history 
        GROUP BY artist, title 
        ORDER BY cnt DESC 
        LIMIT 5
    """)
    top_tracks = cursor.fetchall()
    conn.close()
    return {
        "users": total_users,
        "cache": cached_tracks,
        "downloads": total_downloads,
        "last_users": last_users,
        "top_tracks": top_tracks
    }

# ==========================================
# ПОИСК И СКАЧИВАНИЕ (АУДИО)
# ==========================================

async def search_music(query: str, mode: str):
    loop = asyncio.get_event_loop()
    try:
        search_filter = None
        if mode == 'songs':
            search_filter = 'songs'
        elif mode == 'videos':
            search_filter = 'videos'
        
        results = await loop.run_in_executor(None, lambda: ytmusic.search(query, filter=search_filter, limit=SEARCH_LIMIT))
        
        parsed_results = []
        for track in results:
            if 'videoId' not in track:
                continue
            
            title = track.get('title', 'Unknown')
            artists = track.get('artists', [])
            artist_names = ", ".join([a['name'] for a in artists]) if artists else "Unknown"
            duration = track.get('duration', '') 
            
            res_type = track.get('resultType', '')
            if res_type == 'video':
                title = f"[Video] {title}"

            parsed_results.append({
                'id': track['videoId'],
                'title': title,
                'artist': artist_names,
                'duration': duration
            })
        return parsed_results
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

async def get_track_info(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        track = await loop.run_in_executor(None, lambda: ytmusic.get_song(video_id))
        title = track['videoDetails']['title']
        author = track['videoDetails']['author']
        return title, author
    except:
        return "Unknown Track", "Unknown Artist"

def _run_yt_dlp_audio(opts, url):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return True
    except yt_dlp.utils.DownloadError as e:
        if 'слишком длинное' in str(e).lower():
            return 'TOO_LONG'
        raise e

async def download_track_local(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{video_id}")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_tmpl + '.%(ext)s',
        'ffmpeg_location': BASE_DIR,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'match_filter': lambda info, *_, **__: 'слишком длинное' if info.get('duration', 0) and info.get('duration', 0) > MAX_DURATION else None,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'source_address': '0.0.0.0', 
        'socket_timeout': 15,        
        'retries': 5,               
        'fragment_retries': 5,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    }

    if os.path.exists(COOKIES_FILE):
        ydl_opts['cookiefile'] = COOKIES_FILE

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _run_yt_dlp_audio(ydl_opts, url))
        if result == 'TOO_LONG':
            return 'TOO_LONG'
            
        final_path = out_tmpl + ".mp3"
        if os.path.exists(final_path):
            return final_path
        return None
    except Exception as e:
        logger.error(f"Audio DL Error: {e}")
        return None

# ==========================================
# СКАЧИВАНИЕ ВИДЕО (YOUTUBE / INSTAGRAM)
# ==========================================

async def handle_video_url(message: types.Message, url: str):
    uid = message.from_user.id
    msg = await message.answer("⏳ <b>Анализирую ссылку и добавляю в очередь...</b>", parse_mode="HTML")
    
    async with download_semaphore:
        await msg.edit_text("⬇️ <b>Скачиваю видео (стабильное качество)...</b>", parse_mode="HTML")
        
        timestamp = int(datetime.now().timestamp())
        filename_base = os.path.join(DOWNLOAD_DIR, f"vid_{uid}_{timestamp}")
        
        file_path = None
        title = "Video"
        
        loop = asyncio.get_event_loop()
        try:
            # ВАЖНО: Качаем стабильный mp4 (до 480p). Это не ломает кодек и файл весит мало!
            ydl_opts = {
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]/best',
                'outtmpl': f'{filename_base}.%(ext)s',
                'ffmpeg_location': BASE_DIR,
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                'geo_bypass': True,
                'match_filter': lambda info, *_, **__: 'слишком длинное' if info.get('duration', 0) and info.get('duration', 0) > MAX_DURATION else None,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            }
            if os.path.exists(COOKIES_FILE):
                ydl_opts['cookiefile'] = COOKIES_FILE
                
            def _dl_video():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        ext = info.get('ext', 'mp4')
                        return f"{filename_base}.{ext}", info.get('title', 'Video')
                except yt_dlp.utils.DownloadError as e:
                    if 'слишком длинное' in str(e).lower():
                        return 'TOO_LONG', ''
                    raise e
                    
            file_path, title = await loop.run_in_executor(None, _dl_video)
            
            if file_path == 'TOO_LONG':
                await msg.edit_text("❌ <b>Видео слишком длинное!</b>\nБот скачивает видео только до 15 минут.", parse_mode="HTML")
                return
                
        except Exception as e:
            logger.error(f"Video Handle Error: {e}")
            await msg.edit_text("❌ <b>Ошибка скачивания.</b>\nВозможно, видео удалено, скрыто или защищено.", parse_mode="HTML")
            try:
                prefix = f"vid_{uid}_{timestamp}"
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(prefix):
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
            except:
                pass
            return

        # === ОТПРАВКА ФАЙЛА ===
        try:
            if file_path and os.path.exists(file_path):
                file_size_bytes = os.path.getsize(file_path)
                file_size_mb = file_size_bytes / (1024 * 1024)
                
                # Защита от битых (нулевых) файлов
                if file_size_bytes == 0:
                    await msg.edit_text("❌ <b>Ошибка: Скачанный файл пуст или поврежден.</b>", parse_mode="HTML")
                elif file_size_mb > 49.5:
                    await msg.edit_text(
                        f"❌ <b>Видео слишком большое ({file_size_mb:.1f} МБ)!</b>\n"
                        f"Лимит Telegram (50 МБ) превышен.",
                        parse_mode="HTML"
                    )
                else:
                    await msg.edit_text("📤 <b>Отправляю видео в Telegram...</b>", parse_mode="HTML")
                    video = FSInputFile(file_path)
                    
                    # ВАЖНО: Добавлен request_timeout=300, чтобы сервер не разрывал связь
                    await message.answer_video(
                        video=video,
                        caption=f"🎬 <b>{title}</b>\n🤖 @{ (await bot.get_me()).username }",
                        parse_mode="HTML",
                        request_timeout=300
                    )
                    await msg.delete()
            else:
                await msg.edit_text("❌ <b>Не удалось скачать видео.</b>", parse_mode="HTML")
                
        except Exception as e:
            logger.error(f"Telegram Send Error: {e}")
            await msg.edit_text("❌ <b>Ошибка отправки.</b> Сервер Telegram отклонил файл. Попробуйте другую ссылку.", parse_mode="HTML")
        finally:
            # СТРОГОЕ УДАЛЕНИЕ ВИДЕО С СЕРВЕРА
            try:
                prefix = f"vid_{uid}_{timestamp}"
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(prefix):
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
            except Exception as cleanup_err:
                logger.error(f"Video Cleanup Error: {cleanup_err}")

# ==========================================
# ПРОВЕРКА ПОДПИСКИ И КЛАВИАТУРЫ
# ==========================================

async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ["creator", "administrator", "member"]:
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        return True 

def get_sub_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Подписаться", url=CHANNEL_URL)
    builder.button(text="✅ Проверить подписку", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

# ОГРОМНЫЙ ТАЙМАУТ СЕССИИ ДЛЯ ИСКЛЮЧЕНИЯ ОБРЫВОВ И ПОДКЛЮЧЕНИЕ ПРОКСИ
if PROXY:
    session = AiohttpSession(timeout=3600, proxy=PROXY)
else:
    session = AiohttpSession(timeout=3600)

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
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items = results[start:end]
    
    for track in items:
        dur = track.get('duration', '')
        dur_text = f" ({dur})" if dur else ""
        text = f"{track['artist']} — {track['title']}{dur_text}"
        if len(text) > 60: text = text[:57] + "..."
        builder.button(text=text, callback_data=f"dl_{track['id']}")
    
    builder.adjust(1)
    
    row = []
    total_pages = math.ceil(len(results) / ITEMS_PER_PAGE)
    
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
    else:
        row.append(InlineKeyboardButton(text="✖️", callback_data="ignore"))

    row.append(InlineKeyboardButton(text=f"· {page+1}/{total_pages} ·", callback_data="ignore"))

    if end < len(results):
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
    else:
        row.append(InlineKeyboardButton(text="✖️", callback_data="ignore"))

    builder.row(*row)
    return builder.as_markup()

# ==========================================
# ОБРАБОТЧИКИ (HANDLERS)
# ==========================================

@dp.callback_query(F.data == "ignore")
async def ignore_handler(cb: CallbackQuery):
    try:
        await cb.answer()
    except:
        pass

@dp.message(CommandStart())
async def start(message: types.Message):
    register_user(message.from_user.id, message.from_user.username or "NoUsername")
    is_sub = await check_subscription(message.from_user.id)
    if not is_sub:
        await message.answer(
            "👋 <b>Привет!</b>\n\nЧтобы пользоваться ботом, подпишись на канал:",
            reply_markup=get_sub_keyboard(),
            parse_mode="HTML"
        )
        return
    text = (
        "👋 <b>Music & Video Bot</b>\n\n"
        "Я умею искать музыку и скачивать видео по ссылкам.\n\n"
        "🎵 <b>Для музыки:</b> Напиши название трека.\n"
        "🎬 <b>Для видео:</b> Отправь мне ссылку на YouTube или Instagram.\n\n"
        "🚀 <i>Жду твой запрос:</i>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return 
    stats = get_full_stats()
    text = (
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"👥 Люди: <b>{stats['users']}</b>\n"
        f"💾 Кэш (Аудио БД): <b>{stats['cache']}</b>\n"
        f"📥 Скачиваний: <b>{stats['downloads']}</b>\n"
    )
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(cb: CallbackQuery):
    is_sub = await check_subscription(cb.from_user.id)
    if is_sub:
        try:
            await cb.answer("✅ Подписка подтверждена!")
        except:
            pass
        await cb.message.delete()
        await cb.message.answer("✅ <b>Ок!</b> Пиши название песни или кидай ссылку:", parse_mode="HTML")
    else:
        try:
            await cb.answer("❌ Вы не подписаны!", show_alert=True)
        except:
            pass

@dp.message(F.text)
async def query_handler(message: types.Message):
    register_user(message.from_user.id, message.from_user.username)

    if not await check_subscription(message.from_user.id):
        await message.answer("🛑 Подпишись на канал!", reply_markup=get_sub_keyboard())
        return

    text = message.text.strip()
    supported_domains = ['youtube.com', 'youtu.be', 'instagram.com']
    
    if any(domain in text.lower() for domain in supported_domains) and ("http://" in text or "https://" in text):
        await handle_video_url(message, text)
        return

    USERS_DATA[message.from_user.id] = {
        "query": text,
        "results": [],
        "page": 0,
        "mode": "songs" 
    }

    await message.answer(
        f"📂 Где искать <b>«{text}»</b>?", 
        reply_markup=get_category_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("cat_"))
async def category_handler(cb: CallbackQuery):
    try:
        await cb.answer("🔎 Ищу...")
    except:
        pass

    uid = cb.from_user.id
    if uid not in USERS_DATA or "query" not in USERS_DATA[uid]:
        await cb.message.edit_text("⚠️ Запрос устарел. Напиши название снова.")
        return

    query = USERS_DATA[uid]["query"]
    cat_code = cb.data 
    
    mode = "general"
    mode_text = "🌎 Везде"
    if cat_code == "cat_songs":
        mode = "songs"
        mode_text = "🎵 Официальные треки"
    elif cat_code == "cat_videos":
        mode = "videos"
        mode_text = "🎧 Ремиксы и Видео"

    USERS_DATA[uid]["mode"] = mode

    await cb.message.edit_text(f"🔎 Ищу <b>«{query}»</b> в категории: {mode_text}...", parse_mode="HTML")
    tracks = await search_music(query, mode)
    
    if not tracks:
        await cb.message.edit_text(f"😔 Ничего не найдено ({mode_text}). Попробуй другую категорию.")
        return

    USERS_DATA[uid]["results"] = tracks
    USERS_DATA[uid]["page"] = 0
    
    await cb.message.edit_text(
        f"🎧 Результаты ({mode_text}):", 
        reply_markup=get_results_keyboard(tracks, 0), 
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("page_"))
async def page_handler(cb: CallbackQuery):
    try:
        await cb.answer()
    except:
        pass

    try:
        page = int(cb.data.split("_")[1])
        uid = cb.from_user.id
        if uid in USERS_DATA and "results" in USERS_DATA[uid]:
            USERS_DATA[uid]["page"] = page
            await cb.message.edit_reply_markup(
                reply_markup=get_results_keyboard(USERS_DATA[uid]["results"], page)
            )
        else:
            await cb.message.answer("⚠️ Поиск устарел.")
    except Exception as e:
        logger.error(f"Page error: {e}")

@dp.callback_query(F.data.startswith("dl_"))
async def download_handler(cb: CallbackQuery):
    if not await check_subscription(cb.from_user.id):
        try:
            await cb.answer("❌ Подпишись!", show_alert=True)
        except:
            pass
        return

    try:
        await cb.answer("⏳ Добавляю в очередь...")
    except:
        pass

    video_id = cb.data[3:] 
    uid = cb.from_user.id
    
    title = "Track"
    artist = "Artist"
    
    found_in_search = False
    if uid in USERS_DATA and "results" in USERS_DATA[uid]:
        for t in USERS_DATA[uid]["results"]:
            if t['id'] == video_id:
                title, artist = t['title'], t['artist']
                found_in_search = True
                break
                
    search_mode = USERS_DATA.get(uid, {}).get("mode", "songs")
    
    if search_mode == "songs":
        cached = get_cached_track(video_id)
        if cached:
            c_title, c_artist, file_id = cached
            if file_id:
                try:
                    real_title = c_title if c_title else title
                    real_artist = c_artist if c_artist else artist
                    await cb.message.answer_audio(file_id, caption=f"🎧 {real_artist} — {real_title}\n🤖 @{ (await bot.get_me()).username }")
                    log_download(uid, real_title, real_artist)
                    return
                except:
                    pass

    msg = await cb.message.answer("⏳ <b>Анализирую трек...</b>", parse_mode="HTML")

    if not found_in_search:
         title, artist = await get_track_info(video_id)

    msg_text = "⏳ <b>Ожидание очереди...</b>"
    if download_semaphore.locked():
        msg_text += "\n⚠️ Очередь загружена, пожалуйста подождите..."
    
    await msg.edit_text(msg_text, parse_mode="HTML")

    async with download_semaphore:
        await msg.edit_text(f"⬇️ <b>Загружаю:</b> {artist} - {title}...", parse_mode="HTML")
        file_path = await download_track_local(video_id)

    if file_path == 'TOO_LONG':
        await msg.edit_text("❌ <b>Трек слишком длинный (Больше 15 минут)!</b>\nЧтобы сервер не сломался, я не буду скачивать этот гигантский сет.", parse_mode="HTML")
        try:
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(video_id):
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
        except: pass
        return

    if file_path and os.path.exists(file_path):
        await msg.edit_text("📤 Отправляю файл...")
        try:
            audio = FSInputFile(file_path)
            sent = await cb.message.answer_audio(
                audio,
                title=title,
                performer=artist,
                caption=f"🎧 {artist} — {title}\n🤖 @{ (await bot.get_me()).username }"
            )
            
            if search_mode == "songs":
                cache_track(video_id, title, artist, sent.audio.file_id)
                
            log_download(uid, title, artist)
            await msg.delete()
        except Exception as e:
            logger.error(f"TG Send Error: {e}")
            await msg.edit_text("❌ Ошибка отправки в Telegram.")
        finally:
            try:
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(video_id):
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
            except Exception as cleanup_err:
                logger.error(f"Audio Cleanup Error: {cleanup_err}")
    else:
        await msg.edit_text(
            "❌ <b>Не удалось скачать трек.</b>\n\n"
            "Возможно он заблокирован для скачивания или недоступен.",
            parse_mode="HTML"
        )

# ==========================================
# ЗАПУСК
# ==========================================

async def main():
    init_db()
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print(f"✅ БОТ ЗАПУЩЕН | Admin: {ADMIN_ID} | ULTIMATE STABLE VIDEO MODE")
        await dp.start_polling(bot)
    except TelegramNetworkError as e:
        print("\n" + "="*60)
        print("❌ ОШИБКА: НЕТ ПОДКЛЮЧЕНИЯ К СЕРВЕРАМ TELEGRAM ❌")
        print("Ваш интернет-провайдер блокирует доступ к сайту Telegram.")
        print("Код бота работает исправно, но у него нет связи с сетью.\n")
        print("КАК ЭТО ИСПРАВИТЬ:")
        print("1. Включите любой VPN на вашем компьютере и запустите бота снова.")
        print("2. ИЛИ впишите рабочий прокси в строку 31 (переменная PROXY).")
        print("="*60 + "\n")
        
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Стоп.")