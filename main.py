
import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import feedparser
import httpx
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("sparks-forex")


# ============================================================
# RAILWAY VARIABLES
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
MODEL = os.getenv("XAI_MODEL", "grok-4.6").strip()

CHANNEL_ID = os.getenv("SIGNAL_CHANNEL_ID", "").strip()

INTERVAL = int(os.getenv("UPDATE_INTERVAL_SECONDS", "60"))
NEWS_LIMIT = int(os.getenv("NEWS_LIMIT", "5"))


# ============================================================
# FIXED ACCESS PASSWORD
# Password: Sparks@7421
# ============================================================

PASSWORD_HASH = (
    "537061726b73466978656453616c742d7631:"
    "a537bae5e0dd4994779782c709a117f1de89ba86b0ad1644838ba18e8f16ac92"
)


# ============================================================
# SUPPORTED ASSETS
# ============================================================

ASSETS = {
    "BTC/USDT": "BTC-USD",
    "ETH/USDT": "ETH-USD",
    "SOL/USDT": "SOL-USD",
    "BNB/USDT": "BNB-USD",
    "XRP/USDT": "XRP-USD",
    "XAU/USD": "GC=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "NASDAQ": "^IXIC",
    "S&P 500": "^GSPC",
}


# ============================================================
# MEMORY
# ============================================================

AUTH = set()
WAITING = {}
SESSIONS = {}
LAST_ANALYSIS = {}


# ============================================================
# CONFIG CHECK
# ============================================================

def check_config():
    missing = [
        x
        for x, v in {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "XAI_API_KEY": XAI_API_KEY,
        }.items()
        if not v
    ]

    if missing:
        raise RuntimeError(
            "Missing Railway variables: " + ", ".join(missing)
        )


# ============================================================
# PASSWORD
# ============================================================

def password_ok(value):
    try:
        salt_hex, hash_hex = PASSWORD_HASH.split(":", 1)

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode(),
            bytes.fromhex(salt_hex),
            210_000,
        )

        return hmac.compare_digest(
            actual,
            bytes.fromhex(hash_hex),
        )

    except Exception:
        return False


# ============================================================
# MAIN MENU
# ============================================================

def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Start Session",
                callback_data="start",
            ),
            InlineKeyboardButton(
                "⏹ Stop Session",
                callback_data="stop",
            ),
        ],
        [
            InlineKeyboardButton(
                "💼 Active Trade",
                callback_data="active",
            ),
            InlineKeyboardButton(
                "📋 Status",
                callback_data="status",
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            ),
        ],
    ])


# ============================================================
# ASSET MENU
# ============================================================

def assets_menu():
    rows = []
    row = []

    for name in ASSETS:
        row.append(
            InlineKeyboardButton(
                name,
                callback_data="asset|" + name,
            )
        )

        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows)


# ============================================================
# MONITORING BUTTONS
# ============================================================

def trade_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛑 STOP MONITORING",
                callback_data="stop_trade",
            ),
            InlineKeyboardButton(
                "🔄 REFRESH",
                callback_data="refresh",
            ),
        ]
    ])


# ============================================================
# MARKET SNAPSHOT
# ============================================================

def snapshot(asset):
    ticker = ASSETS[asset]

    df = yf.download(
        ticker,
        period="3mo",
        interval="1h",
        progress=False,
        auto_adjust=False,
        threads=False,
    )

    if df is None or df.empty:
        raise RuntimeError("No market data available.")

    if getattr(df.columns, "nlevels", 1) > 1:
        try:
            df = df.xs(
                ticker,
                axis=1,
                level=-1,
            )
        except Exception:
            df.columns = df.columns.get_level_values(0)

    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()

    if len(close) < 55:
        raise RuntimeError("Not enough candles.")

    delta = close.diff()

    gain = (
        delta.clip(lower=0)
        .rolling(14)
        .mean()
    )

    loss = (
        -delta.clip(upper=0)
    ).rolling(14).mean()

    rs = gain / loss.replace(
        0,
        float("nan"),
    )

    rsi = float(
        (100 - (100 / (1 + rs))).iloc[-1]
    )

    ret = close.pct_change().dropna()

    return {
        "price": float(close.iloc[-1]),
        "change": float(
            (close.iloc[-1] / close.iloc[-2] - 1) * 100
        ),
        "high": float(high.iloc[-1]),
        "low": float(low.iloc[-1]),
        "sma20": float(
            close.rolling(20).mean().iloc[-1]
        ),
        "sma50": float(
            close.rolling(50).mean().iloc[-1]
        ),
        "rsi": rsi,
        "volatility": float(
            ret.tail(20).std() * 100
        ),
    }


# ============================================================
# NEWS
# ============================================================

def get_news(asset):
    query = urllib.parse.quote(
        asset + " market"
    )

    url = (
        "https://news.google.com/rss/search"
        f"?q={query}&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        response = httpx.get(
            url,
            headers={
                "User-Agent": "Sparks-Forex/1.0"
            },
            timeout=10,
        )

        response.raise_for_status()

        feed = feedparser.parse(
            response.text
        )

        return [
            {
                "title": entry.get(
                    "title",
                    "",
                ),
                "published": entry.get(
                    "published",
                    "",
                ),
            }
            for entry in feed.entries[:NEWS_LIMIT]
        ]

    except Exception:
        return []


# ============================================================
# GROK ANALYSIS
# ============================================================

async def ai_analyze(
    asset,
    snap,
    news,
    session,
):
    active = session.get("trade")

    market_direction = (
        "BULLISH"
        if snap["sma20"] > snap["sma50"]
        else "BEARISH"
    )

    analysis_input = {
        "asset": asset,
        "market": snap,
        "technical_direction": market_direction,
        "news": news,
        "active_trade": active,
    }

    instructions = f"""
You are Sparks Market Analysis AI.

Your task is ONLY to determine whether the currently observed market trend
is likely to continue based on the supplied market data and supplied news.

Do not claim certainty.
Do not invent news.
Do not use information that was not supplied.
Do not execute trades.
Do not give financial instructions.

Return ONLY valid JSON.

Required JSON structure:

{{
  "trend_continues": "YES or NO",
  "confidence": 0,
  "trend": "BULLISH or BEARISH or MIXED",
  "summary": "",
  "market_factors": "",
  "news_factors": "",
  "risk_note": ""
}}

Rules:

1. trend_continues must be exactly YES or NO.
2. confidence must be a number from 0 to 100.
3. Consider price change, SMA20, SMA50, RSI and volatility.
4. Consider the supplied news and whether it supports or conflicts with
   the observed trend.
5. If technical and news evidence conflict strongly, prefer NO.
6. If evidence is weak or mixed, use NO.
7. Never invent a headline or event.
8. Keep the response concise.
9. Explain WHY the answer is YES or NO.
10. The answer is an analysis, not a guarantee.

DATA:

{json.dumps(analysis_input, ensure_ascii=False)}
"""

    headers = {
        "Authorization": "Bearer " + XAI_API_KEY,
        "Content-Type": "application/json",
    }

    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful market-analysis assistant. "
                    "Return only valid JSON."
                ),
            },
            {
                "role": "user",
                "content": instructions,
            },
        ],
        "temperature": 0.2,
    }

    async with httpx.AsyncClient(
        timeout=45
    ) as client:

        response = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers=headers,
            json=body,
        )

        response.raise_for_status()

        data = response.json()

    content = (
        data["choices"][0]["message"]["content"]
        .strip()
    )

    if content.startswith("```"):
        content = (
            content
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

    result = json.loads(content)

    trend_continues = str(
        result.get(
            "trend_continues",
            "NO",
        )
    ).upper()

    if trend_continues not in (
        "YES",
        "NO",
    ):
        trend_continues = "NO"

    result["trend_continues"] = (
        trend_continues
    )

    result["confidence"] = float(
        result.get(
            "confidence",
            0,
        )
    )

    result["confidence"] = max(
        0,
        min(
            100,
            result["confidence"],
        ),
    )

    return result


# ============================================================
# FORMAT ANALYSIS
# ============================================================

def analysis_text(
    asset,
    snap,
    result,
):
    trend = result.get(
        "trend",
        "MIXED",
    )

    continuation = result.get(
        "trend_continues",
        "NO",
    )

    confidence = result.get(
        "confidence",
        0,
    )

    summary = result.get(
        "summary",
        "No strong conclusion.",
    )

    market_factors = result.get(
        "market_factors",
        "",
    )

    news_factors = result.get(
        "news_factors",
        "",
    )

    risk_note = result.get(
        "risk_note",
        "",
    )

    return (
        "📊 <b>MARKET ANALYSIS</b>\n\n"
        f"Asset: <b>{asset}</b>\n"
        f"Price: <b>{snap['price']:.6g}</b>\n"
        f"Change: <b>{snap['change']:+.2f}%</b>\n\n"
        f"Trend: <b>{trend}</b>\n"
        f"Trend Continuation: "
        f"<b>{continuation}</b>\n"
        f"Confidence: "
        f"<b>{confidence:.0f}%</b>\n\n"
        f"🧠 <b>Summary</b>\n"
        f"{summary}\n\n"
        f"📈 <b>Market Factors</b>\n"
        f"{market_factors}\n\n"
        f"📰 <b>News Factors</b>\n"
        f"{news_factors}\n\n"
        f"⚠️ <b>Risk Note</b>\n"
        f"{risk_note}"
    )


# ============================================================
# ANALYZE USER
# ============================================================

async def analyze_user(
    uid,
    context,
    force=False,
):
    session = SESSIONS.get(uid)

    if not session:
        return

    try:
        snap = await asyncio.to_thread(
            snapshot,
            session["asset"],
        )

        news = await asyncio.to_thread(
            get_news,
            session["asset"],
        )

        result = await ai_analyze(
            session["asset"],
            snap,
            news,
            session,
        )

        LAST_ANALYSIS[uid] = result

        # ----------------------------------------------------
        # ACTIVE MONITORING
        # ----------------------------------------------------

        if session.get("trade"):
            trade = session["trade"]

            text = (
                "💼 <b>ACTIVE MONITORING UPDATE</b>\n\n"
                f"Asset: <b>{session['asset']}</b>\n"
                f"Reference Direction: "
                f"<b>{trade['direction']}</b>\n"
                f"Entry: <code>{trade['entry']}</code>\n"
                f"SL: <code>{trade['sl']}</code>\n"
                f"TP: <code>{trade['tp']}</code>\n\n"
                f"Current Price: "
                f"<b>{snap['price']:.6g}</b>\n"
                f"Trend: "
                f"<b>{result.get('trend', 'MIXED')}</b>\n"
                f"Trend Continuation: "
                f"<b>{result.get('trend_continues', 'NO')}</b>\n"
                f"Confidence: "
                f"<b>{result.get('confidence', 0):.0f}%</b>\n\n"
                f"🧠 "
                f"{result.get('summary', '')}\n\n"
                f"Management note: "
                f"{result.get('risk_note', '')}"
            )

            await context.bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=trade_buttons(),
            )

        # ----------------------------------------------------
        # NORMAL ANALYSIS
        # ----------------------------------------------------

        else:
            text = analysis_text(
                session["asset"],
                snap,
                result,
            )

            await context.bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=menu(),
            )

    except Exception:
        log.exception(
            "Analysis failed"
        )

        await context.bot.send_message(
            uid,
            (
                "⚠️ <b>Analysis temporarily unavailable.</b>\n"
                "The session remains active and will retry."
            ),
            parse_mode="HTML",
        )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = update.effective_user.id

    if uid not in AUTH:
        WAITING[uid] = "password"

        await update.message.reply_text(
            (
                "🔐 <b>Authentication Required</b>\n\n"
                "Enter your access password."
            ),
            parse_mode="HTML",
        )

    else:
        await update.message.reply_text(
            (
                "🔓 <b>Authenticated.</b>\n\n"
                "Choose an option."
            ),
            parse_mode="HTML",
            reply_markup=menu(),
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = update.effective_user.id

    value = (
        update.message.text or ""
    ).strip()

    state = WAITING.get(uid)

    # --------------------------------------------------------
    # PASSWORD
    # --------------------------------------------------------

    if state == "password":

        if password_ok(value):

            AUTH.add(uid)
            WAITING.pop(uid, None)

            await update.message.reply_text(
                (
                    "✅ <b>Authentication successful.</b>"
                ),
                parse_mode="HTML",
                reply_markup=menu(),
            )

        else:

            await update.message.reply_text(
                "❌ Incorrect password."
            )

        return

    # --------------------------------------------------------
    # AUTH REQUIRED
    # --------------------------------------------------------

    if uid not in AUTH:

        await update.message.reply_text(
            "🔒 Send /start first."
        )

        return

    # --------------------------------------------------------
    # BUDGET
    # --------------------------------------------------------

    if state == "budget":

        try:
            budget = float(
                value.replace(",", "")
            )

            if budget <= 0:
                raise ValueError

        except ValueError:

            await update.message.reply_text(
                (
                    "Enter a valid positive budget, "
                    "e.g. 10000."
                )
            )

            return

        context.user_data[
            "budget"
        ] = budget

        WAITING[uid] = "asset"

        await update.message.reply_text(
            (
                "🎯 <b>Select the main asset:</b>"
            ),
            parse_mode="HTML",
            reply_markup=assets_menu(),
        )

        return

    # --------------------------------------------------------
    # RISK
    # --------------------------------------------------------

    if state == "risk":

        try:
            risk = float(
                value.replace("%", "")
            )

            if not 0 < risk <= 10:
                raise ValueError

        except ValueError:

            await update.message.reply_text(
                (
                    "Enter a risk rate between "
                    "0.1% and 10%, e.g. 1."
                )
            )

            return

        SESSIONS[uid] = {
            "budget": context.user_data["budget"],
            "asset": context.user_data["asset"],
            "risk": risk,
            "trade": None,
        }

        WAITING.pop(uid, None)

        await update.message.reply_text(
            (
                "🔎 <b>Market scanning started.</b>\n"
                f"Updates every {INTERVAL} seconds."
            ),
            parse_mode="HTML",
            reply_markup=menu(),
        )

        await analyze_user(
            uid,
            context,
            True,
        )

        return

    # --------------------------------------------------------
    # MONITORING REFERENCE
    # --------------------------------------------------------

    if state == "trade":

        parts = (
            value
            .replace(",", " ")
            .split()
        )

        if len(parts) != 3:

            await update.message.reply_text(
                (
                    "Send reference levels:\n"
                    "<code>entry stop_loss target</code>"
                ),
                parse_mode="HTML",
            )

            return

        try:
            entry, sl, tp = map(
                float,
                parts,
            )

        except ValueError:

            await update.message.reply_text(
                "Use three numeric values."
            )

            return

        session = SESSIONS.get(uid)

        if not session:

            await
