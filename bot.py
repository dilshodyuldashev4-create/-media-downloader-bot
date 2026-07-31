"""
Telegram-бот: скачивание видео с любых платформ (YouTube, TikTok, Instagram, VK и др.)
с выбором качества, поиск и отправка MP3 по названию трека, распознавание музыки
в видео (как Shazam).

Запуск:
    export BOT_TOKEN="ВАШ_ТОКЕН_ОТ_BOTFATHER"
    python bot.py

Требуется установленный ffmpeg в системе (для конвертации в mp3 и склейки видео+аудио).
"""

import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.enums import ParseMode

try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")
MAX_TELEGRAM_FILE_MB = 50  # ограничение обычного бота на отправку файлов

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tg_media_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Временное хранилище в памяти (для реального бота лучше использовать Redis/БД)
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


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋\n\n"
        "🔗 Пришли ссылку на видео (YouTube, TikTok, Instagram, VK и др.) — "
        "предложу качество и скачаю.\n"
        "🎵 Напиши название песни или исполнителя — найду треки и пришлю MP3.\n"
        "🎧 После получения видео можно нажать «Распознать музыку» — попробую "
        "определить трек, звучащий в нём."
    )


# ---------------------------------------------------------------------------
# Скачивание видео по ссылке
# ---------------------------------------------------------------------------

@dp.message(F.text.func(is_url))
async def handle_url(message: Message):
    url = message.text.strip()
    status = await message.answer("🔍 Ищу доступные варианты качества...")

    try:
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, False)
    except Exception as e:
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
        await callback.message.answer(
            f"🎶 Похоже, это:\n<b>{title}</b> — {subtitle}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка распознавания: {e}")


# ---------------------------------------------------------------------------
# Поиск музыки по названию и отправка MP3 (как VK Music бот)
# ---------------------------------------------------------------------------

@dp.message(F.text)
async def handle_search(message: Message):
    query = message.text.strip()
    if not query:
        return

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
        await status.edit_text(f"❌ Ошибка поиска: {e}")
        return

    entries = (info.get("entries") or [])[:5]
    if not entries:
        await status.edit_text("😔 Ничего не найдено.")
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
        await callback.message.answer(f"❌ Ошибка при скачивании: {e}")
    finally:
        await callback.answer()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
