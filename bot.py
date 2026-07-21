import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TPE1, TIT2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("media-bot-pro-v6-tiktok-instagram-youtube")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
AUDD_TOKEN = os.environ.get("AUDD_API_TOKEN", "").strip()
COOKIES_B64 = os.environ.get("YTDLP_COOKIES_B64", "").strip()
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "49"))
MAX_RECOGNITION_MB = int(os.environ.get("MAX_RECOGNITION_MB", "18"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "2"))
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "8"))
DEFAULT_MP3_QUALITY = int(os.environ.get("DEFAULT_MP3_QUALITY", "320"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "21600"))
# Верхний потолок для входящих файлов на распознавание (голос/аудио/видео),
# чтобы не скачивать в память гигантские документы просто так.
MAX_INCOMING_MB = int(os.environ.get("MAX_INCOMING_MB", "40"))

URL_RE = re.compile(r"https?://\S+", re.I)
TRAILING_PUNCT = ").,]}\"'»›"
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL)
CACHE_DIR = Path("/tmp/media_bot_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    log.warning("ffmpeg не найден в PATH — обложки и извлечение MP3 могут не работать.")

WELCOME = (
    "👋 Отправь мне:\n\n"
    "🔎 название песни или исполнителя — я покажу варианты, а после выбора "
    "отправлю настоящий MP3-файл прямо в Telegram;\n"
    "🔗 ссылку TikTok / Instagram / YouTube — выберешь видео без водяного знака, когда источник это позволяет, или MP3;\n"
    "🎤 голосовое, 🎵 аудио или 🎬 видео — попробую распознать песню.\n\n"
    "Никаких ссылок вместо MP3: после выбора бот загружает и отправляет аудиофайл.\n"
    "Используй бот только для контента, который тебе разрешено скачивать."
)

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
]


def install_cookies() -> str | None:
    if not COOKIES_B64:
        return None
    try:
        path = Path("/tmp/cookies.txt")
        path.write_bytes(base64.b64decode(COOKIES_B64))
        return str(path)
    except Exception:
        log.exception("Не удалось декодировать YTDLP_COOKIES_B64")
        return None


COOKIE_FILE = install_cookies()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти музыку", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ])


def media_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Видео 480p", callback_data="video_480"),
            InlineKeyboardButton("🎬 Видео 720p", callback_data="video_720"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 128", callback_data="audio_128"),
            InlineKeyboardButton("💎 MP3 320", callback_data="audio_320"),
        ],
        [InlineKeyboardButton("🎧 Распознать песню", callback_data="recognize_url")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=main_keyboard())


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(WELCOME, reply_markup=main_keyboard())


def common_ydl(outtmpl: str, user_agent: str) -> dict:
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 40,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "file_access_retries": 5,
        "concurrent_fragment_downloads": 6,
        "http_headers": {
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "geo_bypass": True,
        "cachedir": False,
        "windowsfilenames": True,
        "trim_file_name": 100,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]},
        },
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    return opts


def clean_cache() -> None:
    now = time.time()
    for path in CACHE_DIR.glob("*"):
        try:
            if path.is_file() and now - path.stat().st_mtime > CACHE_TTL:
                path.unlink()
        except Exception:
            log.warning("Не удалось удалить файл кэша: %s", path)


def cache_key(url: str, kind: str, quality: int) -> str:
    raw = f"{url}|{kind}|{quality}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._ -]+", "", value).strip()
    return cleaned[:90] or "audio"



def platform_name(url: str) -> str:
    lowered = url.lower()
    if "tiktok.com" in lowered:
        return "TikTok"
    if "instagram.com" in lowered:
        return "Instagram"
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "YouTube"
    return "сайт"


def search_youtube(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    opts = common_ydl("%(title)s.%(ext)s", USER_AGENTS[0])
    opts.update({
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    results = []
    for entry in (info or {}).get("entries", []) or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        results.append({
            "title": entry.get("title") or "Без названия",
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return results


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def pick_downloaded_file(workdir: Path, kind: str) -> Path:
    if kind == "audio":
        files = list(workdir.glob("*.mp3"))
    else:
        files = [
            p for p in workdir.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        ]

    if not files:
        raise RuntimeError("Файл после загрузки не найден")

    return max(files, key=lambda p: p.stat().st_size)


def try_download(
    url: str,
    kind: str,
    quality: int,
    workdir: Path,
    user_agent: str,
) -> tuple[Path, dict]:
    opts = common_ydl(str(workdir / "%(title).100s.%(ext)s"), user_agent)

    if kind == "video":
        opts.update({
            "format": (
                f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
                f"b[height<={quality}][ext=mp4]/"
                f"bv*[height<={quality}]+ba/b[height<={quality}]/best"
            ),
            "merge_output_format": "mp4",
            "format_sort": [
                f"res:{quality}",
                "ext:mp4:m4a",
                "codec:h264:aac",
                "size",
            ],
        })
    else:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(quality),
            }],
        })

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    path = pick_downloaded_file(workdir, kind)
    return path, info or {}


CACHE_META_FIELDS = ("title", "artist", "uploader", "channel", "album", "thumbnail")


def _meta_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".json")


def _save_cache_meta(target: Path, info: dict) -> None:
    try:
        meta = {field: info.get(field) for field in CACHE_META_FIELDS}
        _meta_path(target).write_text(json.dumps(meta, ensure_ascii=False))
    except Exception:
        log.warning("Не удалось сохранить метаданные кэша", exc_info=True)


def _load_cache_meta(target: Path) -> dict:
    meta_file = _meta_path(target)
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text())
        except Exception:
            log.warning("Не удалось прочитать метаданные кэша", exc_info=True)
    return {"title": target.stem.split("__", 1)[-1]}


def download_media(
    url: str,
    kind: str,
    quality: int,
) -> tuple[Path, dict, bool]:
    clean_cache()
    key = cache_key(url, kind, quality)
    cached = [p for p in CACHE_DIR.glob(f"{key}__*") if p.suffix != ".json"]

    if cached:
        path = cached[0]
        return path, _load_cache_meta(path), True

    last_error: Exception | None = None

    for user_agent in USER_AGENTS:
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                path, info = try_download(
                    url=url,
                    kind=kind,
                    quality=quality,
                    workdir=Path(temp_dir),
                    user_agent=user_agent,
                )

                title = info.get("title") or path.stem
                target = CACHE_DIR / f"{key}__{safe_name(title)}{path.suffix}"
                shutil.copy2(path, target)
                _save_cache_meta(target, info)
                return target, info, False
            except Exception as exc:
                last_error = exc
                log.warning("Попытка загрузки не удалась: %s", exc)

    raise RuntimeError(str(last_error) if last_error else "Неизвестная ошибка загрузки")


def download_thumbnail(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": USER_AGENTS[0]},
        )
        response.raise_for_status()
        if len(response.content) > 5 * 1024 * 1024:
            return None
        return response.content
    except Exception:
        return None


def make_telegram_cover(info: dict, workdir: Path) -> Path | None:
    """Создаёт отдельную квадратную JPEG-обложку для карточки Telegram Audio."""
    thumbnail = download_thumbnail(info.get("thumbnail"))
    if not thumbnail:
        return None

    source = workdir / "cover_source.jpg"
    target = workdir / "cover_telegram.jpg"
    source.write_bytes(thumbnail)

    if not FFMPEG_AVAILABLE:
        return source

    # Telegram принимает JPEG thumbnail небольшого размера.
    # ffmpeg обрезает картинку в квадрат и уменьшает её до 320x320.
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(source),
                "-vf", "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
                "-frames:v", "1",
                str(target),
            ],
            timeout=30,
            capture_output=True,
        )
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return target
    except (subprocess.TimeoutExpired, OSError):
        log.warning("Не удалось создать обложку через ffmpeg", exc_info=True)

    return source if source.exists() else None


def add_mp3_tags(path: Path, info: dict) -> None:
    try:
        title = info.get("title") or path.stem
        artist = info.get("artist") or info.get("uploader") or info.get("channel") or ""
        album = info.get("album") or ""
        thumbnail = download_thumbnail(info.get("thumbnail"))

        try:
            tags = ID3(path)
        except Exception:
            tags = ID3()

        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=title))

        if artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=artist))

        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=album))

        if thumbnail:
            tags.delall("APIC")
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=thumbnail,
            ))

        tags.save(path, v2_version=3)
    except Exception:
        log.exception("Не удалось добавить MP3-теги")


async def call_with_retry(coro_func, *args, retries: int = 2, **kwargs):
    """Повторяет вызов Telegram API при RetryAfter/TimedOut — частая причина
    'зависших' отправок больших аудио/видео файлов."""
    seekable_kwargs = [v for v in kwargs.values() if hasattr(v, "seek")]

    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except RetryAfter as exc:
            if attempt == retries:
                raise
            for f in seekable_kwargs:
                f.seek(0)
            await asyncio.sleep(exc.retry_after + 1)
        except TimedOut:
            if attempt == retries:
                raise
            for f in seekable_kwargs:
                f.seek(0)
            await asyncio.sleep(2 * (attempt + 1))


async def send_mp3(
    message,
    url: str,
    quality: int,
    status_text: str,
    performer_hint: str | None = None,
):
    status = await message.reply_text(status_text)

    async with DOWNLOAD_SEMAPHORE:
        try:
            await message.chat.send_action(ChatAction.UPLOAD_AUDIO)
            path, info, from_cache = await asyncio.to_thread(
                download_media,
                url,
                "audio",
                quality,
            )

            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > MAX_UPLOAD_MB:
                await status.edit_text(
                    f"MP3 получился {size_mb:.1f} МБ и превышает установленный "
                    f"лимит {MAX_UPLOAD_MB} МБ."
                )
                return

            await asyncio.to_thread(add_mp3_tags, path, info)

            title = info.get("title") or path.stem
            performer = (
                info.get("artist")
                or performer_hint
                or info.get("uploader")
                or info.get("channel")
                or None
            )

            await status.edit_text(
                "⚡ Нашёл в кэше, отправляю MP3…"
                if from_cache
                else "📤 Отправляю MP3-файл…"
            )

            # Отдельная обложка нужна, чтобы Telegram показал красивую
            # музыкальную карточку как на примере пользователя.
            with tempfile.TemporaryDirectory() as cover_dir:
                cover_path = await asyncio.to_thread(
                    make_telegram_cover,
                    info,
                    Path(cover_dir),
                )

                with path.open("rb") as file_obj:
                    if cover_path and cover_path.exists():
                        with cover_path.open("rb") as cover_obj:
                            await call_with_retry(
                                message.reply_audio,
                                audio=file_obj,
                                thumbnail=cover_obj,
                                title=title,
                                performer=performer,
                                filename=f"{safe_name(title)}.mp3",
                                caption=f"🎵 {title}\n👤 {performer or 'Неизвестный исполнитель'}",
                                read_timeout=300,
                                write_timeout=300,
                                connect_timeout=60,
                                pool_timeout=60,
                            )
                    else:
                        await call_with_retry(
                            message.reply_audio,
                            audio=file_obj,
                            title=title,
                            performer=performer,
                            filename=f"{safe_name(title)}.mp3",
                            caption=f"🎵 {title}\n👤 {performer or 'Неизвестный исполнитель'}",
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60,
                            pool_timeout=60,
                        )

            await status.delete()

        except Exception as exc:
            log.exception("Ошибка отправки MP3: %s", exc)
            error_text = str(exc)
            if len(error_text) > 600:
                error_text = error_text[-600:]
            await status.edit_text(
                "Не удалось отправить MP3.\n\n"
                "Возможные причины: YouTube временно блокирует Railway, "
                "нужны cookies или трек недоступен в регионе.\n\n"
                f"Техническая причина:\n{error_text}"
            )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    match = URL_RE.search(text)

    if match:
        url = match.group(0).rstrip(TRAILING_PUNCT)
        context.user_data["media_url"] = url
        platform = platform_name(url)

        await update.message.reply_text(
            f"✅ Ссылка {platform} принята.\n"
            "Выбери: скачать видео или извлечь музыку в MP3:",
            reply_markup=media_keyboard(),
        )
        return

    if len(text) < 2:
        await update.message.reply_text("Напиши название песни или исполнителя.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        tracks = await asyncio.to_thread(search_youtube, text, SEARCH_LIMIT)
    except Exception:
        log.exception("Ошибка поиска музыки")
        await update.message.reply_text(
            "Не удалось выполнить поиск. Попробуй написать название точнее."
        )
        return

    if not tracks:
        await update.message.reply_text("Ничего не нашёл. Попробуй другое название.")
        return

    context.user_data["search_results"] = tracks
    lines = ["🎵 Выбери песню — бот отправит MP3-файл:\n"]
    buttons = []

    for index, item in enumerate(tracks, start=1):
        duration = format_duration(item.get("duration"))
        uploader = item.get("uploader")
        meta = " • ".join(value for value in [uploader, duration] if value)
        lines.append(
            f"{index}. {item['title']}"
            + (f"\n   {meta}" if meta else "")
        )
        buttons.append([
            InlineKeyboardButton(
                f"🎵 MP3 {index}",
                callback_data=f"track_mp3_{index - 1}",
            )
        ])

    result_text = "\n".join(lines)
    if len(result_text) > 4000:
        result_text = result_text[:4000] + "\n…"

    await update.message.reply_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def track_mp3_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    results = context.user_data.get("search_results") or []

    try:
        index = int(query.data.rsplit("_", 1)[1])
        item = results[index]
    except Exception:
        await query.message.reply_text(
            "Результаты поиска устарели. Напиши название песни ещё раз."
        )
        return

    await send_mp3(
        message=query.message,
        url=item["url"],
        quality=DEFAULT_MP3_QUALITY,
        status_text=f"⏳ Загружаю MP3:\n{item['title']}",
        performer_hint=item.get("uploader"),
    )


async def media_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    url = context.user_data.get("media_url")
    if not url:
        await query.message.reply_text("Ссылка устарела. Отправь её ещё раз.")
        return

    action = query.data

    if action.startswith("audio_"):
        quality = int(action.split("_")[1])
        await send_mp3(
            message=query.message,
            url=url,
            quality=quality,
            status_text=f"⏳ Готовлю MP3 {quality} kbps…",
        )
        return

    quality = int(action.split("_")[1])
    status = await query.message.reply_text(f"⏳ Скачиваю видео из {platform_name(url)} в качестве до {quality}p…")

    async with DOWNLOAD_SEMAPHORE:
        try:
            await query.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
            path, info, from_cache = await asyncio.to_thread(
                download_media,
                url,
                "video",
                quality,
            )

            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > MAX_UPLOAD_MB:
                await status.edit_text(
                    f"Видео получилось {size_mb:.1f} МБ и превышает лимит "
                    f"{MAX_UPLOAD_MB} МБ. Попробуй 480p."
                )
                return

            title = info.get("title") or path.stem
            await status.edit_text(
                "⚡ Нашёл в кэше, отправляю…"
                if from_cache
                else "📤 Отправляю видео…"
            )

            with path.open("rb") as file_obj:
                await call_with_retry(
                    query.message.reply_video,
                    video=file_obj,
                    caption=f"🎬 {title}\n📥 Источник: {platform_name(url)}\n📺 Качество: до {quality}p",
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60,
                )

            await status.delete()

        except Exception as exc:
            log.exception("Ошибка загрузки видео: %s", exc)
            error_text = str(exc)
            if len(error_text) > 600:
                error_text = error_text[-600:]
            await status.edit_text(
                "Не удалось загрузить видео.\n\n"
                "Для закрытых публикаций или блокировок добавь cookies через "
                "YTDLP_COOKIES_B64.\n\n"
                f"Техническая причина:\n{error_text}"
            )


def extract_audio_from_url(url: str, workdir: Path) -> Path:
    last_error: Exception | None = None

    for user_agent in USER_AGENTS:
        try:
            opts = common_ydl(str(workdir / "source.%(ext)s"), user_agent)
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }],
            })

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

            files = list(workdir.glob("*.mp3"))
            if files:
                return files[0]

        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        str(last_error) if last_error else "Не удалось извлечь аудио"
    )


def recognize_audd(path: Path) -> dict:
    if not AUDD_TOKEN:
        return {
            "error": "Для распознавания добавь AUDD_API_TOKEN в Railway."
        }

    if path.stat().st_size > MAX_RECOGNITION_MB * 1024 * 1024:
        return {
            "error": (
                f"Файл больше лимита распознавания "
                f"{MAX_RECOGNITION_MB} МБ."
            )
        }

    with path.open("rb") as file_obj:
        response = requests.post(
            "https://api.audd.io/",
            data={
                "api_token": AUDD_TOKEN,
                "return": "apple_music,spotify",
            },
            files={"file": file_obj},
            timeout=90,
        )

    response.raise_for_status()
    return response.json()


def recognition_result(data: dict) -> tuple[str, dict | None]:
    if data.get("error"):
        return f"⚠️ {data['error']}", None

    result = data.get("result")
    if not result:
        return "Не удалось распознать песню.", None

    artist = result.get("artist", "Неизвестный исполнитель")
    title = result.get("title", "Неизвестная песня")
    album = result.get("album")
    release = result.get("release_date")

    text = f"🎧 Нашёл:\n\n👤 {artist}\n🎵 {title}"
    if album:
        text += f"\n💿 {album}"
    if release:
        text += f"\n📅 {release}"

    return text, {
        "artist": artist,
        "title": title,
        "query": f"{artist} {title}",
    }


async def recognize_url_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    url = context.user_data.get("media_url")
    if not url:
        await query.message.reply_text("Ссылка устарела. Отправь её ещё раз.")
        return

    status = await query.message.reply_text("🎧 Пытаюсь распознать песню…")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = await asyncio.to_thread(
                extract_audio_from_url,
                url,
                Path(temp_dir),
            )
            data = await asyncio.to_thread(recognize_audd, audio_path)

        text, song = recognition_result(data)

        if song:
            context.user_data["recognized_song"] = song
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🎵 Найти и скачать MP3",
                    callback_data="recognized_mp3",
                )
            ]])
        else:
            keyboard = None

        await status.edit_text(text, reply_markup=keyboard)

    except Exception:
        log.exception("Ошибка распознавания по ссылке")
        await status.edit_text("Не удалось распознать музыку по этой ссылке.")


async def recognized_mp3_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    song = context.user_data.get("recognized_song")
    if not song:
        await query.message.reply_text("Результат устарел. Распознай песню ещё раз.")
        return

    try:
        tracks = await asyncio.to_thread(search_youtube, song["query"], 1)
    except Exception:
        tracks = []

    if not tracks:
        await query.message.reply_text("Не удалось найти MP3 этой песни.")
        return

    await send_mp3(
        message=query.message,
        url=tracks[0]["url"],
        quality=DEFAULT_MP3_QUALITY,
        status_text=f"⏳ Загружаю MP3:\n{song['artist']} — {song['title']}",
        performer_hint=song["artist"],
    )


MEDIA_DOC_PREFIXES = ("audio/", "video/")


async def recognize_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    suffix = ".bin"
    file_size = None

    if message.audio:
        suffix = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
        file_size = message.audio.file_size
        tg_getter = message.audio.get_file
    elif message.voice:
        suffix = ".ogg"
        file_size = message.voice.file_size
        tg_getter = message.voice.get_file
    elif message.video:
        suffix = Path(message.video.file_name or "video.mp4").suffix or ".mp4"
        file_size = message.video.file_size
        tg_getter = message.video.get_file
    elif message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith(MEDIA_DOC_PREFIXES):
            # Не тратим время/трафик на документы, которые точно не медиа.
            return
        suffix = Path(message.document.file_name or "file.bin").suffix or ".bin"
        file_size = message.document.file_size
        tg_getter = message.document.get_file
    else:
        return

    if file_size and file_size > MAX_INCOMING_MB * 1024 * 1024:
        await message.reply_text(
            f"Файл больше {MAX_INCOMING_MB} МБ, я не смогу его обработать."
        )
        return

    tg_file = await tg_getter()
    status = await message.reply_text("🎧 Пытаюсь распознать песню…")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / f"input{suffix}"
            await tg_file.download_to_drive(custom_path=str(path))
            data = await asyncio.to_thread(recognize_audd, path)

        text, song = recognition_result(data)

        if song:
            context.user_data["recognized_song"] = song
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🎵 Найти и скачать MP3",
                    callback_data="recognized_mp3",
                )
            ]])
        else:
            keyboard = None

        await status.edit_text(text, reply_markup=keyboard)

    except Exception:
        log.exception("Ошибка распознавания файла")
        await status.edit_text("Не удалось обработать этот файл.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Необработанная ошибка", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Что-то пошло не так. Попробуй ещё раз чуть позже.",
            )
        except Exception:
            log.warning("Не удалось уведомить пользователя об ошибке", exc_info=True)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не добавлена")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(300)
        .write_timeout(300)
        .pool_timeout(60)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CallbackQueryHandler(help_cb, pattern=r"^help$"))
    application.add_handler(
        CallbackQueryHandler(track_mp3_cb, pattern=r"^track_mp3_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(media_cb, pattern=r"^(video|audio)_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(recognize_url_cb, pattern=r"^recognize_url$")
    )
    application.add_handler(
        CallbackQueryHandler(recognized_mp3_cb, pattern=r"^recognized_mp3$")
    )
    application.add_handler(MessageHandler(
        filters.AUDIO | filters.VOICE | filters.VIDEO | filters.Document.ALL,
        recognize_upload,
    ))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()