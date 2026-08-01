
import asyncio
import html
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import mplfinance as mpf
import numpy as np
import pandas as pd
import requests
import feedparser
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("xau-signal-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TD_KEY = os.environ["TWELVE_DATA_API_KEY"]
TE_KEY = os.getenv("TRADING_ECONOMICS_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "true").lower() == "true"
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "35"))
AI_BLOCK_OPPOSITE = os.getenv("AI_BLOCK_OPPOSITE", "true").lower() == "true"
SYMBOL = os.getenv("MARKET_SYMBOL", "XAU/USD")
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))

SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "3"))
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "7"))
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "6"))
SIGNAL_MAX_HOURS = int(os.getenv("SIGNAL_MAX_HOURS", "48"))
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "8"))
SWING_HOURS = int(os.getenv("SWING_HOURS", "36"))
NEWS_BLOCK_BEFORE = int(os.getenv("NEWS_BLOCK_BEFORE_MINUTES", "45"))
NEWS_CONFIRM_AFTER = int(os.getenv("NEWS_CONFIRM_AFTER_MINUTES", "30"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SMC_LOOKBACK = int(os.getenv("SMC_LOOKBACK", "80"))
SMC_REQUIRE_CONFIRMATION = os.getenv("SMC_REQUIRE_CONFIRMATION", "true").lower() == "true"

OFFICIAL_NEWS_SCAN_MINUTES = int(os.getenv("OFFICIAL_NEWS_SCAN_MINUTES", "5"))
OFFICIAL_NEWS_SEND_EXISTING = os.getenv("OFFICIAL_NEWS_SEND_EXISTING", "false").lower() == "true"
OFFICIAL_NEWS_MAX_AGE_HOURS = int(os.getenv("OFFICIAL_NEWS_MAX_AGE_HOURS", "24"))

OFFICIAL_FEEDS = {
    "Federal Reserve — Monetary Policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "Federal Reserve — Speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "BLS — Employment Situation": "https://www.bls.gov/feed/empsit.rss",
    "BLS — CPI": "https://www.bls.gov/feed/cpi.rss",
    "BLS — PPI": "https://www.bls.gov/feed/ppi.rss",
    "BEA — Economic Releases": "https://apps.bea.gov/rss/rss.xml",
}

OFFICIAL_NEWS_KEYWORDS = (
    "fomc", "federal funds", "interest rate", "monetary policy", "inflation",
    "consumer price", "producer price", "cpi", "ppi", "payroll", "employment",
    "unemployment", "job openings", "labor market", "wages", "average hourly",
    "gross domestic product", "gdp", "personal consumption", "pce",
    "personal income", "retail", "economic outlook", "treasury yield",
    "rate cut", "rate hike", "balance sheet", "quantitative tightening",
    "powell", "chair", "governor", "vice chair"
)

STATE_FILE = Path("state.json")
CHART_FILE = Path("latest_signal.png")
USER_STATE_FILE = Path("users.json")
ACTIVE_SIGNALS_FILE = Path("active_signals.json")

SELECT_LANGUAGE, ENTER_CAPITAL = range(2)

LANG_KEYBOARD = ReplyKeyboardMarkup(
    [["🇷🇺 Русский", "🇺🇿 O‘zbekcha"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

RU_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Анализ", "📰 Новости"],
        ["✅ Статус", "🧪 Тест-сигнал"],
        ["👤 Профиль", "🏛 Официальные новости"],
        ["⚙️ Изменить язык/капитал"],
    ],
    resize_keyboard=True,
)

UZ_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Tahlil", "📰 Yangiliklar"],
        ["✅ Holat", "🧪 Test-signal"],
        ["👤 Profil", "🏛 Rasmiy yangiliklar"],
        ["⚙️ Til/kapitalni o‘zgartirish"],
    ],
    resize_keyboard=True,
)


def menu_for(lang: str) -> ReplyKeyboardMarkup:
    return UZ_MENU if lang == "uz" else RU_MENU

TEXTS = {
    "ru": {
        "welcome": "👋 Добро пожаловать! Выберите язык:",
        "ask_capital": "💵 Введите ваш торговый капитал в долларах, например: 100 или 2500",
        "bad_capital": "Введите корректную сумму от 1 до 1 000 000 долларов.",
        "saved": "✅ Готово! Капитал сохранён: ${capital:,.2f}\n📌 Рекомендованный минимальный лот: {lot:.2f}\n🔔 Теперь вы будете получать персональные сигналы.",
        "cancelled": "Настройка отменена.",
        "not_configured": "Сначала нажмите /start и настройте язык и капитал.",
        "profile": "Язык: Русский\nКапитал: ${capital:,.2f}\nРекомендованный минимальный лот: {lot:.2f}",
        "personal_plan": "👤 Персональный план\nКапитал: ${capital:,.2f}\nРекомендованный минимальный лот: {lot:.2f}\nРиск: минимальный",
    },
    "uz": {
        "welcome": "👋 Xush kelibsiz! Tilni tanlang:",
        "ask_capital": "💵 Savdo kapitalingizni dollarda kiriting, masalan: 100 yoki 2500",
        "bad_capital": "1 dan 1 000 000 dollargacha to‘g‘ri summa kiriting.",
        "saved": "✅ Tayyor! Kapital saqlandi: ${capital:,.2f}\n📌 Tavsiya etilgan minimal lot: {lot:.2f}\n🔔 Endi siz shaxsiy signallar olasiz.",
        "cancelled": "Sozlash bekor qilindi.",
        "not_configured": "Avval /start ni bosing va til hamda kapitalni sozlang.",
        "profile": "Til: O‘zbekcha\nKapital: ${capital:,.2f}\nTavsiya etilgan minimal lot: {lot:.2f}",
        "personal_plan": "👤 Shaxsiy reja\nKapital: ${capital:,.2f}\nTavsiya etilgan minimal lot: {lot:.2f}\nRisk: minimal",
    }
}

HIGH_IMPACT_WORDS = (
    "non farm", "non-farm", "nonfarm", "payroll", "nfp",
    "cpi", "core cpi", "consumer price", "inflation",
    "ppi", "core ppi", "producer price",
    "pce", "core pce", "personal consumption",
    "fomc", "fed interest", "interest rate", "powell", "federal reserve",
    "unemployment", "jobless", "initial claims", "continuing claims",
    "gdp", "retail sales", "employment", "average hourly", "adp", "jolts",
    "pmi", "services pmi", "manufacturing pmi", "composite pmi",
    "s&p global", "ism manufacturing", "ism services", "ism non-manufacturing",
    "consumer confidence", "michigan sentiment"
)

@dataclass
class NewsContext:
    blocked: bool
    bias: str
    title: str
    explanation: str
    upcoming: list
    recent: list

@dataclass
class SMCContext:
    bullish_bos: bool
    bearish_bos: bool
    bullish_choch: bool
    bearish_choch: bool
    bullish_sweep: bool
    bearish_sweep: bool
    bullish_fvg: Optional[tuple[float, float]]
    bearish_fvg: Optional[tuple[float, float]]
    bullish_ob: Optional[tuple[float, float]]
    bearish_ob: Optional[tuple[float, float]]
    summary: list[str]


@dataclass
class Signal:
    side: str
    score: int
    entry_low: float
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    invalidation: str
    reasons: list[str]
    strategies: list[str]
    price: float
    fib_low: float
    fib_high: float
    news: NewsContext
    smc: SMCContext
    ai_verdict: str = "UNAVAILABLE"
    ai_confidence: int = 0
    ai_summary: str = "OpenAI-анализ не выполнялся."
    trade_style: str = "INTRADAY"
    expected_hold_hours: int = 8
    expires_at: str = ""


def td_candles(interval: str, outputsize: int = 500) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TD_KEY,
        "format": "JSON",
        "timezone": "UTC",
    }
    data = requests.get(url, params=params, timeout=25).json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("datetime").set_index("datetime").dropna()
    return df


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def rsi(s: pd.Series, length: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    a = atr(df, length)
    plus_di = 100 * plus_dm.ewm(alpha=1/length, adjust=False).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1/length, adjust=False).mean() / a
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1/length, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ema20"] = ema(x["close"], 20)
    x["ema50"] = ema(x["close"], 50)
    x["ema200"] = ema(x["close"], 200)
    x["rsi"] = rsi(x["close"])
    x["atr"] = atr(x)
    x["adx"] = adx(x)
    fast = ema(x["close"], 12)
    slow = ema(x["close"], 26)
    x["macd"] = fast - slow
    x["macd_signal"] = ema(x["macd"], 9)
    mid = x["close"].rolling(20).mean()
    std = x["close"].rolling(20).std()
    x["bb_upper"] = mid + 2 * std
    x["bb_lower"] = mid - 2 * std
    return x.dropna()


def swing_range(df: pd.DataFrame, lookback: int = 80) -> tuple[float, float]:
    part = df.tail(lookback)
    low_i = part["low"].idxmin()
    high_i = part["high"].idxmax()
    low = float(part.loc[low_i, "low"])
    high = float(part.loc[high_i, "high"])
    return low, high


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_ff_datetime(date_text: str, time_text: str) -> Optional[datetime]:
    """Parse Forex Factory/FairEconomy calendar time as America/New_York."""
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip().lower().replace(" ", "")
    if not date_text or not time_text or time_text in {"all day", "tentative", ""}:
        return None
    parsed_date = None
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed_date = datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        return None
    parsed_time = None
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            parsed_time = datetime.strptime(time_text, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None
    ny = ZoneInfo("America/New_York")
    return datetime.combine(parsed_date, parsed_time, tzinfo=ny).astimezone(timezone.utc)


def fetch_faireconomy_events() -> list[dict]:
    """No-key Forex Factory/FairEconomy calendar feed."""
    urls = (
        "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "application/xml,text/xml,*/*",
    }
    events: list[dict] = []
    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for node in root.findall(".//event"):
                get = lambda tag: (node.findtext(tag) or "").strip()
                country = get("country").upper()
                if country not in {"USD", "US", "USA", "UNITED STATES"}:
                    continue
                dt = _parse_ff_datetime(get("date"), get("time"))
                if dt is None:
                    continue
                impact_text = get("impact").lower()
                importance = 3 if "high" in impact_text else 2 if "medium" in impact_text else 1
                title = get("title")
                events.append({
                    "Event": title,
                    "Category": title,
                    "Country": "United States",
                    "Date": dt.isoformat(),
                    "Importance": importance,
                    "Actual": get("actual"),
                    "Forecast": get("forecast"),
                    "Previous": get("previous"),
                    "Source": "FairEconomy",
                    "URL": get("url"),
                })
        except Exception as exc:
            log.warning("FairEconomy calendar failed: %s — %s", url, exc)
    return events


def _tv_importance(value) -> int:
    if isinstance(value, (int, float)):
        value = int(value)
        return 3 if value >= 3 else 2 if value == 2 else 1
    text = str(value or "").lower()
    return 3 if "high" in text else 2 if "medium" in text else 1


def _tv_datetime(value) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        # TradingView may return seconds or milliseconds.
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    if not value:
        return None
    try:
        return parse_dt(str(value))
    except Exception:
        return None


def fetch_tradingview_events() -> list[dict]:
    """No-key TradingView economic-calendar fallback.

    The parser intentionally accepts several response shapes because the public
    endpoint has changed field names in the past.
    """
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    date_to = (now + timedelta(days=8)).strftime("%Y-%m-%dT23:59:59.999Z")
    url = "https://economic-calendar.tradingview.com/events"
    params = {
        "from": date_from,
        "to": date_to,
        "countries": "US",
        "minImportance": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/markets/currencies/economic-calendar/",
        "Accept": "application/json,text/plain,*/*",
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=25)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("TradingView calendar failed: %s", exc)
        return []

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("result") or payload.get("events") or payload.get("data") or []
    else:
        rows = []

    events: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        country = str(row.get("country") or row.get("countryCode") or row.get("currency") or "").upper()
        if country and country not in {"US", "USA", "USD", "UNITED STATES"}:
            continue
        title = str(row.get("title") or row.get("name") or row.get("event") or "").strip()
        dt = _tv_datetime(row.get("date") or row.get("time") or row.get("timestamp") or row.get("datetime"))
        if not title or dt is None:
            continue
        events.append({
            "Event": title,
            "Category": str(row.get("category") or title),
            "Country": "United States",
            "Date": dt.isoformat(),
            "Importance": _tv_importance(row.get("importance") or row.get("impact")),
            "Actual": row.get("actual") if row.get("actual") not in (None, "") else "",
            "Forecast": row.get("forecast") if row.get("forecast") not in (None, "") else "",
            "Previous": row.get("previous") if row.get("previous") not in (None, "") else "",
            "Source": "TradingView calendar",
            "URL": "",
        })
    return events


def economic_events() -> list[dict]:
    """Load and merge calendar events from all available sources."""
    events: list[dict] = []
    if TE_KEY:
        try:
            today = datetime.now(timezone.utc).date() - timedelta(days=1)
            end = today + timedelta(days=9)
            url = (
                "https://api.tradingeconomics.com/calendar/country/united%20states/"
                f"{today.isoformat()}/{end.isoformat()}"
            )
            params = {"c": TE_KEY, "importance": 1, "values": "true", "f": "json"}
            response = requests.get(url, params=params, timeout=25)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                for item in data:
                    item.setdefault("Source", "Trading Economics")
                events.extend(data)
        except Exception as exc:
            log.warning("Trading Economics failed: %s", exc)

    # Do not depend on one provider: merge two no-key calendars.
    events.extend(fetch_faireconomy_events())
    events.extend(fetch_tradingview_events())

    unique: dict[tuple[str, str], dict] = {}
    for event in events:
        try:
            dt = parse_dt(str(event.get("Date", ""))).replace(second=0, microsecond=0)
        except Exception:
            continue
        title = str(event.get("Event") or event.get("Category") or "").strip()
        if not title:
            continue
        key = (title.lower(), dt.isoformat())
        current = unique.get(key)
        # Prefer the copy containing actual/forecast/previous values.
        score = sum(bool(event.get(x) not in (None, "")) for x in ("Actual", "Forecast", "Previous"))
        current_score = sum(bool(current and current.get(x) not in (None, "")) for x in ("Actual", "Forecast", "Previous"))
        if current is None or score > current_score:
            event["Date"] = dt.isoformat()
            unique[key] = event
    result = sorted(unique.values(), key=lambda e: e.get("Date", ""))
    log.info("Economic calendar loaded %s events (%s relevant)", len(result), sum(event_relevant(e) for e in result))
    return result

def event_relevant(event: dict) -> bool:
    text = f"{event.get('Event','')} {event.get('Category','')}".lower()
    # Medium-impact releases are included when they are directly relevant to USD/XAU,
    # so S&P Global PMI is not silently discarded.
    return int(event.get("Importance", 0) or 0) >= 2 and any(w in text for w in HIGH_IMPACT_WORDS)


def value_num(event: dict, field: str) -> Optional[float]:
    direct = event.get(field + "Value")
    if isinstance(direct, (int, float)):
        return float(direct)
    raw = str(event.get(field, "")).replace(",", "").strip()
    mult = 1.0
    if raw.endswith("K"):
        mult, raw = 1_000.0, raw[:-1]
    elif raw.endswith("M"):
        mult, raw = 1_000_000.0, raw[:-1]
    elif raw.endswith("B"):
        mult, raw = 1_000_000_000.0, raw[:-1]
    raw = raw.replace("%", "")
    try:
        return float(raw) * mult
    except ValueError:
        return None


def news_direction(event: dict) -> tuple[str, str]:
    actual = value_num(event, "Actual")
    forecast = value_num(event, "Forecast")
    title = str(event.get("Event", "")).lower()
    if actual is None or forecast is None or forecast == 0:
        return "neutral", "нет достаточных числовых данных"

    surprise = (actual - forecast) / abs(forecast)
    if abs(surprise) < 0.02:
        return "neutral", "факт близок к прогнозу"

    lower_is_usd_positive = any(k in title for k in ("unemployment", "jobless", "claims"))
    usd_positive = surprise < 0 if lower_is_usd_positive else surprise > 0

    if usd_positive:
        return "bearish_gold", "данные сильнее ожиданий поддерживают USD и могут давить на золото"
    return "bullish_gold", "данные слабее ожиданий могут ослаблять USD и поддерживать золото"


def get_news_context() -> NewsContext:
    now = datetime.now(timezone.utc)
    events = [e for e in economic_events() if event_relevant(e)]
    upcoming, recent = [], []
    blocked = False
    biases = []
    title = "Нет важных новостей рядом"
    explanation = "Сигнал оценивается главным образом по цене."

    for e in events:
        dt = parse_dt(e["Date"])
        mins = (dt - now).total_seconds() / 60
        item = {**e, "_dt": dt}
        if 0 <= mins <= 360:
            upcoming.append(item)
        if -180 <= mins < 0:
            recent.append(item)
        if 0 <= mins <= NEWS_BLOCK_BEFORE:
            blocked = True
            title = e.get("Event", "Важная новость США")
            explanation = f"До новости около {int(mins)} мин. Новый сигнал заблокирован."
        if -NEWS_CONFIRM_AFTER <= mins < 0:
            blocked = True
            title = e.get("Event", "Важная новость США")
            explanation = "Новость только что вышла. Бот ждёт подтверждение закрытием H1."

    for e in recent:
        bias, reason = news_direction(e)
        biases.append(bias)
        if bias != "neutral":
            title = e.get("Event", "Новость США")
            explanation = reason

    bull = biases.count("bullish_gold")
    bear = biases.count("bearish_gold")
    bias = "bullish_gold" if bull > bear else "bearish_gold" if bear > bull else "neutral"
    return NewsContext(blocked, bias, title, explanation, upcoming, recent)


def pivot_highs(series: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    flags = pd.Series(False, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i-left:i+right+1]
        if series.iloc[i] == window.max():
            flags.iloc[i] = True
    return flags


def pivot_lows(series: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    flags = pd.Series(False, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i-left:i+right+1]
        if series.iloc[i] == window.min():
            flags.iloc[i] = True
    return flags


def latest_zone(df: pd.DataFrame, bullish: bool, lookback: int = 40) -> Optional[tuple[float, float]]:
    part = df.tail(lookback)
    for i in range(len(part) - 2, 1, -1):
        candle = part.iloc[i]
        nxt = part.iloc[i + 1]
        if bullish and candle["close"] < candle["open"] and nxt["close"] > candle["high"]:
            return float(candle["low"]), float(candle["high"])
        if not bullish and candle["close"] > candle["open"] and nxt["close"] < candle["low"]:
            return float(candle["low"]), float(candle["high"])
    return None


def latest_fvg(df: pd.DataFrame, bullish: bool, lookback: int = 50) -> Optional[tuple[float, float]]:
    part = df.tail(lookback)
    for i in range(len(part) - 1, 1, -1):
        first = part.iloc[i - 2]
        third = part.iloc[i]
        if bullish and third["low"] > first["high"]:
            return float(first["high"]), float(third["low"])
        if not bullish and third["high"] < first["low"]:
            return float(third["high"]), float(first["low"])
    return None


def analyze_smc(df: pd.DataFrame) -> SMCContext:
    part = df.tail(max(SMC_LOOKBACK, 30)).copy()
    highs = pivot_highs(part["high"])
    lows = pivot_lows(part["low"])
    swing_highs = part.loc[highs, "high"]
    swing_lows = part.loc[lows, "low"]

    last = part.iloc[-1]
    prev = part.iloc[-2]
    prev_high = float(swing_highs.iloc[-1]) if len(swing_highs) else float(part["high"].iloc[:-1].max())
    prev_low = float(swing_lows.iloc[-1]) if len(swing_lows) else float(part["low"].iloc[:-1].min())

    bullish_bos = float(last["close"]) > prev_high
    bearish_bos = float(last["close"]) < prev_low

    recent_trend_up = part["close"].iloc[-10:-1].mean() > part["close"].iloc[-20:-10].mean()
    bullish_choch = (not recent_trend_up) and bullish_bos
    bearish_choch = recent_trend_up and bearish_bos

    bullish_sweep = float(last["low"]) < prev_low and float(last["close"]) > prev_low
    bearish_sweep = float(last["high"]) > prev_high and float(last["close"]) < prev_high

    bullish_fvg = latest_fvg(part, True)
    bearish_fvg = latest_fvg(part, False)
    bullish_ob = latest_zone(part, True)
    bearish_ob = latest_zone(part, False)

    summary = []
    if bullish_bos:
        summary.append("BOS вверх: цена закрылась выше предыдущего swing high")
    if bearish_bos:
        summary.append("BOS вниз: цена закрылась ниже предыдущего swing low")
    if bullish_choch:
        summary.append("CHoCH вверх: возможная смена медвежьей структуры")
    if bearish_choch:
        summary.append("CHoCH вниз: возможная смена бычьей структуры")
    if bullish_sweep:
        summary.append("снята ликвидность под минимумом с возвратом выше")
    if bearish_sweep:
        summary.append("снята ликвидность над максимумом с возвратом ниже")
    if bullish_ob:
        summary.append(f"бычий Order Block {bullish_ob[0]:.2f}–{bullish_ob[1]:.2f}")
    if bearish_ob:
        summary.append(f"медвежий Order Block {bearish_ob[0]:.2f}–{bearish_ob[1]:.2f}")
    if bullish_fvg:
        summary.append(f"бычий FVG {bullish_fvg[0]:.2f}–{bullish_fvg[1]:.2f}")
    if bearish_fvg:
        summary.append(f"медвежий FVG {bearish_fvg[0]:.2f}–{bearish_fvg[1]:.2f}")

    return SMCContext(
        bullish_bos, bearish_bos, bullish_choch, bearish_choch,
        bullish_sweep, bearish_sweep, bullish_fvg, bearish_fvg,
        bullish_ob, bearish_ob, summary
    )


def price_in_zone(price: float, zone: Optional[tuple[float, float]], tolerance: float) -> bool:
    if not zone:
        return False
    return zone[0] - tolerance <= price <= zone[1] + tolerance


def build_signal(h1: pd.DataFrame, h4: pd.DataFrame, d1: pd.DataFrame, news: NewsContext) -> Optional[Signal]:
    a = h1.iloc[-1]
    b = h4.iloc[-1]
    d = d1.iloc[-1]
    price = float(a["close"])
    unit = float(a["atr"])
    fib_low, fib_high = swing_range(h1)
    span = fib_high - fib_low
    if span <= 0 or not math.isfinite(unit) or unit <= 0:
        return None

    smc = analyze_smc(h1)
    bull_score = 0
    bear_score = 0
    bull_reasons, bear_reasons = [], []
    bull_strategies, bear_strategies = [], []

    # D1 is the macro filter, H4 is the directional filter.
    if d["close"] > d["ema50"] > d["ema200"]:
        bull_score += 2
        bull_reasons.append("D1 подтверждает долгосрочный бычий уклон")
        bull_strategies.append("Макро-тренд D1 по EMA")
    elif d["close"] < d["ema50"] < d["ema200"]:
        bear_score += 2
        bear_reasons.append("D1 подтверждает долгосрочный медвежий уклон")
        bear_strategies.append("Макро-тренд D1 по EMA")

    if b["close"] > b["ema50"] > b["ema200"]:
        bull_score += 2
        bull_reasons.append("H4 выше EMA50 и EMA200")
        bull_strategies.append("Тренд H4 по EMA")
    if b["close"] < b["ema50"] < b["ema200"]:
        bear_score += 2
        bear_reasons.append("H4 ниже EMA50 и EMA200")
        bear_strategies.append("Тренд H4 по EMA")

    # H1 momentum.
    if a["close"] > a["ema20"] > a["ema50"]:
        bull_score += 2
        bull_reasons.append("H1 удерживается выше EMA20 и EMA50")
        bull_strategies.append("Тренд H1 по EMA")
    if a["close"] < a["ema20"] < a["ema50"]:
        bear_score += 2
        bear_reasons.append("H1 удерживается ниже EMA20 и EMA50")
        bear_strategies.append("Тренд H1 по EMA")

    if a["macd"] > a["macd_signal"]:
        bull_score += 1
        bull_reasons.append("MACD подтверждает импульс вверх")
        bull_strategies.append("MACD momentum")
    else:
        bear_score += 1
        bear_reasons.append("MACD подтверждает импульс вниз")
        bear_strategies.append("MACD momentum")

    if 50 <= a["rsi"] <= 68:
        bull_score += 1
        bull_reasons.append(f"RSI {a['rsi']:.0f}: бычий без сильной перекупленности")
        bull_strategies.append("RSI filter")
    if 32 <= a["rsi"] <= 50:
        bear_score += 1
        bear_reasons.append(f"RSI {a['rsi']:.0f}: медвежий без сильной перепроданности")
        bear_strategies.append("RSI filter")

    # Fibonacci retracement.
    bull_fib_zone = (fib_high - 0.618 * span, fib_high - 0.382 * span)
    bear_fib_zone = (fib_low + 0.382 * span, fib_low + 0.618 * span)
    if price_in_zone(price, bull_fib_zone, unit * 0.15):
        bull_score += 2
        bull_reasons.append("цена находится в Fibonacci-зоне 0.382–0.618")
        bull_strategies.append("Fibonacci retracement")
    if price_in_zone(price, bear_fib_zone, unit * 0.15):
        bear_score += 2
        bear_reasons.append("цена находится в Fibonacci-зоне 0.382–0.618")
        bear_strategies.append("Fibonacci retracement")

    # Smart Money Concepts.
    if smc.bullish_bos or smc.bullish_choch:
        bull_score += 2
        bull_reasons.append("структура Smart Money подтверждает движение вверх (BOS/CHoCH)")
        bull_strategies.append("Smart Money: BOS/CHoCH")
    if smc.bearish_bos or smc.bearish_choch:
        bear_score += 2
        bear_reasons.append("структура Smart Money подтверждает движение вниз (BOS/CHoCH)")
        bear_strategies.append("Smart Money: BOS/CHoCH")

    if smc.bullish_sweep:
        bull_score += 2
        bull_reasons.append("снята sell-side liquidity под локальным минимумом")
        bull_strategies.append("Smart Money: Liquidity Sweep")
    if smc.bearish_sweep:
        bear_score += 2
        bear_reasons.append("снята buy-side liquidity над локальным максимумом")
        bear_strategies.append("Smart Money: Liquidity Sweep")

    if price_in_zone(price, smc.bullish_ob, unit * 0.25):
        bull_score += 2
        bull_reasons.append("цена тестирует бычий Order Block")
        bull_strategies.append("Smart Money: Order Block")
    if price_in_zone(price, smc.bearish_ob, unit * 0.25):
        bear_score += 2
        bear_reasons.append("цена тестирует медвежий Order Block")
        bear_strategies.append("Smart Money: Order Block")

    if price_in_zone(price, smc.bullish_fvg, unit * 0.20):
        bull_score += 1
        bull_reasons.append("цена вернулась в бычий Fair Value Gap")
        bull_strategies.append("Smart Money: Fair Value Gap")
    if price_in_zone(price, smc.bearish_fvg, unit * 0.20):
        bear_score += 1
        bear_reasons.append("цена вернулась в медвежий Fair Value Gap")
        bear_strategies.append("Smart Money: Fair Value Gap")

    if news.bias == "bullish_gold":
        bull_score += 1
        bull_reasons.append("официальные данные США дают потенциальную поддержку золоту")
        bull_strategies.append("Фундаментальный новостной фильтр")
    elif news.bias == "bearish_gold":
        bear_score += 1
        bear_reasons.append("официальные данные США поддерживают доллар")
        bear_strategies.append("Фундаментальный новостной фильтр")

    if news.blocked:
        return None

    side = "BUY" if bull_score > bear_score else "SELL"
    score = max(bull_score, bear_score)
    reasons = bull_reasons if side == "BUY" else bear_reasons
    strategies = bull_strategies if side == "BUY" else bear_strategies

    smc_confirmed = (
        smc.bullish_bos or smc.bullish_choch or smc.bullish_sweep
        or price_in_zone(price, smc.bullish_ob, unit * 0.25)
        or price_in_zone(price, smc.bullish_fvg, unit * 0.20)
    ) if side == "BUY" else (
        smc.bearish_bos or smc.bearish_choch or smc.bearish_sweep
        or price_in_zone(price, smc.bearish_ob, unit * 0.25)
        or price_in_zone(price, smc.bearish_fvg, unit * 0.20)
    )

    if score < MIN_SIGNAL_SCORE or (SMC_REQUIRE_CONFIRMATION and not smc_confirmed):
        return None

    if side == "BUY":
        preferred = smc.bullish_ob or smc.bullish_fvg
        if preferred:
            entry_low, entry_high = preferred
        else:
            entry_low, entry_high = price - 0.15 * unit, price + 0.10 * unit
        stop_reference = min(entry_low, fib_low)
        stop = stop_reference - 0.25 * unit
        risk = ((entry_low + entry_high) / 2) - stop
        tp1 = ((entry_low + entry_high) / 2) + max(1.2 * risk, 1.3 * unit)
        tp2 = ((entry_low + entry_high) / 2) + max(MIN_RR * risk, 2.0 * unit)
        invalidation = f"закрытие H1 ниже {stop:.2f}"
    else:
        preferred = smc.bearish_ob or smc.bearish_fvg
        if preferred:
            entry_low, entry_high = preferred
        else:
            entry_low, entry_high = price - 0.10 * unit, price + 0.15 * unit
        stop_reference = max(entry_high, fib_high)
        stop = stop_reference + 0.25 * unit
        risk = stop - ((entry_low + entry_high) / 2)
        tp1 = ((entry_low + entry_high) / 2) - max(1.2 * risk, 1.3 * unit)
        tp2 = ((entry_low + entry_high) / 2) - max(MIN_RR * risk, 2.0 * unit)
        invalidation = f"закрытие H1 выше {stop:.2f}"

    rr = abs(tp2 - ((entry_low + entry_high) / 2)) / max(abs(((entry_low + entry_high) / 2) - stop), 0.01)
    if rr < MIN_RR:
        return None

    # Signals are deliberately short-lived: intraday or at most two days.
    d1_aligned = (side == "BUY" and d["close"] > d["ema50"]) or (side == "SELL" and d["close"] < d["ema50"])
    h4_strong = float(b["adx"]) >= 24
    trade_style = "SWING 1–2 DAYS" if d1_aligned and h4_strong else "INTRADAY 4–8 HOURS"
    expected_hold_hours = min(SIGNAL_MAX_HOURS, SWING_HOURS if trade_style.startswith("SWING") else INTRADAY_HOURS)

    # Keep targets realistic for the chosen holding period.
    mid_entry = (entry_low + entry_high) / 2
    max_move = unit * (3.2 if trade_style.startswith("SWING") else 2.2)
    if side == "BUY":
        tp2 = min(tp2, mid_entry + max_move)
        tp1 = min(tp1, mid_entry + max_move * 0.65)
    else:
        tp2 = max(tp2, mid_entry - max_move)
        tp1 = max(tp1, mid_entry - max_move * 0.65)
    rr = abs(tp2 - mid_entry) / max(abs(mid_entry - stop), 0.01)
    if rr < 1.25:
        return None

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expected_hold_hours)).isoformat()
    strategies = list(dict.fromkeys(strategies))
    return Signal(
        side, score, entry_low, entry_high, stop, tp1, tp2, rr,
        invalidation, reasons[:9], strategies, price, fib_low, fib_high, news, smc,
        trade_style=trade_style, expected_hold_hours=expected_hold_hours, expires_at=expires_at
    )



def _extract_openai_text(payload: dict) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    parts: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def openai_json(instructions: str, user_input: str) -> Optional[dict]:
    """Call OpenAI only as a secondary analytical layer. Returns None on any failure."""
    if not OPENAI_ENABLED or not OPENAI_API_KEY:
        return None
    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": user_input,
        "temperature": 0.1,
        "max_output_tokens": 700,
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=OPENAI_TIMEOUT,
        )
        response.raise_for_status()
        raw = _extract_openai_text(response.json())
        if not raw:
            return None
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except Exception:
        log.exception("OpenAI analysis failed")
        return None


def market_snapshot(df: pd.DataFrame) -> dict:
    row = df.iloc[-1]
    return {
        "time_utc": df.index[-1].isoformat(),
        "close": round(float(row["close"]), 3),
        "ema20": round(float(row["ema20"]), 3),
        "ema50": round(float(row["ema50"]), 3),
        "ema200": round(float(row["ema200"]), 3),
        "rsi": round(float(row["rsi"]), 2),
        "macd": round(float(row["macd"]), 4),
        "macd_signal": round(float(row["macd_signal"]), 4),
        "atr": round(float(row["atr"]), 3),
        "adx": round(float(row["adx"]), 2),
        "bb_upper": round(float(row["bb_upper"]), 3),
        "bb_lower": round(float(row["bb_lower"]), 3),
    }


def ai_validate_signal(signal: Signal, h1: pd.DataFrame, h4: pd.DataFrame, d1: pd.DataFrame) -> dict:
    instructions = (
        "Ты независимый риск-фильтр для XAU/USD. Анализируй только переданные числовые данные, "
        "структуру рынка и подтвержденные новости. Не придумывай цены, факты или новости. "
        "Верни ТОЛЬКО корректный JSON без markdown: "
        '{"verdict":"BUY|SELL|WAIT","confidence":0,"summary":"до 350 символов",'
        '"risks":["..."],"news_conclusion":"до 250 символов"}. '
        "BUY/SELL означает согласие с направлением; WAIT — недостаточно подтверждений."
    )
    data = {
        "symbol": SYMBOL,
        "algorithm_signal": {
            "side": signal.side,
            "score": signal.score,
            "entry": [round(signal.entry_low, 3), round(signal.entry_high, 3)],
            "stop": round(signal.stop, 3),
            "tp1": round(signal.tp1, 3),
            "tp2": round(signal.tp2, 3),
            "rr": round(signal.rr, 2),
            "reasons": signal.reasons,
            "strategies": signal.strategies,
        },
        "H1": market_snapshot(h1),
        "H4": market_snapshot(h4),
        "D1": market_snapshot(d1),
        "holding_policy": {
            "style": signal.trade_style,
            "expected_hours": signal.expected_hold_hours,
            "absolute_max_hours": SIGNAL_MAX_HOURS,
        },
        "smart_money": signal.smc.summary,
        "news": {
            "blocked": signal.news.blocked,
            "bias": signal.news.bias,
            "title": signal.news.title,
            "explanation": signal.news.explanation,
        },
    }
    result = openai_json(instructions, json.dumps(data, ensure_ascii=False))
    if not result:
        return {"verdict": "UNAVAILABLE", "confidence": 0, "summary": "OpenAI временно недоступен; использован алгоритмический анализ.", "risks": []}
    verdict = str(result.get("verdict", "WAIT")).upper()
    if verdict not in {"BUY", "SELL", "WAIT"}:
        verdict = "WAIT"
    try:
        confidence = max(0, min(100, int(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": str(result.get("summary", ""))[:500],
        "risks": [str(x)[:180] for x in result.get("risks", [])[:3]],
        "news_conclusion": str(result.get("news_conclusion", ""))[:350],
    }


def ai_explain_news_item(item: dict) -> Optional[dict]:
    instructions = (
        "Ты анализируешь официальную макроэкономическую публикацию США для трейдера XAU/USD. "
        "Используй только переданный заголовок и текст. Не выдумывай отсутствующие цифры. "
        "Верни ТОЛЬКО JSON: "
        '{"impact":"BULLISH_GOLD|BEARISH_GOLD|NEUTRAL|UNCERTAIN",'
        '"conclusion":"краткий окончательный вывод до 400 символов",'
        '"usd_effect":"до 180 символов","gold_effect":"до 180 символов"}.'
    )
    data = {
        "source": item.get("source"),
        "title": item.get("title"),
        "summary": item.get("summary"),
        "published": item.get("published").isoformat() if item.get("published") else None,
    }
    return openai_json(instructions, json.dumps(data, ensure_ascii=False))

def chart_signal(h1: pd.DataFrame, signal: Signal, path: Path) -> None:
    plot = h1.tail(100).copy()
    plot.index = plot.index.tz_convert(TZ).tz_localize(None)
    renamed = plot.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    })

    ap = [
        mpf.make_addplot(plot["ema20"], width=1),
        mpf.make_addplot(plot["ema50"], width=1),
        mpf.make_addplot(plot["ema200"], width=1),
    ]

    fig, axes = mpf.plot(
        renamed,
        type="candle",
        addplot=ap,
        volume=False,
        figsize=(13, 8),
        title=f"XAU/USD {signal.side} | H1/H4/D1 | {signal.trade_style}",
        ylabel="Price",
        datetime_format="%d %b %H:%M",
        xrotation=15,
        tight_layout=True,
        returnfig=True,
    )
    ax = axes[0]

    # Entry area and trade levels.
    ax.axhspan(signal.entry_low, signal.entry_high, alpha=0.18)
    labelled_levels = [
        (signal.entry_low, "ENTRY LOW"),
        (signal.entry_high, "ENTRY HIGH"),
        (signal.stop, "STOP LOSS"),
        (signal.tp1, "TAKE PROFIT 1"),
        (signal.tp2, "TAKE PROFIT 2"),
    ]
    for level, label in labelled_levels:
        ax.axhline(level, linestyle="--", linewidth=1.2)
        ax.text(
            0.995, level, f" {label}: {level:.2f}",
            transform=ax.get_yaxis_transform(),
            ha="right", va="bottom", fontsize=8,
            bbox=dict(boxstyle="round,pad=0.2", alpha=0.65)
        )

    # Fibonacci levels.
    span = signal.fib_high - signal.fib_low
    for ratio in (0.382, 0.5, 0.618):
        level = signal.fib_low + ratio * span
        ax.axhline(level, linestyle=":", linewidth=0.8)
        ax.text(
            0.01, level, f"FIB {ratio:.3f}  {level:.2f}",
            transform=ax.get_yaxis_transform(),
            ha="left", va="bottom", fontsize=7
        )

    # Smart Money areas.
    zone = signal.smc.bullish_ob if signal.side == "BUY" else signal.smc.bearish_ob
    fvg = signal.smc.bullish_fvg if signal.side == "BUY" else signal.smc.bearish_fvg
    if zone:
        ax.axhspan(zone[0], zone[1], alpha=0.10)
        ax.text(0.02, (zone[0] + zone[1]) / 2, "ORDER BLOCK",
                transform=ax.get_yaxis_transform(), fontsize=8, va="center")
    if fvg:
        ax.axhspan(fvg[0], fvg[1], alpha=0.08)
        ax.text(0.18, (fvg[0] + fvg[1]) / 2, "FAIR VALUE GAP",
                transform=ax.get_yaxis_transform(), fontsize=8, va="center")

    fig.savefig(path, dpi=170, bbox_inches="tight")


def caption(signal: Signal) -> str:
    emoji = "🟢⬆️" if signal.side == "BUY" else "🔴⬇️"
    reasons = "\n".join(f"• {x}" for x in signal.reasons)
    strategies = "\n".join(f"• {x}" for x in signal.strategies)
    news_line = f"{signal.news.title}: {signal.news.explanation}"
    ai_icon = "✅" if signal.ai_verdict == signal.side else "⏸" if signal.ai_verdict == "WAIT" else "⚠️"
    ai_line = html.escape(signal.ai_summary or "Нет комментария")
    return (
        f"{emoji} <b>XAU/USD — {signal.side}</b>\n"
        f"⏱ Таймфреймы: H1 / H4 / D1\n"
        f"⌛ Формат: {signal.trade_style}; ожидаемо до {signal.expected_hold_hours} ч\n\n"
        f"<b>Зона входа:</b> {signal.entry_low:.2f}–{signal.entry_high:.2f}\n"
        f"<b>Stop Loss:</b> {signal.stop:.2f}\n"
        f"<b>Take Profit 1:</b> {signal.tp1:.2f}\n"
        f"<b>Take Profit 2:</b> {signal.tp2:.2f}\n"
        f"<b>Risk/Reward:</b> 1:{signal.rr:.1f}\n"
        f"<b>Сила сигнала:</b> {signal.score}/10+\n\n"
        f"🧠 <b>Стратегия сигнала:</b>\n{strategies}\n\n"
        f"<b>Почему бот дал сигнал:</b>\n{reasons}\n\n"
        f"📰 <b>Новостной фон:</b>\n{news_line}\n\n"
        f"🤖 <b>OpenAI-проверка:</b> {ai_icon} {signal.ai_verdict} ({signal.ai_confidence}%)\n{ai_line}\n\n"
        f"❌ <b>Отмена сценария:</b> {signal.invalidation}\n\n"
        f"⚠️ Алгоритмический сценарий не гарантирует прибыль. Соблюдайте риск-менеджмент."
    )


def clean_html(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())


def entry_datetime(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def official_news_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(word in text for word in OFFICIAL_NEWS_KEYWORDS)


def explain_official_news(title: str, summary: str) -> tuple[str, str]:
    text = f"{title} {summary}".lower()

    if any(x in text for x in ("rate hike", "higher interest", "inflation remains elevated",
                               "restrictive policy", "hawkish", "rates remain high")):
        return (
            "🔴 Возможное давление на золото",
            "Более жёсткая политика ФРС обычно поддерживает доходности и доллар, "
            "что может давить на золото. Направление нужно подтвердить движением цены."
        )
    if any(x in text for x in ("rate cut", "lower interest", "disinflation",
                               "inflation eased", "slower inflation", "dovish")):
        return (
            "🟢 Возможная поддержка золота",
            "Более мягкая политика или замедление инфляции может ослаблять доллар "
            "и доходности, что способно поддержать золото."
        )
    if any(x in text for x in ("payroll", "employment situation", "jobs", "unemployment", "wages")):
        return (
            "🟡 Важная новость по рынку труда",
            "Сильный рынок труда может поддержать доллар и давить на золото; "
            "слабые данные могут дать обратный эффект. Бот ждёт реакцию XAU/USD."
        )
    if any(x in text for x in ("consumer price", "producer price", "cpi", "ppi", "pce", "inflation")):
        return (
            "🟡 Важная инфляционная новость",
            "Инфляция выше ожиданий обычно уменьшает вероятность быстрого снижения ставок "
            "и может поддержать доллар. Более слабая инфляция чаще поддерживает золото."
        )
    if any(x in text for x in ("gross domestic product", "gdp", "personal income", "consumption")):
        return (
            "🟡 Важные данные по экономике США",
            "Сильные данные могут поддержать доллар; слабые — повысить ожидания смягчения ФРС "
            "и поддержать золото. Реакция рынка важнее одной цифры."
        )
    return (
        "⚪️ Новость может повлиять на USD и золото",
        "Влияние неоднозначное. Бот не объявляет BUY/SELL только по заголовку "
        "и ждёт подтверждение на H1."
    )


def fetch_official_news() -> list[dict]:
    items: list[dict] = []
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=OFFICIAL_NEWS_MAX_AGE_HOURS)

    headers = {"User-Agent": "XAU-Signal-Bot/1.0 (+Telegram official-news monitor)"}
    for source, url in OFFICIAL_FEEDS.items():
        try:
            response = requests.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            feed = feedparser.loads(response.content)
            for entry in feed.entries[:15]:
                title = clean_html(entry.get("title", ""))
                summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
                link = entry.get("link", "")
                published = entry_datetime(entry)
                if now - published > max_age or published > now + timedelta(hours=2):
                    continue
                if not official_news_relevant(title, summary):
                    continue
                uid = entry.get("id") or entry.get("guid") or link or f"{source}:{title}:{published.isoformat()}"
                items.append({
                    "id": str(uid),
                    "source": source,
                    "title": title,
                    "summary": summary[:800],
                    "link": link,
                    "published": published,
                })
        except Exception:
            log.exception("Official feed failed: %s", source)

    unique = {}
    for item in items:
        unique[item["id"]] = item
    return sorted(unique.values(), key=lambda x: x["published"])


def official_news_post(item: dict) -> str:
    impact, explanation = explain_official_news(item["title"], item["summary"])
    ai = item.get("ai_analysis") or {}
    ai_conclusion = str(ai.get("conclusion", "")).strip()
    ai_block = (f"\n🤖 <b>Итог OpenAI:</b>\n{html.escape(ai_conclusion)}\n" if ai_conclusion else "")
    local = item["published"].astimezone(TZ)
    summary = item["summary"]
    if len(summary) > 500:
        summary = summary[:497].rstrip() + "…"

    return (
        f"📰 <b>ОФИЦИАЛЬНАЯ НОВОСТЬ США</b>\n\n"
        f"<b>{item['title']}</b>\n\n"
        f"{summary}\n\n"
        f"<b>Возможное влияние:</b>\n"
        f"{impact}\n{explanation}\n"
        f"{ai_block}\n"
        f"<b>Источник:</b> {item['source']}\n"
        f"<b>Время Ташкента:</b> {local:%d.%m.%Y %H:%M}\n"
        f'<a href="{item["link"]}">Открыть официальный источник</a>\n\n'
        f"⚠️ Это анализ возможного влияния, а не гарантия направления цены."
    )


async def broadcast_text_to_users(app: Application, text: str) -> int:
    """Send a text post to every configured subscriber."""
    users = load_users()
    sent = 0
    for user_id, profile in users.items():
        if not isinstance(profile, dict) or not profile.get("capital"):
            continue
        try:
            await app.bot.send_message(
                chat_id=int(user_id),
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            sent += 1
        except Exception as exc:
            log.warning("Could not send text to user %s: %s", user_id, exc)
    return sent


async def scan_official_news(app: Application, manual: bool = False) -> str:
    items = await asyncio.to_thread(fetch_official_news)
    state = load_state()
    seen = set(state.get("seen_official_news", []))
    initialized = bool(state.get("official_news_initialized"))

    if not initialized and not OFFICIAL_NEWS_SEND_EXISTING and not manual:
        seen.update(item["id"] for item in items)
        state["seen_official_news"] = list(seen)[-1000:]
        state["official_news_initialized"] = True
        save_state(state)
        return f"Официальные ленты подключены. Запомнено текущих публикаций: {len(items)}."

    new_items = [item for item in items if item["id"] not in seen]
    sent = 0
    for item in new_items:
        if DRY_RUN and not manual:
            log.info("OFFICIAL NEWS DRY RUN\n%s", official_news_post(item))
        else:
            if OPENAI_ENABLED and OPENAI_API_KEY:
                item["ai_analysis"] = await asyncio.to_thread(ai_explain_news_item, item) or {}
            post = official_news_post(item)
            if CHAT_ID:
                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=post,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            await broadcast_text_to_users(app, post)
        seen.add(item["id"])
        sent += 1

    state["seen_official_news"] = list(seen)[-1000:]
    state["official_news_initialized"] = True
    save_state(state)
    return f"Новых официальных публикаций отправлено: {sent}."


async def officialnews_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        items = await asyncio.to_thread(fetch_official_news)
        if not items:
            await update.message.reply_text("За последние часы подходящих официальных новостей не найдено.")
            return
        latest = items[-1]
        if OPENAI_ENABLED and OPENAI_API_KEY:
            latest["ai_analysis"] = await asyncio.to_thread(ai_explain_news_item, latest) or {}
        await update.message.reply_text(
            official_news_post(latest),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as exc:
        log.exception("official news command failed")
        await update.message.reply_text(f"Ошибка официальных новостей: {exc}")


def load_users() -> dict:
    if not USER_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(USER_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_users(users: dict) -> None:
    USER_STATE_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def lot_for_capital(capital: float) -> float:
    if capital < 1000:
        return 0.01
    if capital < 10000:
        return 0.03
    if capital < 25000:
        return 0.05
    if capital < 50000:
        return 0.10
    return 0.20


def user_language(user_id: int) -> str:
    users = load_users()
    return users.get(str(user_id), {}).get("language", "ru")


def localized_caption(signal: Signal, lang: str, capital: float, lot: float) -> str:
    if lang == "uz":
        emoji = "🟢⬆️" if signal.side == "BUY" else "🔴⬇️"
        side = "SOTIB OLISH" if signal.side == "BUY" else "SOTISH"
        reasons = "\n".join(f"• {x}" for x in signal.reasons)
        strategies = "\n".join(f"• {x}" for x in signal.strategies)
        return (
            f"{emoji} <b>XAU/USD — {side}</b>\n"
            f"⏱ Taymfreym: H1 / H4 / D1\n"
            f"⌛ Format: {signal.trade_style}; taxminan {signal.expected_hold_hours} soatgacha\n\n"
            f"<b>Kirish zonasi:</b> {signal.entry_low:.2f}–{signal.entry_high:.2f}\n"
            f"<b>Stop Loss:</b> {signal.stop:.2f}\n"
            f"<b>Take Profit 1:</b> {signal.tp1:.2f}\n"
            f"<b>Take Profit 2:</b> {signal.tp2:.2f}\n"
            f"<b>Risk/Reward:</b> 1:{signal.rr:.1f}\n"
            f"<b>Signal kuchi:</b> {signal.score}/10+\n\n"
            f"👤 <b>Shaxsiy reja:</b>\n"
            f"Kapital: ${capital:,.2f}\n"
            f"Tavsiya etilgan minimal lot: <b>{lot:.2f}</b>\n"
            f"Risk: minimal\n\n"
            f"🧠 <b>Signal strategiyasi:</b>\n{strategies}\n\n"
            f"<b>Nima uchun signal berildi:</b>\n{reasons}\n\n"
            f"📰 <b>Yangiliklar foni:</b>\n"
            f"{signal.news.title}: {signal.news.explanation}\n\n"
            f"❌ <b>Stsenariy bekor bo‘ladi:</b> {signal.invalidation}\n\n"
            f"⚠️ Bu foyda kafolati emas. Lot hajmi broker shartlari va stop masofasiga qarab farq qilishi mumkin."
        )

    base = caption(signal)
    personal = (
        f"\n\n👤 <b>Персональный план:</b>\n"
        f"Капитал: ${capital:,.2f}\n"
        f"Рекомендованный минимальный лот: <b>{lot:.2f}</b>\n"
        f"Риск: минимальный"
    )
    return base + personal


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        TEXTS["ru"]["welcome"],
        reply_markup=LANG_KEYBOARD
    )
    return SELECT_LANGUAGE


async def language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text in {"O‘zbekcha", "🇺🇿 O‘zbekcha"}:
        lang = "uz"
    else:
        lang = "ru"
    context.user_data["language"] = lang
    await update.message.reply_text(
        TEXTS[lang]["ask_capital"],
        reply_markup=ReplyKeyboardRemove()
    )
    return ENTER_CAPITAL


async def capital_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("language", "ru")
    raw = (update.message.text or "").replace(",", ".").replace("$", "").strip()
    try:
        capital = float(raw)
    except ValueError:
        await update.message.reply_text(TEXTS[lang]["bad_capital"])
        return ENTER_CAPITAL

    if not 1 <= capital <= 1_000_000:
        await update.message.reply_text(TEXTS[lang]["bad_capital"])
        return ENTER_CAPITAL

    lot = lot_for_capital(capital)
    users = load_users()
    user_id = str(update.effective_user.id)
    users[user_id] = {
        "language": lang,
        "capital": capital,
        "lot": lot,
        "chat_id": update.effective_chat.id,
        "username": update.effective_user.username or "",
        "first_name": update.effective_user.first_name or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_users(users)

    await update.message.reply_text(
        TEXTS[lang]["saved"].format(capital=capital, lot=lot),
        reply_markup=menu_for(lang),
    )
    return ConversationHandler.END


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("language", "ru")
    await update.message.reply_text(
        TEXTS[lang]["cancelled"],
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = load_users()
    profile = users.get(str(update.effective_user.id))
    if not profile:
        await update.message.reply_text(TEXTS["ru"]["not_configured"])
        return
    lang = profile.get("language", "ru")
    await update.message.reply_text(
        TEXTS[lang]["profile"].format(
            capital=float(profile["capital"]),
            lot=float(profile["lot"])
        )
    )


async def broadcast_personal_signal(app: Application, signal: Signal, chart_path: Path) -> int:
    users = load_users()
    sent = 0
    for profile in users.values():
        chat_id = profile.get("chat_id")
        if not chat_id:
            continue
        lang = profile.get("language", "ru")
        capital = float(profile.get("capital", 0))
        lot = float(profile.get("lot", lot_for_capital(capital)))
        try:
            with chart_path.open("rb") as photo:
                await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=localized_caption(signal, lang, capital, lot),
                    parse_mode=ParseMode.HTML,
                )
            sent += 1
        except Exception:
            log.exception("Failed to send personal signal to %s", chat_id)
    return sent


def load_active_signals() -> list[dict]:
    if not ACTIVE_SIGNALS_FILE.exists():
        return []
    try:
        data = json.loads(ACTIVE_SIGNALS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_active_signals(signals: list[dict]) -> None:
    ACTIVE_SIGNALS_FILE.write_text(
        json.dumps(signals, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def register_active_signal(signal: Signal) -> None:
    signals = load_active_signals()
    signal_id = f"{int(datetime.now(timezone.utc).timestamp())}-{signal.side}-{signal.price:.2f}"
    signals.append({
        "id": signal_id,
        "side": signal.side,
        "entry_low": signal.entry_low,
        "entry_high": signal.entry_high,
        "stop": signal.stop,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": signal.expires_at,
        "trade_style": signal.trade_style,
        "expected_hold_hours": signal.expected_hold_hours,
        "status": "active",
        "tp1_hit": False,
        "tp2_hit": False,
        "stop_hit": False,
    })
    save_active_signals(signals)


def outcome_text(lang: str, outcome: str, signal_data: dict, hit_price: float) -> str:
    side = signal_data["side"]
    if lang == "uz":
        if outcome == "tp1":
            headline = "✅ SIGNAL TAKE PROFIT 1 GA YETDI"
            detail = "Birinchi maqsad bajarildi."
        elif outcome == "tp2":
            headline = "🏆 SIGNAL TAKE PROFIT 2 GA YETDI"
            detail = "Ikkinchi maqsad bajarildi. Signal muvaffaqiyatli yakunlandi."
        elif outcome == "expired":
            headline = "⏳ SIGNAL VAQTI TUGADI"
            detail = "Signal 1–2 kundan uzoq ushlab turilmaydi. Bozor narxida yopish yoki qayta baholash kerak."
        else:
            headline = "❌ SIGNAL STOP LOSS OLDI"
            detail = "Signal stop loss bilan yopildi."
        return (
            f"{headline}\n\n"
            f"Yo‘nalish: {side}\n"
            f"Kirish zonasi: {signal_data['entry_low']:.2f}–{signal_data['entry_high']:.2f}\n"
            f"Fakt narx: {hit_price:.2f}\n"
            f"TP1: {signal_data['tp1']:.2f}\n"
            f"TP2: {signal_data['tp2']:.2f}\n"
            f"Stop Loss: {signal_data['stop']:.2f}\n\n"
            f"{detail}\n"
            f"⚠️ Bu foyda kafolati emas."
        )

    if outcome == "tp1":
        headline = "✅ СИГНАЛ ДОШЁЛ ДО TAKE PROFIT 1"
        detail = "Первая цель достигнута."
    elif outcome == "tp2":
        headline = "🏆 СИГНАЛ ДОШЁЛ ДО TAKE PROFIT 2"
        detail = "Вторая цель достигнута. Сигнал завершён успешно."
    elif outcome == "expired":
        headline = "⏳ СРОК СИГНАЛА ЗАВЕРШЁН"
        detail = "Сигнал не рассчитан на удержание дольше 1–2 дней. Закройте по рынку или выполните новый анализ."
    else:
        headline = "❌ СИГНАЛ ПОЛУЧИЛ STOP LOSS"
        detail = "Сигнал закрыт по Stop Loss."

    return (
        f"{headline}\n\n"
        f"Направление: {side}\n"
        f"Зона входа: {signal_data['entry_low']:.2f}–{signal_data['entry_high']:.2f}\n"
        f"Фактическая цена: {hit_price:.2f}\n"
        f"TP1: {signal_data['tp1']:.2f}\n"
        f"TP2: {signal_data['tp2']:.2f}\n"
        f"Stop Loss: {signal_data['stop']:.2f}\n\n"
        f"{detail}\n"
        f"⚠️ Один результат не гарантирует будущую прибыль."
    )


def outcome_chart(h1: pd.DataFrame, signal_data: dict, outcome: str, hit_price: float, path: Path) -> None:
    plot = h1.tail(100).copy()
    plot.index = plot.index.tz_convert(TZ).tz_localize(None)
    renamed = plot.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    })

    fig, axes = mpf.plot(
        renamed,
        type="candle",
        volume=False,
        figsize=(13, 8),
        title=f"XAU/USD {signal_data['side']} | {outcome.upper()} HIT",
        ylabel="Price",
        datetime_format="%d %b %H:%M",
        xrotation=15,
        tight_layout=True,
        returnfig=True,
    )
    ax = axes[0]

    ax.axhspan(signal_data["entry_low"], signal_data["entry_high"], alpha=0.18)
    levels = [
        (signal_data["entry_low"], "ENTRY LOW"),
        (signal_data["entry_high"], "ENTRY HIGH"),
        (signal_data["stop"], "STOP LOSS"),
        (signal_data["tp1"], "TAKE PROFIT 1"),
        (signal_data["tp2"], "TAKE PROFIT 2"),
        (hit_price, "HIT PRICE"),
    ]
    for level, label in levels:
        ax.axhline(level, linestyle="--", linewidth=1.2)
        ax.text(
            0.995, level, f" {label}: {level:.2f}",
            transform=ax.get_yaxis_transform(),
            ha="right", va="bottom", fontsize=8,
            bbox=dict(boxstyle="round,pad=0.2", alpha=0.65)
        )

    fig.savefig(path, dpi=170, bbox_inches="tight")


async def broadcast_outcome(app: Application, signal_data: dict, outcome: str, hit_price: float, chart_path: Path) -> int:
    users = load_users()
    sent = 0
    for profile in users.values():
        chat_id = profile.get("chat_id")
        if not chat_id:
            continue
        lang = profile.get("language", "ru")
        try:
            with chart_path.open("rb") as photo:
                await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=outcome_text(lang, outcome, signal_data, hit_price),
                )
            sent += 1
        except Exception:
            log.exception("Failed to send outcome to %s", chat_id)
    return sent


async def monitor_active_signals(app: Application) -> None:
    signals = load_active_signals()
    if not signals:
        return

    h1 = enrich(await asyncio.to_thread(td_candles, "1h", 300))
    latest = h1.iloc[-1]
    high = float(latest["high"])
    low = float(latest["low"])
    current_price = float(latest["close"])
    changed = False

    for s in signals:
        if s.get("status") != "active":
            continue

        outcome = None
        hit_price = None
        expires_raw = s.get("expires_at")
        if expires_raw:
            try:
                expires = datetime.fromisoformat(expires_raw)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= expires:
                    s["status"] = "expired"
                    outcome = "expired"
                    hit_price = current_price
            except Exception:
                log.warning("Invalid expires_at for signal %s", s.get("id"))

        if outcome != "expired":
            if s["side"] == "BUY":
                if not s.get("tp1_hit") and high >= s["tp1"]:
                    s["tp1_hit"] = True
                    outcome = "tp1"
                    hit_price = s["tp1"]
                if high >= s["tp2"]:
                    s["tp2_hit"] = True
                    s["status"] = "closed_tp"
                    outcome = "tp2"
                    hit_price = s["tp2"]
                elif low <= s["stop"]:
                    s["stop_hit"] = True
                    s["status"] = "closed_sl"
                    outcome = "sl"
                    hit_price = s["stop"]
            else:
                if not s.get("tp1_hit") and low <= s["tp1"]:
                    s["tp1_hit"] = True
                    outcome = "tp1"
                    hit_price = s["tp1"]
                if low <= s["tp2"]:
                    s["tp2_hit"] = True
                    s["status"] = "closed_tp"
                    outcome = "tp2"
                    hit_price = s["tp2"]
                elif high >= s["stop"]:
                    s["stop_hit"] = True
                    s["status"] = "closed_sl"
                    outcome = "sl"
                    hit_price = s["stop"]

        if outcome:
            changed = True
            chart_path = Path(f"outcome_{s['id']}_{outcome}.png")
            await asyncio.to_thread(outcome_chart, h1, s, outcome, hit_price, chart_path)

            if CHAT_ID:
                with chart_path.open("rb") as photo:
                    await app.bot.send_photo(
                        chat_id=CHAT_ID,
                        photo=photo,
                        caption=outcome_text("ru", outcome, s, hit_price),
                    )

            count = await broadcast_outcome(app, s, outcome, hit_price, chart_path)
            log.info("Outcome sent: %s to %s users", outcome, count)

    if changed:
        save_active_signals(signals)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def duplicate(signal: Signal) -> bool:
    state = load_state()
    last = state.get("last_signal")
    if not last:
        return False
    dt = datetime.fromisoformat(last["time"])
    too_soon = datetime.now(timezone.utc) - dt < timedelta(hours=COOLDOWN_HOURS)
    same_side = last.get("side") == signal.side
    close_entry = abs(float(last.get("entry", 0)) - signal.price) < max(1.0, abs(signal.price) * 0.001)
    return too_soon and same_side and close_entry


def remember(signal: Signal) -> None:
    save_state({"last_signal": {
        "time": datetime.now(timezone.utc).isoformat(),
        "side": signal.side,
        "entry": signal.price
    }})


async def create_analysis() -> tuple[Optional[Signal], Optional[pd.DataFrame], NewsContext]:
    h1 = enrich(await asyncio.to_thread(td_candles, "1h", 500))
    h4 = enrich(await asyncio.to_thread(td_candles, "4h", 500))
    d1 = enrich(await asyncio.to_thread(td_candles, "1day", 500))
    news = await asyncio.to_thread(get_news_context)
    signal = build_signal(h1, h4, d1, news)
    if signal and OPENAI_ENABLED and OPENAI_API_KEY:
        review = await asyncio.to_thread(ai_validate_signal, signal, h1, h4, d1)
        signal.ai_verdict = review["verdict"]
        signal.ai_confidence = review["confidence"]
        signal.ai_summary = review["summary"]
        if review.get("news_conclusion"):
            signal.ai_summary += " Новостной итог: " + review["news_conclusion"]
        opposite = signal.ai_verdict in {"BUY", "SELL"} and signal.ai_verdict != signal.side
        if AI_BLOCK_OPPOSITE and opposite and signal.ai_confidence >= 65:
            log.warning("Signal blocked by OpenAI: algo=%s ai=%s confidence=%s", signal.side, signal.ai_verdict, signal.ai_confidence)
            return None, h1, news
    elif signal:
        signal.ai_summary = "OPENAI_API_KEY не добавлен; использован алгоритмический анализ."
    return signal, h1, news


async def publish_signal(app: Application, force: bool = False) -> str:
    signal, h1, news = await create_analysis()
    if not signal:
        if news.blocked:
            return f"Сигнал заблокирован: {news.title}. {news.explanation}"
        return "Сейчас подтверждённого сетапа H1/H4/D1 нет."

    if duplicate(signal) and not force:
        return "Похожий сигнал уже публиковался недавно."

    await asyncio.to_thread(chart_signal, h1, signal, CHART_FILE)
    text = caption(signal)

    if DRY_RUN:
        log.info("DRY RUN\n%s", text)
        return "DRY_RUN: карточка создана, но не опубликована."

    channel_sent = False
    if CHAT_ID:
        with CHART_FILE.open("rb") as photo:
            await app.bot.send_photo(
                chat_id=CHAT_ID,
                photo=photo,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        channel_sent = True

    personal_count = await broadcast_personal_signal(app, signal, CHART_FILE)
    register_active_signal(signal)
    remember(signal)
    return f"Сигнал опубликован. Канал: {channel_sent}. Личных сообщений: {personal_count}."


async def analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Проверяю H1/H4/D1, Smart Money, Fibonacci, новости и OpenAI…")
    try:
        signal, h1, news = await create_analysis()
        if not signal:
            await msg.edit_text(
                f"Подтверждённого сигнала сейчас нет.\n"
                f"Новости: {news.title}\n{news.explanation}"
            )
            return
        await asyncio.to_thread(chart_signal, h1, signal, CHART_FILE)
        with CHART_FILE.open("rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=caption(signal),
                parse_mode=ParseMode.HTML,
            )
        await msg.delete()
    except Exception as exc:
        log.exception("analysis failed")
        await msg.edit_text(f"Ошибка анализа: {exc}")


async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        all_events = await asyncio.to_thread(economic_events)
        relevant = [e for e in all_events if event_relevant(e)]
        now_local = datetime.now(TZ)
        today = now_local.date()
        tomorrow = today + timedelta(days=1)

        today_events = []
        next_events = []
        for e in relevant:
            try:
                dt_utc = parse_dt(str(e.get("Date", "")))
            except Exception:
                continue
            item = {**e, "_dt": dt_utc}
            local_date = dt_utc.astimezone(TZ).date()
            if local_date == today:
                today_events.append(item)
            elif today < local_date <= tomorrow:
                next_events.append(item)

        def render_event(e: dict) -> str:
            local = e["_dt"].astimezone(TZ)
            actual = e.get("Actual", "—") or "—"
            forecast = e.get("Forecast", "—") or "—"
            previous = e.get("Previous", "—") or "—"
            released = local <= now_local
            icon = "✅" if released and actual != "—" else "⏳" if not released else "⚪️"
            line = (
                f"{icon} {local:%H:%M} — {e.get('Event','')}\n"
                f"   Факт: {actual} | Прогноз: {forecast} | Пред.: {previous}\n"
                f"   Источник: {e.get('Source','Календарь')}"
            )
            if released and actual != "—":
                bias, conclusion = news_direction(e)
                xau = "поддержка золоту" if bias == "bullish_gold" else "давление на золото" if bias == "bearish_gold" else "нейтрально"
                line += f"\n   Вывод XAU/USD: {xau}. {conclusion}"
            return line

        rows = [f"🇺🇸 Важные новости США на {today:%d.%m.%Y} (Ташкент):"]
        if today_events:
            rows.extend(render_event(e) for e in sorted(today_events, key=lambda x: x["_dt"]))
        elif next_events:
            rows.append("Сегодня важных событий не найдено. Ближайшие:")
            rows.extend(render_event(e) for e in sorted(next_events, key=lambda x: x["_dt"])[:8])
        else:
            rows.append(
                "Календарные источники сейчас не вернули важные события. "
                "Проверь Railway → Console: там будет строка 'Economic calendar loaded ...'."
            )
        await update.message.reply_text("\n\n".join(rows)[:4000])
    except Exception as exc:
        log.exception("news command failed")
        await update.message.reply_text(f"Ошибка календаря: {exc}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"✅ Бот запущен\n"
        f"Инструмент: {SYMBOL}\n"
        f"Таймфреймы: H1/H4/D1\n"
        f"Стратегия: Smart Money + Fibonacci + индикаторы + OpenAI-фильтр\n"
        f"Срок сигнала: 4–8 часов или максимум {SIGNAL_MAX_HOURS} часов\n"
        f"Проверка: каждые {SCAN_MINUTES} мин\n"
        f"DRY_RUN: {DRY_RUN}\n"
        f"OpenAI: {'подключён' if OPENAI_ENABLED and OPENAI_API_KEY else 'не подключён'}"
    )


async def testsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = await publish_signal(context.application, force=True)
        await update.message.reply_text(result)
    except Exception as exc:
        log.exception("test signal failed")
        await update.message.reply_text(f"Ошибка: {exc}")


async def scheduled_scan(app: Application) -> None:
    try:
        result = await publish_signal(app)
        log.info(result)
    except Exception:
        log.exception("scheduled scan failed")


async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        scheduled_scan,
        "interval",
        minutes=SCAN_MINUTES,
        args=[app],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scan_official_news,
        "interval",
        minutes=OFFICIAL_NEWS_SCAN_MINUTES,
        args=[app],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor_active_signals,
        "interval",
        minutes=SCAN_MINUTES,
        args=[app],
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("Scheduler started")


async def post_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    routes = {
        "📊 Анализ": analysis_cmd,
        "📊 Tahlil": analysis_cmd,
        "📰 Новости": news_cmd,
        "📰 Yangiliklar": news_cmd,
        "✅ Статус": status_cmd,
        "✅ Holat": status_cmd,
        "🧪 Тест-сигнал": testsignal_cmd,
        "🧪 Test-signal": testsignal_cmd,
        "👤 Профиль": profile_cmd,
        "👤 Profil": profile_cmd,
        "🏛 Официальные новости": officialnews_cmd,
        "🏛 Rasmiy yangiliklar": officialnews_cmd,
    }
    if text in {"⚙️ Изменить язык/капитал", "⚙️ Til/kapitalni o‘zgartirish"}:
        await start_cmd(update, context)
        return
    handler = routes.get(text)
    if handler:
        await handler(update, context)



def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={
            SELECT_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, language_choice)],
            ENTER_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, capital_entry)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)],
    )
    app.add_handler(setup_handler)
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("analysis", analysis_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("testsignal", testsignal_cmd))
    app.add_handler(CommandHandler("officialnews", officialnews_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_button_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
