"""
Telegram-бот: скачивание видео с любых платформ (YouTube, TikTok, Instagram, VK и др.)
с выбором качества, поиск и отправка MP3 по названию трека, распознавание музыки
в видео (как Shazam), плюс админ-панель с рассылкой всем подписчикам.

Разворачивается на Railway.com (см. Procfile / nixpacks.toml в этом репозитории).

Локальный запуск:
    export BOT_TOKEN="ВАШ_ТОКЕН_ОТ_BOTFATHER"
    export ADMIN_IDS="123456789"          # твой Telegram user id (через запятую, если админов несколько)
    python bot.py

Требуется установленный ffmpeg в системе (для конвертации в mp3 и склейки видео+аудио).
На Railway ffmpeg ставится автоматически через nixpacks.toml.
"""

import asyncio
import logging
import os
import sqlite3
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import yt_dlp
import asyncpg
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except Exception:  # ловим не только ImportError, но и сбои внутри самого пакета
    SHAZAM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Логирование — критично для отладки на Railway, где нет локальной консоли,
# только вкладка Logs. Пишем в stdout, Railway сам подхватит.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tg_media_bot")

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    log.critical(
        "Переменная окружения BOT_TOKEN не задана! "
        "На Railway: Project -> Variables -> добавь BOT_TOKEN."
    )
    sys.exit(1)

# ID администраторов через запятую, например "111111111,222222222"
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
if not ADMIN_IDS:
    log.warning(
        "ADMIN_IDS не задан — команда /admin будет недоступна никому. "
        "Узнай свой Telegram ID у @userinfobot и добавь его в Variables на Railway."
    )

MAX_TELEGRAM_FILE_MB = 50  # ограничение обычного бота на отправку файлов
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
YTDLP_COOKIES_FILE = (os.getenv("YTDLP_COOKIES_FILE") or "").strip()

# Путь к базе подписчиков. На Railway контейнер эфемерный: без подключённого
# Volume файл пропадёт при следующем деплое/рестарте. Чтобы база переживала
# рестарты — создай Volume (Settings -> Volumes) и примонтируй его, например,
# на /data, а затем поставь переменную DB_PATH=/data/subscribers.db
DB_PATH = os.getenv("DB_PATH", "subscribers.db")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tg_media_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Временное хранилище в памяти для навигации по кнопкам (для реального бота с
# большой нагрузкой лучше Redis, но для одного/нескольких инстансов сойдёт)
video_cache: dict[str, dict] = {}   # short_id -> {"url", "title", "file_path"}
search_cache: dict[str, dict] = {}  # short_id -> {"0": {"url","title"}, ...}


def short_id() -> str:
    return uuid.uuid4().hex[:10]


def is_url(text: str) -> bool:
    return text.strip().startswith(("http://", "https://"))


def cleanup_file(path: str | Path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# База подписчиков: PostgreSQL Railway с безопасным резервом SQLite
# ---------------------------------------------------------------------------

db_pool: asyncpg.Pool | None = None
USE_POSTGRES = False


async def init_db() -> None:
    global db_pool, USE_POSTGRES

    if DATABASE_URL:
        try:
            db_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subscribers (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            USE_POSTGRES = True
            log.info("База подписчиков: PostgreSQL подключён.")
            return
        except Exception:
            log.exception("PostgreSQL недоступен — включаю резерв SQLite.")
            if db_pool is not None:
                await db_pool.close()
                db_pool = None

    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()
    USE_POSTGRES = False
    log.info("База подписчиков: используется SQLite %s", DB_PATH)


async def close_db() -> None:
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


async def save_subscriber(user_id: int, username: str | None, first_name: str | None) -> None:
    if USE_POSTGRES and db_pool is not None:
        await db_pool.execute(
            """
            INSERT INTO subscribers (user_id, username, first_name, is_active, updated_at)
            VALUES ($1, $2, $3, TRUE, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                is_active = TRUE,
                updated_at = NOW()
            """,
            user_id,
            username or "",
            first_name or "",
        )
        return

    def _save_sqlite():
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            INSERT INTO subscribers (user_id, username, first_name, is_active, updated_at)
            VALUES (?, ?, ?, 1, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                is_active=1,
                updated_at=datetime('now')
            """,
            (user_id, username or "", first_name or ""),
        )
        conn.commit()
        conn.close()

    await asyncio.to_thread(_save_sqlite)


async def get_all_subscriber_ids() -> list[int]:
    if USE_POSTGRES and db_pool is not None:
        rows = await db_pool.fetch(
            "SELECT user_id FROM subscribers WHERE is_active = TRUE ORDER BY first_seen"
        )
        return [int(row["user_id"]) for row in rows]

    def _read_sqlite():
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT user_id FROM subscribers WHERE is_active = 1 ORDER BY first_seen"
        ).fetchall()
        conn.close()
        return [int(row[0]) for row in rows]

    return await asyncio.to_thread(_read_sqlite)


async def count_subscribers() -> int:
    if USE_POSTGRES and db_pool is not None:
        value = await db_pool.fetchval(
            "SELECT COUNT(*) FROM subscribers WHERE is_active = TRUE"
        )
        return int(value or 0)

    def _count_sqlite():
        conn = sqlite3.connect(DB_PATH)
        value = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE is_active = 1"
        ).fetchone()[0]
        conn.close()
        return int(value or 0)

    return await asyncio.to_thread(_count_sqlite)


async def deactivate_subscriber(user_id: int) -> None:
    if USE_POSTGRES and db_pool is not None:
        await db_pool.execute(
            "UPDATE subscribers SET is_active = FALSE, updated_at = NOW() WHERE user_id = $1",
            user_id,
        )
        return

    def _deactivate_sqlite():
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE subscribers SET is_active = 0, updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()

    await asyncio.to_thread(_deactivate_sqlite)


# ---------------------------------------------------------------------------
# Middleware: автоматически сохраняем каждого, кто написал боту или нажал
# любую кнопку, в базу подписчиков — без этого рассылка была бы не по кому.
# ---------------------------------------------------------------------------

class SaveSubscriberMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        user: User | None = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await save_subscriber(user.id, user.username, user.first_name)
            except Exception:
                log.exception("Не удалось сохранить подписчика %s", user.id)
        return await handler(event, data)


dp.update.outer_middleware(SaveSubscriberMiddleware())


# ---------------------------------------------------------------------------
# FSM-состояния для админ-рассылки
# ---------------------------------------------------------------------------

class AdminStates(StatesGroup):
    waiting_broadcast = State()

class UserStates(StatesGroup):
    waiting_music_query = State()
    waiting_instagram_url = State()
    waiting_tiktok_url = State()
    waiting_youtube_url = State()


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎵 Искать музыку"),
                KeyboardButton(text="🟣 Instagram"),
            ],
            [
                KeyboardButton(text="⚫ TikTok"),
                KeyboardButton(text="🔴 YouTube"),
            ],
            [
                KeyboardButton(text="🔗 Скачать по ссылке"),
                KeyboardButton(text="ℹ️ Помощь"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или отправьте ссылку",
    )


def ydl_common_options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).exists():
        options["cookiefile"] = YTDLP_COOKIES_FILE
    return options


async def ask_for_url(message: Message, state: FSMContext, platform: str) -> None:
    state_map = {
        "Instagram": UserStates.waiting_instagram_url,
        "TikTok": UserStates.waiting_tiktok_url,
        "YouTube": UserStates.waiting_youtube_url,
    }
    await state.set_state(state_map.get(platform, UserStates.waiting_youtube_url))
    await message.answer(
        f"🔗 Отправьте ссылку на видео из <b>{platform}</b>.",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    log.info("Получен /start от %s", message.from_user.id if message.from_user else "?")
    await message.answer(
        "✨ <b>VIDEO SAVED BOT</b>\n\n"
        "Скачиваю видео из Instagram, TikTok, YouTube и других сайтов.\n"
        "Также умею искать музыку, отправлять MP3 и распознавать трек из видео.\n\n"
        "Выберите действие кнопкой ниже 👇",
        reply_markup=main_menu(),
    )



MENU_LABELS = {
    "🎵 Искать музыку",
    "🟣 Instagram",
    "📸 Instagram",
    "⚫ TikTok",
    "🎬 TikTok",
    "🔴 YouTube",
    "▶️ YouTube",
    "🔗 Скачать по ссылке",
    "ℹ️ Помощь",
}


async def dispatch_menu_text(message: Message, state: FSMContext) -> bool:
    """Обрабатывает кнопки меню даже если пользователь остался в старом FSM-состоянии."""
    value = (message.text or "").strip()

    if value == "🎵 Искать музыку":
        await state.set_state(UserStates.waiting_music_query)
        await message.answer(
            "🎵 Напишите исполнителя и название песни.\n"
            "Например: <b>Eminem — Mockingbird</b>",
            reply_markup=main_menu(),
        )
        return True

    if value in {"🟣 Instagram", "📸 Instagram"}:
        await ask_for_url(message, state, "Instagram")
        return True

    if value in {"⚫ TikTok", "🎬 TikTok"}:
        await ask_for_url(message, state, "TikTok")
        return True

    if value in {"🔴 YouTube", "▶️ YouTube"}:
        await ask_for_url(message, state, "YouTube")
        return True

    if value == "🔗 Скачать по ссылке":
        await state.clear()
        await message.answer(
            "🔗 Отправьте ссылку на видео из любого поддерживаемого сайта.",
            reply_markup=main_menu(),
        )
        return True

    if value == "ℹ️ Помощь":
        await state.clear()
        await message.answer(
            "📌 <b>Как пользоваться</b>\n\n"
            "• Нажмите кнопку нужной платформы и отправьте ссылку.\n"
            "• Для музыки нажмите «🎵 Искать музыку» и напишите название.\n"
            "• Можно просто отправить любую ссылку без выбора кнопки.",
            reply_markup=main_menu(),
        )
        return True

    return False


@dp.message(Command("menu"))
@dp.message(F.text == "ℹ️ Помощь")
async def show_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📌 <b>Как пользоваться</b>\n\n"
        "• Нажмите кнопку нужной платформы и отправьте ссылку.\n"
        "• Для музыки нажмите «🎵 Искать музыку» и напишите название.\n"
        "• Можно просто отправить любую ссылку без выбора кнопки.",
        reply_markup=main_menu(),
    )


@dp.message(F.text == "🎵 Искать музыку")
async def menu_music(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_music_query)
    await message.answer(
        "🎵 Напишите исполнителя и название песни.\n"
        "Например: <b>Eminem — Mockingbird</b>",
        reply_markup=main_menu(),
    )


@dp.message(F.text.in_({"🟣 Instagram", "📸 Instagram"}))
async def menu_instagram(message: Message, state: FSMContext):
    await ask_for_url(message, state, "Instagram")


@dp.message(F.text.in_({"⚫ TikTok", "🎬 TikTok"}))
async def menu_tiktok(message: Message, state: FSMContext):
    await ask_for_url(message, state, "TikTok")


@dp.message(F.text.in_({"🔴 YouTube", "▶️ YouTube"}))
async def menu_youtube(message: Message, state: FSMContext):
    await ask_for_url(message, state, "YouTube")


@dp.message(F.text == "🔗 Скачать по ссылке")
async def menu_any_url(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔗 Отправьте ссылку на видео из любого поддерживаемого сайта.",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return  # для не-админов бот молчит, как будто такой команды нет

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        ]
    )
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=kb)


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    n = await count_subscribers()
    await callback.message.answer(f"📊 Подписчиков в базе: <b>{n}</b>")
    await callback.answer()


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.answer(
        "Пришли сообщение для рассылки — можно текст, фото, видео, документ, "
        "с любым форматированием. Оно будет разослано всем подписчикам как есть.\n\n"
        "Отменить — /cancel"
    )
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.answer()


@dp.message(Command("cancel"), StateFilter(AdminStates.waiting_broadcast))
async def admin_broadcast_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.")


@dp.message(StateFilter(AdminStates.waiting_broadcast))
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    await state.clear()
    subscriber_ids = await get_all_subscriber_ids()
    status = await message.answer(
        f"⏳ Начинаю рассылку на {len(subscriber_ids)} подписчиков..."
    )

    sent = 0
    failed = 0
    for uid in subscriber_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
        except Exception as e:
            failed += 1
            await deactivate_subscriber(uid)
            log.warning("Не удалось отправить рассылку %s: %s", uid, e)
        # небольшая пауза, чтобы не упереться в лимиты Telegram (~30 сообщений/сек)
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ Рассылка завершена.\n"
        f"Доставлено: {sent}\n"
        f"Не доставлено (бот заблокирован и т.п.): {failed}"
    )
    log.info("Рассылка от %s: доставлено %s, не доставлено %s", message.from_user.id, sent, failed)


# ---------------------------------------------------------------------------
# Скачивание видео по ссылке
# ---------------------------------------------------------------------------

@dp.message(
    StateFilter(
        UserStates.waiting_instagram_url,
        UserStates.waiting_tiktok_url,
        UserStates.waiting_youtube_url,
    ),
    F.text,
)
async def handle_expected_url(message: Message, state: FSMContext):
    if await dispatch_menu_text(message, state):
        return

    if not is_url(message.text or ""):
        await message.answer(
            "❗ Отправьте именно ссылку на видео или выберите другую кнопку.",
            reply_markup=main_menu(),
        )
        return

    await state.clear()
    await handle_url(message)


@dp.message(StateFilter(None), F.text.func(is_url))
async def handle_url(message: Message):
    url = message.text.strip()
    log.info("URL от %s: %s", message.from_user.id if message.from_user else "?", url)
    status = await message.answer("🔍 Ищу доступные варианты качества...")

    try:
        ydl_opts = ydl_common_options() | {"skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, False)
    except Exception as e:
        log.exception("Ошибка extract_info для %s", url)
        await status.edit_text(f"❌ Не удалось получить информацию по ссылке.\n{e}")
        return

    formats = info.get("formats", []) or []
    heights_seen = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        heights_seen[h] = True

    qualities = sorted(heights_seen.keys(), reverse=True)[:6]

    sid = short_id()
    video_cache[sid] = {"url": url, "title": info.get("title", "video")}

    buttons = [
        [InlineKeyboardButton(text=f"🎬 {h}p", callback_data=f"dl:{sid}:{h}")]
        for h in qualities
    ]
    buttons.append(
        [InlineKeyboardButton(text="🎧 Только аудио (MP3)", callback_data=f"dl:{sid}:audio")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    title = info.get("title", "видео")
    await status.edit_text(f"Найдено: {title}\nВыбери качество:", reply_markup=kb)


@dp.callback_query(F.data.startswith("dl:"))
async def handle_download(callback: CallbackQuery):
    _, sid, quality = callback.data.split(":")
    entry = video_cache.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, пришли её ещё раз.", show_alert=True)
        return

    await callback.message.edit_text("⬇️ Скачиваю, подожди немного...")
    url = entry["url"]
    out_template = str(DOWNLOAD_DIR / f"{sid}.%(ext)s")

    try:
        if quality == "audio":
            ydl_opts = ydl_common_options() | {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "quiet": True,
            }
        else:
            ffmpeg_available = shutil.which("ffmpeg") is not None

            if ffmpeg_available:
                video_format = (
                    f"bestvideo[height<={quality}]+bestaudio/"
                    f"best[ext=mp4][height<={quality}]/best[height<={quality}]"
                )
            else:
                # Без FFmpeg берём один уже готовый видеофайл.
                video_format = (
                    f"best[ext=mp4][height<={quality}]/"
                    f"best[height<={quality}]/best"
                )

            ydl_opts = ydl_common_options() | {
                "format": video_format,
                "outtmpl": out_template,
                "quiet": True,
            }

            if ffmpeg_available:
                ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])

        files = list(DOWNLOAD_DIR.glob(f"{sid}.*"))
        if not files:
            raise FileNotFoundError("Файл не найден после загрузки")
        file_path = files[0]

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_FILE_MB:
            await callback.message.answer(
                f"⚠️ Файл весит {size_mb:.0f} МБ — это больше лимита обычного "
                f"бота ({MAX_TELEGRAM_FILE_MB} МБ). Попробуй качество пониже."
            )
            cleanup_file(file_path)
            await callback.answer()
            return

        caption = entry.get("title", "")
        if quality == "audio":
            await callback.message.answer_audio(FSInputFile(file_path), caption=caption)
            cleanup_file(file_path)
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎵 Распознать музыку", callback_data=f"shz:{sid}")
            ]])
            await callback.message.answer_video(FSInputFile(file_path), caption=caption, reply_markup=kb)
            entry["file_path"] = str(file_path)  # оставляем файл для распознавания

        await callback.message.delete()

    except Exception as e:
        log.exception("Ошибка при скачивании %s (качество %s)", url, quality)
        error_text = str(e)
        if "ffmpeg is not installed" in error_text.lower():
            error_text = "На сервере не установлен FFmpeg. Используйте готовый Dockerfile из комплекта."
        elif "sign in to confirm" in error_text.lower():
            error_text = (
                "YouTube временно требует подтверждение. "
                "Instagram и TikTok продолжат работать; для YouTube нужны cookies."
            )
        await callback.message.answer(
            "❌ Не удалось скачать этот файл.\n"
            "Проверьте, что ссылка открытая, и попробуйте ещё раз."
        )
    finally:
        await callback.answer()


# ---------------------------------------------------------------------------
# Распознавание музыки (как Shazam)
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("shz:"))
async def handle_shazam(callback: CallbackQuery):
    if not SHAZAM_AVAILABLE:
        await callback.answer(
            "Модуль распознавания музыки (shazamio) не установлен на сервере.",
            show_alert=True,
        )
        return

    sid = callback.data.split(":")[1]
    entry = video_cache.get(sid)
    if not entry or "file_path" not in entry:
        await callback.answer("Файл не найден, скачай видео заново.", show_alert=True)
        return

    await callback.answer("🎧 Распознаю музыку...")
    try:
        shazam = Shazam()
        result = await shazam.recognize(entry["file_path"])
        track = result.get("track")
        if not track:
            await callback.message.answer("😔 Не удалось распознать музыку в этом видео.")
            return
        title = track.get("title", "неизвестно")
        subtitle = track.get("subtitle", "неизвестно")
        await callback.message.answer(f"🎶 Похоже, это:\n<b>{title}</b> — {subtitle}")
    except Exception as e:
        log.exception("Ошибка распознавания Shazam")
        await callback.message.answer(f"❌ Ошибка распознавания: {e}")


# ---------------------------------------------------------------------------
# Поиск музыки по названию и отправка MP3 (как VK Music бот)
# ---------------------------------------------------------------------------

@dp.message(StateFilter(UserStates.waiting_music_query), F.text)
async def handle_music_query(message: Message, state: FSMContext):
    if await dispatch_menu_text(message, state):
        return

    await state.clear()
    await handle_search(message)


@dp.message(StateFilter(None), F.text)
async def handle_search(message: Message, state: FSMContext):
    if await dispatch_menu_text(message, state):
        return

    query = message.text.strip()
    if not query:
        return

    log.info("Поиск музыки от %s: %s", message.from_user.id if message.from_user else "?", query)
    status = await message.answer(f"🔍 Ищу «{query}»...")

    try:
        ydl_opts = ydl_common_options() | {
            "default_search": "ytsearch5",
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, False)
    except Exception as e:
        log.exception("Ошибка поиска для запроса: %s", query)
        error_text = str(e)
        if "sign in to confirm" in error_text.lower():
            error_text = (
                "YouTube временно требует подтверждение, поэтому поиск музыки недоступен. "
                "Для стабильной работы нужно подключить cookies."
            )
        await status.edit_text(
            "❌ Сейчас поиск музыки через YouTube временно недоступен из-за защиты YouTube.\n\n"
            "Попробуйте позже или отправьте прямую ссылку на видео/аудио."
        )
        return

    entries = (info.get("entries") or [])[:5]
    if not entries:
        await status.edit_text(
            "😔 Ничего не найдено. Попробуй указать исполнителя и название вместе, "
            "например: Eminem - Mockingbird."
        )
        return

    sid = short_id()
    search_cache[sid] = {}

    buttons = []
    for i, e in enumerate(entries):
        title = e.get("title", "Без названия")
        duration = e.get("duration")
        dur_str = ""
        if duration:
            dur_str = f" ({int(duration // 60)}:{int(duration % 60):02d})"
        search_cache[sid][str(i)] = {
            "url": e.get("webpage_url") or e.get("url"),
            "title": title,
        }
        buttons.append([InlineKeyboardButton(
            text=f"{title[:40]}{dur_str}",
            callback_data=f"mp3:{sid}:{i}",
        )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status.edit_text("Выбери трек:", reply_markup=kb)


@dp.callback_query(F.data.startswith("mp3:"))
async def handle_mp3(callback: CallbackQuery):
    _, sid, idx = callback.data.split(":")
    item = search_cache.get(sid, {}).get(idx)
    if not item:
        await callback.answer("Список устарел, повтори поиск.", show_alert=True)
        return

    await callback.message.edit_text(f"⬇️ Скачиваю: {item['title']}...")
    file_id = short_id()
    out_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    try:
        ydl_opts = ydl_common_options() | {
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.to_thread(ydl.download, [item["url"]])

        files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
        if not files:
            raise FileNotFoundError("Файл не найден")
        file_path = files[0]

        await callback.message.answer_audio(FSInputFile(file_path), title=item["title"])
        await callback.message.delete()
        cleanup_file(file_path)

    except Exception as e:
        log.exception("Ошибка при скачивании MP3: %s", item.get("url"))
        await callback.message.answer(f"❌ Ошибка при скачивании: {e}")
    finally:
        await callback.answer()


# ---------------------------------------------------------------------------
# Глобальный обработчик необработанных ошибок —
# чтобы одна упавшая апдейт-обработка не "тихо" гасила бота
# ---------------------------------------------------------------------------

@dp.error()
async def global_error_handler(event):
    log.exception("Необработанная ошибка апдейта: %s", event.exception)
    return True


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    log.info("Запуск бота...")
    await init_db()

    if not shutil.which("ffmpeg"):
        log.warning("FFmpeg не найден. Видео+аудио и MP3 могут не работать.")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
