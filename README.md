# Media Downloader Bot

Telegram-бот для поиска музыки, отправки MP3 с названием, исполнителем и обложкой, а также загрузки разрешённого контента из YouTube, TikTok и Instagram.

## Файлы проекта

- `bot.py` — код бота
- `requirements.txt` — Python-зависимости
- `Dockerfile` — запуск на Railway

## Переменные Railway

Обязательно:

```text
TELEGRAM_BOT_TOKEN=токен_бота_из_BotFather
```

Для распознавания музыки:

```text
AUDD_API_TOKEN=токен_AudD
```

Дополнительно:

```text
MAX_UPLOAD_MB=49
MAX_RECOGNITION_MB=18
MAX_PARALLEL_DOWNLOADS=2
SEARCH_LIMIT=8
DEFAULT_MP3_QUALITY=320
CACHE_TTL_SECONDS=21600
```

При ограничениях YouTube или Instagram могут понадобиться cookies:

```text
YTDLP_COOKIES_B64=cookies_в_base64
```

Используйте бот только для контента, который вам разрешено скачивать.
