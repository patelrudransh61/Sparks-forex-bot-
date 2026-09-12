import asyncio
import hashlib
import hmac
import logging
import os
import json
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

from google import genai
from google.genai import types


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("sparks-forex")


BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
).strip()

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

CHANNEL_ID = os.getenv(
    "SIGNAL_CHANNEL_ID",
    ""
).strip()

INTERVAL = int(
    os.getenv(
        "UPDATE_INTERVAL_SECONDS",
        "60"
    )
)

NEWS_LIMIT = int(
    os.getenv(
        "NEWS_LIMIT",
        "5"
    )
)


# Fixed access password is hashed, not stored as plaintext.
# Password: Sparks@7421
PASSWORD_HASH = (
    "537061726b73466978656453616c742d7631:"
    "a537bae5e0dd4994779782c709a117f1de89ba86b0ad1644838ba18e8f16ac92"
)


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


AUTH = set()
WAITING = {}
SESSIONS = {}
LAST_SIGNAL = {}


def check_config():
    missing = [
        x
        for x, v in {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "GEMINI_API_KEY": GEMINI_API_KEY,
        }.items()
        if not v
    ]

    if missing:
        raise RuntimeError(
            "Missing Railway variables: "
            + ", ".join(missing)
        )


def password_ok(value):
    try:
        salt_hex, hash_hex = PASSWORD_HASH.split(
            ":",
            1
        )

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode(),
            bytes.fromhex(salt_hex),
            210_000
        )

        return hmac.compare_digest(
            actual,
            bytes.fromhex(hash_hex)
        )

    except Exception:
        return False


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Start Session",
                callback_data="start"
            ),
            InlineKeyboardButton(
                "⏹ Stop Session",
                callback_data="stop"
            )
        ],
        [
            InlineKeyboardButton(
                "💼 Active Trade",
                callback_data="active"
            ),
            InlineKeyboardButton(
                "📋 Status",
                callback_data="status"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help"
            )
        ],
    ])


def assets_menu():
    rows = []
    row = []

    for name in ASSETS:
        row.append(
            InlineKeyboardButton(
                name,
                callback_data="asset|" + name
            )
        )

        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows)


def signal_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ TRADE TAKEN",
                callback_data="taken"
            ),
            InlineKeyboardButton(
                "❌ TRADE NOT TAKEN",
                callback_data="not_taken"
            )
        ]
    ])


def trade_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛑 STOP TRADE",
                callback_data="stop_trade"
            ),
            InlineKeyboardButton(
                "🔄 REFRESH",
                callback_data="refresh"
            )
        ]
    ])


def snapshot(asset):
    ticker = ASSETS[asset]

    df = yf.download(
        ticker,
        period="3mo",
        interval="1h",
        progress=False,
        auto_adjust=False,
        threads=False
    )

    if df is None or df.empty:
        raise RuntimeError(
            "No market data available."
        )

    if getattr(
        df.columns,
        "nlevels",
        1
    ) > 1:

        try:
            df = df.xs(
                ticker,
                axis=1,
                level=-1
            )

        except Exception:
            df.columns = (
                df.columns
                .get_level_values(0)
            )

    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()

    if len(close) < 55:
        raise RuntimeError(
            "Not enough candles."
        )

    delta = close.diff()

    gain = (
        delta
        .clip(lower=0)
        .rolling(14)
        .mean()
    )

    loss = (
        -delta
        .clip(upper=0)
        .rolling(14)
        .mean()
    )

    rs = gain / loss.replace(
        0,
        float("nan")
    )

    rsi = float(
        (
            100
            - (
                100
                / (1 + rs)
            )
        ).iloc[-1]
    )

    ret = close.pct_change().dropna()

    return {
        "price": float(
            close.iloc[-1]
        ),
        "change": float(
            (
                close.iloc[-1]
                / close.iloc[-2]
                - 1
            )
            * 100
        ),
        "high": float(
            high.iloc[-1]
        ),
        "low": float(
            low.iloc[-1]
        ),
        "sma20": float(
            close
            .rolling(20)
            .mean()
            .iloc[-1]
        ),
        "sma50": float(
            close
            .rolling(50)
            .mean()
            .iloc[-1]
        ),
        "rsi": rsi,
        "volatility": float(
            ret.tail(20).std() * 100
        ),
    }


def get_news(asset):
    q = urllib.parse.quote(
        asset + " market"
    )

    url = (
        "https://news.google.com/rss/search"
        f"?q={q}&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        r = httpx.get(
            url,
            headers={
                "User-Agent":
                "Sparks-Forex/1.0"
            },
            timeout=10
        )

        r.raise_for_status()

        feed = feedparser.parse(
            r.text
        )

        return [
            {
                "title": e.get(
                    "title",
                    ""
                ),
                "published": e.get(
                    "published",
                    ""
                )
            }
            for e in feed.entries[
                :NEWS_LIMIT
            ]
        ]

    except Exception:
        return []


# ============================================================
# GEMINI AI
# ============================================================

async def ai_analyze(
    asset,
    snap,
    news,
    session
):

    active = session.get(
        "trade"
    )

    prompt = {
        "asset": asset,
        "market": snap,
        "news": news,
        "session_budget": session[
            "budget"
        ],
        "risk_percent": session[
            "risk"
        ],
        "active_trade": active
    }

    instructions = """
You are an analytical market assistant.

Analyze the supplied market data and supplied news only.

If an active trade exists, prioritize monitoring it.

Never claim certainty.
Never invent news.
Never claim guaranteed future movement.

Return ONLY valid JSON.
Do not use markdown.
Do not wrap the response in ```.

Use exactly this structure:

{
  "action": "BUY | SELL | WAIT | HOLD | EXIT_WATCH",
  "confidence": 0,
  "entry_reference": 0,
  "stop_loss_reference": 0,
  "target_reference": 0,
  "risk_level": "LOW | MEDIUM | HIGH",
  "suggested_amount": 0,
  "summary": "",
  "trade_management": ""
}

Rules:

1. BUY means the supplied evidence suggests a potential bullish setup.
2. SELL means the supplied evidence suggests a potential bearish setup.
3. WAIT means evidence is weak, mixed, or unclear.
4. HOLD means an existing monitored trade still appears acceptable.
5. EXIT_WATCH means an existing monitored trade has deteriorating conditions.
6. Confidence must be between 0 and 100.
7. Suggested amount must not exceed the session budget.
8. Consider price, SMA20, SMA50, RSI and volatility.
9. Consider supplied news as supporting or conflicting evidence.
10. Do not invent or assume news.
11. Keep explanations concise.
12. BUY/SELL are analytical outputs only and are not guarantees.
13. If technical evidence and news conflict strongly, reduce confidence or use WAIT.

DATA:

%s
""" % json.dumps(
        prompt,
        ensure_ascii=False
    )

    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL,
        contents=instructions,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json"
        )
    )

    content = (
        response.text or ""
    ).strip()

    if not content:
        raise RuntimeError(
            "Gemini returned an empty response."
        )

    if content.startswith("```"):
        content = (
            content
            .replace(
                "```json",
                ""
            )
            .replace(
                "```",
                ""
            )
            .strip()
        )

    result = json.loads(
        content
    )

    result["confidence"] = float(
        result.get(
            "confidence",
            0
        )
    )

    result["suggested_amount"] = float(
        result.get(
            "suggested_amount",
            0
        )
    )

    return result


def channel_signal(
    asset,
    result
):

    action = result.get(
        "action",
        "WAIT"
    )

    return (
        f"🚨 <b>{asset} — {action}</b>\n\n"
        f"Entry: <code>"
        f"{result.get('entry_reference', 0)}"
        f"</code>\n"
        f"SL: <code>"
        f"{result.get('stop_loss_reference', 0)}"
        f"</code>\n"
        f"TP: <code>"
        f"{result.get('target_reference', 0)}"
        f"</code>\n\n"
        f"Risk: <b>"
        f"{result.get('risk_level', 'UNKNOWN')}"
        f"</b>\n"
        f"Confidence: <b>"
        f"{result.get('confidence', 0):.0f}%"
        f"</b>\n\n"
        "AI MARKET ANALYZER"
    )


async def analyze_user(
    uid,
    context,
    force=False
):

    s = SESSIONS.get(uid)

    if not s:
        return

    try:

        snap = await asyncio.to_thread(
            snapshot,
            s["asset"]
        )

        news = await asyncio.to_thread(
            get_news,
            s["asset"]
        )

        result = await ai_analyze(
            s["asset"],
            snap,
            news,
            s
        )

        LAST_SIGNAL[uid] = result

        if s.get("trade"):

            t = s["trade"]

            text = (
                "💼 <b>ACTIVE TRADE UPDATE</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\n"
                f"Direction: <b>{t['direction']}</b>\n"
                f"Entry: <code>{t['entry']}</code>\n"
                f"SL: <code>{t['sl']}</code>\n"
                f"TP: <code>{t['tp']}</code>\n\n"
                f"Current Price: <b>"
                f"{snap['price']:.6g}"
                f"</b>\n"
                f"Decision: <b>"
                f"{result.get('action','HOLD')}"
                f"</b>\n"
                f"Risk: <b>"
                f"{result.get('risk_level','UNKNOWN')}"
                f"</b>\n"
                f"Confidence: <b>"
                f"{result.get('confidence',0):.0f}%"
                f"</b>\n\n"
                f"🧠 "
                f"{result.get('summary','')}\n\n"
                f"Management: "
                f"{result.get('trade_management','Monitor the position.')}"
            )

            await context.bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=trade_buttons()
            )

        elif result.get(
            "action"
        ) in (
            "BUY",
            "SELL"
        ):

            amount = min(
                max(
                    0,
                    result.get(
                        "suggested_amount",
                        s["budget"]
                        * s["risk"]
                        / 100
                    )
                ),
                s["budget"]
            )

            text = (
                "🚨 <b>SIGNAL DETECTED</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\n"
                f"Direction: <b>{result['action']}</b>\n"
                f"Entry: <code>"
                f"{result.get('entry_reference',0)}"
                f"</code>\n"
                f"SL: <code>"
                f"{result.get('stop_loss_reference',0)}"
                f"</code>\n"
                f"TP: <code>"
                f"{result.get('target_reference',0)}"
                f"</code>\n"
                f"Risk: <b>"
                f"{s['risk']:.2f}%"
                f"</b>\n"
                f"Suggested Amount: <b>"
                f"₹{amount:,.2f}"
                f"</b>\n"
                f"Risk Level: <b>"
                f"{result.get('risk_level','UNKNOWN')}"
                f"</b>\n"
                f"Confidence: <b>"
                f"{result.get('confidence',0):.0f}%"
                f"</b>\n\n"
                f"🧠 "
                f"{result.get('summary','')}"
            )

            await context.bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=signal_buttons()
            )

            if CHANNEL_ID:

                try:

                    await context.bot.send_message(
                        int(CHANNEL_ID),
                        channel_signal(
                            s["asset"],
                            result
                        ),
                        parse_mode="HTML"
                    )

                except Exception:

                    log.exception(
                        "Could not post signal to channel."
                    )

        else:

            text = (
                "📊 <b>MARKET UPDATE</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\n"
                f"Price: <b>"
                f"{snap['price']:.6g}"
                f"</b>\n"
                f"Change: <b>"
                f"{snap['change']:+.2f}%"
                f"</b>\n"
                f"Trend: <b>"
                f"{'Bullish' if snap['sma20'] > snap['sma50'] else 'Bearish'}"
                f"</b>\n"
                f"RSI: <b>"
                f"{snap['rsi']:.1f}"
                f"</b>\n"
                f"Risk: <b>"
                f"{s['risk']:.2f}%"
                f"</b>\n"
                f"Signal: <b>"
                f"{result.get('action','WAIT')}"
                f"</b>\n\n"
                f"🧠 "
                f"{result.get('summary','No strong setup right now.')}"
            )

            await context.bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=menu()
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
            parse_mode="HTML"
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    if uid not in AUTH:

        WAITING[uid] = "password"

        await update.message.reply_text(
            (
                "🔐 <b>Authentication Required</b>\n\n"
                "Enter your access password."
            ),
            parse_mode="HTML"
        )

    else:

        await update.message.reply_text(
            (
                "🔓 <b>Authenticated.</b>\n\n"
                "Choose an option."
            ),
            parse_mode="HTML",
            reply_markup=menu()
        )


async def text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    value = (
        update.message.text or ""
    ).strip()

    state = WAITING.get(uid)

    if state == "password":

        if password_ok(value):

            AUTH.add(uid)

            WAITING.pop(
                uid,
                None
            )

            await update.message.reply_text(
                "✅ <b>Authentication successful.</b>",
                parse_mode="HTML",
                reply_markup=menu()
            )

        else:

            await update.message.reply_text(
                "❌ Incorrect password."
            )

        return

    if uid not in AUTH:

        await update.message.reply_text(
            "🔒 Send /start first."
        )

        return

    if state == "budget":

        try:

            budget = float(
                value.replace(
                    ",",
                    ""
                )
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
            "🎯 <b>Select the main asset:</b>",
            parse_mode="HTML",
            reply_markup=assets_menu()
        )

        return

    if state == "risk":

        try:

            risk = float(
                value.replace(
                    "%",
                    ""
                )
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
            "budget": context.user_data[
                "budget"
            ],
            "asset": context.user_data[
                "asset"
            ],
            "risk": risk,
            "trade": None
        }

        WAITING.pop(
            uid,
            None
        )

        await update.message.reply_text(
            (
                "🔎 <b>Signal scanning started.</b>\n"
                "Updates every minute."
            ),
            parse_mode="HTML",
            reply_markup=menu()
        )

        await analyze_user(
            uid,
            context,
            True
        )

        return

    # ========================================================
    # MONITORING REFERENCE
    # ========================================================

    if state == "trade":

        p = (
            value
            .replace(
                ",",
                " "
            )
            .split()
        )

        if len(p) != 3:

            await update.message.reply_text(
                (
                    "💼 <b>Monitoring Reference</b>\n\n"
                    "Send:\n"
                    "<code>entry stop_loss target</code>\n\n"
                    "Example:\n"
                    "<code>100.50 99.80 102.00</code>"
                ),
                parse_mo
