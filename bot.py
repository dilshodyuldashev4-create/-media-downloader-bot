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
# База подписчиков PostgreSQL (Railway)
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
db_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Подключается к PostgreSQL и создаёт таблицу подписчиков."""
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError(
            "Переменная DATABASE_URL не найдена. "
            "Добавьте PostgreSQL в проект Railway."
        )

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


async def close_db() -> None:
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


def require_db() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError("База данных ещё не подключена.")
    return db_pool


async def save_subscriber(
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    pool = require_db()
    await pool.execute(
        """
        INSERT INTO subscribers (
            user_id,
            username,
            first_name,
            is_active,
            updated_at
        )
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


async def deactivate_subscriber(user_id: int) -> None:
    pool = require_db()
    await pool.execute(
        """
        UPDATE subscribers
        SET is_active = FALSE, updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id,
    )


async def get_all_subscriber_ids() -> list[int]:
    pool = require_db()
    rows = await pool.fetch(
        """
        SELECT user_id
        FROM subscribers
        WHERE is_active = TRUE
        ORDER BY first_seen ASC
        """
    )
    return [int(row["user_id"]) for row in rows]


async def count_subscribers() -> int:
    pool = require_db()
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM subscribers WHERE is_active = TRUE"
    )
    return int(count or 0)


# ---------------------------------------------------------------------------
# Middleware: автоматически сохраняем каждого, кто написал боту или нажал
# любую кнопку, в базу подписчиков — без этого рассылка была бы не по кому.
# ---------------------------------------------------------------------------

class SaveSubscriberMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        user: User | None = data.get("event_from_user")
        if user and not user.is_bot:
            await save_subscriber(user.id, user.username, user.first_name)
        return await handler(event, data)


dp.update.outer_middleware(SaveSubscriberMiddleware())


# ---------------------------------------------------------------------------
# FSM-состояния для админ-рассылки
# ---------------------------------------------------------------------------

class AdminStates(StatesGroup):
    waiting_broadcast = State()


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    log.info("Получен /start от %s", message.from_user.id if message.from_user else "?")
    await message.answer(
        "Привет! 👋\n\n"
        "🔗 Пришли ссылку на видео (YouTube, TikTok, Instagram, VK и др.) — "
        "предложу качество и скачаю.\n"
        "🎵 Напиши название песни или исполнителя — найду треки и пришлю MP3.\n"
        "🎧 После получения видео можно нажать «Распознать музыку» — попробую "
        "определить трек, звучащий в нём."
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

@dp.message(StateFilter(None), F.text.func(is_url))
async def handle_url(message: Message):
    url = message.text.strip()
    log.info("URL от %s: %s", message.from_user.id if message.from_user else "?", url)
    status = await message.answer("🔍 Ищу доступные варианты качества...")

    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
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
            ydl_opts = {
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
            ydl_opts = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
                "merge_output_format": "mp4",
                "outtmpl": out_template,
                "quiet": True,
            }

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
        await callback.message.answer(f"❌ Ошибка при скачивании: {e}")
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

@dp.message(StateFilter(None), F.text)
async def handle_search(message: Message):
    query = message.text.strip()
    if not query:
        return

    log.info("Поиск музыки от %s: %s", message.from_user.id if message.from_user else "?", query)
    status = await message.answer(f"🔍 Ищу «{query}»...")

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch5",
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, False)
    except Exception as e:
        log.exception("Ошибка поиска для запроса: %s", query)
        await status.edit_text(f"❌ Ошибка поиска: {e}")
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
        ydl_opts = {
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
    log.info("PostgreSQL подключён, таблица подписчиков готова.")

    try:
        # На случай, если Railway не убил старый инстанс сразу при редеплое —
        # сбрасываем возможный webhook перед стартом polling.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
