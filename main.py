import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

import feedparser
import httpx
import yfinance as yf
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("sparks-forex")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
CHANNEL_ID = os.getenv("SIGNAL_CHANNEL_ID", "").strip()
INTERVAL = int(os.getenv("UPDATE_INTERVAL_SECONDS", "60"))
NEWS_LIMIT = int(os.getenv("NEWS_LIMIT", "5"))

# Fixed access password is hashed, not stored as plaintext.
# Password: Sparks@7421
PASSWORD_HASH = "537061726b73466978656453616c742d7631:a537bae5e0dd4994779782c709a117f1de89ba86b0ad1644838ba18e8f16ac92"

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
    missing = [x for x, v in {
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "GEMINI_API_KEY": GEMINI_API_KEY,
    }.items() if not v]
    if missing:
        raise RuntimeError("Missing Railway variables: " + ", ".join(missing))

def password_ok(value):
    try:
        salt_hex, hash_hex = PASSWORD_HASH.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt_hex), 210_000)
        return hmac.compare_digest(actual, bytes.fromhex(hash_hex))
    except Exception:
        return False

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Start Session", callback_data="start"),
         InlineKeyboardButton("⏹ Stop Session", callback_data="stop")],
        [InlineKeyboardButton("💼 Active Trade", callback_data="active"),
         InlineKeyboardButton("📋 Status", callback_data="status")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])

def assets_menu():
    rows, row = [], []
    for name in ASSETS:
        row.append(InlineKeyboardButton(name, callback_data="asset|" + name))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def signal_buttons():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ TRADE TAKEN", callback_data="taken"),
        InlineKeyboardButton("❌ TRADE NOT TAKEN", callback_data="not_taken")
    ]])

def trade_buttons():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 STOP TRADE", callback_data="stop_trade"),
        InlineKeyboardButton("🔄 REFRESH", callback_data="refresh")
    ]])

def snapshot(asset):
    ticker = ASSETS[asset]
    df = yf.download(ticker, period="3mo", interval="1h", progress=False,
                     auto_adjust=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError("No market data available.")
    if getattr(df.columns, "nlevels", 1) > 1:
        try:
            df = df.xs(ticker, axis=1, level=-1)
        except Exception:
            df.columns = df.columns.get_level_values(0)

    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()
    if len(close) < 55:
        raise RuntimeError("Not enough candles.")

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])

    ret = close.pct_change().dropna()
    return {
        "price": float(close.iloc[-1]),
        "change": float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
        "high": float(high.iloc[-1]),
        "low": float(low.iloc[-1]),
        "sma20": float(close.rolling(20).mean().iloc[-1]),
        "sma50": float(close.rolling(50).mean().iloc[-1]),
        "rsi": rsi,
        "volatility": float(ret.tail(20).std() * 100),
    }

def get_news(asset):
    import urllib.parse
    q = urllib.parse.quote(asset + " market")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = httpx.get(url, headers={"User-Agent": "Sparks-Forex/1.0"}, timeout=10)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        return [{"title": e.get("title", ""), "published": e.get("published", "")}
                for e in feed.entries[:NEWS_LIMIT]]
    except Exception:
        return []

async def ai_analyze(asset, snap, news, session):
    active = session.get("trade")
    prompt = {
        "asset": asset,
        "market": snap,
        "news": news,
        "session_budget": session["budget"],
        "risk_percent": session["risk"],
        "active_trade": active,
    }
    instructions = """You are an analytical market assistant. Never claim certainty or future knowledge.
Analyze market data and supplied news only. If an active trade exists, prioritize monitoring it.
Return ONLY valid JSON matching the requested fields.
Rules: BUY/SELL is only a potential setup. If evidence is weak use WAIT.
With an active trade use HOLD if acceptable and EXIT_WATCH if conditions deteriorate.
Do not invent news. Keep all explanations in English. Suggested amount must not exceed session budget.
DATA:
%s""" % prompt

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=GEMINI_MODEL,
        contents=instructions,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    content = (response.text or "").strip()
    if not content:
        raise RuntimeError("Gemini returned an empty response.")
    import json
    result = json.loads(content)
    result["confidence"] = float(result.get("confidence", 0))
    result["suggested_amount"] = float(result.get("suggested_amount", 0))
    return result

def channel_signal(asset, result):
    action = result.get("action", "WAIT")
    return (
        f"🚨 <b>{asset} — {action}</b>\n\n"
        f"Entry: <code>{result.get('entry_reference', 0)}</code>\n"
        f"SL: <code>{result.get('stop_loss_reference', 0)}</code>\n"
        f"TP: <code>{result.get('target_reference', 0)}</code>\n\n"
        f"Risk: <b>{result.get('risk_level', 'UNKNOWN')}</b>\n"
        f"Confidence: <b>{result.get('confidence', 0):.0f}%</b>\n\n"
        "AI MARKET ANALYZER"
    )

async def analyze_user(uid, context, force=False):
    s = SESSIONS.get(uid)
    if not s:
        return
    try:
        snap = await asyncio.to_thread(snapshot, s["asset"])
        news = await asyncio.to_thread(get_news, s["asset"])
        result = await ai_analyze(s["asset"], snap, news, s)
        LAST_SIGNAL[uid] = result

        if s.get("trade"):
            t = s["trade"]
            text = (
                "💼 <b>ACTIVE TRADE UPDATE</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\nDirection: <b>{t['direction']}</b>\n"
                f"Entry: <code>{t['entry']}</code>\nSL: <code>{t['sl']}</code>\nTP: <code>{t['tp']}</code>\n\n"
                f"Current Price: <b>{snap['price']:.6g}</b>\n"
                f"Decision: <b>{result.get('action','HOLD')}</b>\n"
                f"Risk: <b>{result.get('risk_level','UNKNOWN')}</b>\n"
                f"Confidence: <b>{result.get('confidence',0):.0f}%</b>\n\n"
                f"🧠 {result.get('summary','')}\n\n"
                f"Management: {result.get('trade_management','Monitor the position.')}"
            )
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=trade_buttons())
        elif result.get("action") in ("BUY", "SELL"):
            amount = min(max(0, result.get("suggested_amount", s["budget"] * s["risk"] / 100)), s["budget"])
            text = (
                "🚨 <b>SIGNAL DETECTED</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\nDirection: <b>{result['action']}</b>\n"
                f"Entry: <code>{result.get('entry_reference',0)}</code>\n"
                f"SL: <code>{result.get('stop_loss_reference',0)}</code>\n"
                f"TP: <code>{result.get('target_reference',0)}</code>\n"
                f"Risk: <b>{s['risk']:.2f}%</b>\nSuggested Amount: <b>₹{amount:,.2f}</b>\n"
                f"Risk Level: <b>{result.get('risk_level','UNKNOWN')}</b>\n"
                f"Confidence: <b>{result.get('confidence',0):.0f}%</b>\n\n"
                f"🧠 {result.get('summary','')}"
            )
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=signal_buttons())
            if CHANNEL_ID:
                try:
                    await context.bot.send_message(int(CHANNEL_ID), channel_signal(s["asset"], result), parse_mode="HTML")
                except Exception:
                    log.exception("Could not post signal to channel.")
        else:
            text = (
                "📊 <b>MARKET UPDATE</b>\n\n"
                f"Asset: <b>{s['asset']}</b>\nPrice: <b>{snap['price']:.6g}</b>\n"
                f"Change: <b>{snap['change']:+.2f}%</b>\n"
                f"Trend: <b>{'Bullish' if snap['sma20'] > snap['sma50'] else 'Bearish'}</b>\n"
                f"RSI: <b>{snap['rsi']:.1f}</b>\n"
                f"Risk: <b>{s['risk']:.2f}%</b>\nSignal: <b>{result.get('action','WAIT')}</b>\n\n"
                f"🧠 {result.get('summary','No strong setup right now.')}"
            )
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=menu())
    except Exception:
        log.exception("Analysis failed")
        await context.bot.send_message(uid, "⚠️ <b>Analysis temporarily unavailable.</b>\nThe session remains active and will retry.", parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in AUTH:
        WAITING[uid] = "password"
        await update.message.reply_text("🔐 <b>Authentication Required</b>\n\nEnter your access password.", parse_mode="HTML")
    else:
        await update.message.reply_text("🔓 <b>Authenticated.</b>\n\nChoose an option.", parse_mode="HTML", reply_markup=menu())

async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    value = (update.message.text or "").strip()
    state = WAITING.get(uid)

    if state == "password":
        if password_ok(value):
            AUTH.add(uid); WAITING.pop(uid, None)
            await update.message.reply_text("✅ <b>Authentication successful.</b>", parse_mode="HTML", reply_markup=menu())
        else:
            await update.message.reply_text("❌ Incorrect password.")
        return

    if uid not in AUTH:
        await update.message.reply_text("🔒 Send /start first.")
        return

    if state == "budget":
        try:
            budget = float(value.replace(",", ""))
            if budget <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid positive budget, e.g. 10000.")
            return
        context.user_data["budget"] = budget
        WAITING[uid] = "asset"
        await update.message.reply_text("🎯 <b>Select the main asset:</b>", parse_mode="HTML", reply_markup=assets_menu())
        return

    if state == "risk":
        try:
            risk = float(value.replace("%", ""))
            if not 0 < risk <= 10: raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a risk rate between 0.1% and 10%, e.g. 1.")
            return
        SESSIONS[uid] = {"budget": context.user_data["budget"], "asset": context.user_data["asset"], "risk": risk, "trade": None}
        WAITING.pop(uid, None)
        await update.message.reply_text("🔎 <b>Signal scanning started.</b>\nUpdates every minute.", parse_mode="HTML", reply_markup=menu())
        await analyze_user(uid, context, True)
        return

    if state == "trade":
        p = value.replace(",", " ").split()
        if len(p) != 3:
            await update.message.reply_text("Send: <code>entry stop_loss target</code>", parse_mode="HTML")
            return
        try:
            entry, sl, tp = map(float, p)
        except ValueError:
            await update.message.reply_text("Use three numeric values.")
            return
        s = SESSIONS.get(uid)
        action = LAST_SIGNAL.get(uid, {}).get("action", "BUY")
        if action not in ("BUY", "SELL"): action = "BUY"
        s["trade"] = {"direction": action, "entry": entry, "sl": sl, "tp": tp}
        WAITING.pop(uid, None)
        await update.message.reply_text("💼 <b>Trade monitoring activated.</b>\nMonitoring now has priority.", parse_mode="HTML", reply_markup=trade_buttons())
        return

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if uid not in AUTH:
        await q.message.reply_text("🔒 Authenticate with /start first.")
        return
    d = q.data

    if d == "start":
        WAITING[uid] = "budget"
        await q.message.reply_text("💰 <b>Session Budget</b>\n\nEnter the budget in INR.", parse_mode="HTML")
    elif d == "stop":
        SESSIONS.pop(uid, None)
        WAITING.pop(uid, None)
        await q.message.reply_text("⏹ <b>Session stopped.</b>", parse_mode="HTML", reply_markup=menu())
    elif d == "status":
        s = SESSIONS.get(uid)
        await q.message.reply_text(
            f"📋 <b>SESSION</b>\n\nAsset: {s['asset']}\nBudget: ₹{s['budget']:,.2f}\nRisk: {s['risk']:.2f}%\nActive Trade: {'YES' if s.get('trade') else 'NO'}"
            if s else "No active session.", parse_mode="HTML" if s else None, reply_markup=menu())
    elif d == "active":
        s = SESSIONS.get(uid)
        if s and s.get("trade"):
            t = s["trade"]
            await q.message.reply_text(
                f"💼 <b>ACTIVE TRADE</b>\n\nAsset: {s['asset']}\nDirection: {t['direction']}\nEntry: {t['entry']}\nSL: {t['sl']}\nTP: {t['tp']}",
                parse_mode="HTML", reply_markup=trade_buttons())
        else:
            await q.message.reply_text("No active trade.")
    elif d == "help":
        await q.message.reply_text("English-only market analyzer. No MT5 connection and no automatic trade execution.")
    elif d.startswith("asset|"):
        asset = d.split("|", 1)[1]
        if asset not in ASSETS: return
        context.user_data["asset"] = asset
        WAITING[uid] = "risk"
        await q.message.reply_text("⚠️ <b>Risk Rate</b>\n\nEnter maximum risk per trade, e.g. <code>1</code>.", parse_mode="HTML")
    elif d == "taken":
        if uid in SESSIONS:
            WAITING[uid] = "trade"
            await q.message.reply_text("💼 Send actual trade levels:\n<code>entry stop_loss target</code>", parse_mode="HTML")
    elif d == "not_taken":
        await q.message.reply_text("🔎 Continuing to scan for new setups.")
    elif d == "stop_trade":
        if uid in SESSIONS: SESSIONS[uid]["trade"] = None
        await q.message.reply_text("🛑 Trade monitoring stopped. New-signal scanning resumed.", reply_markup=menu())
    elif d == "refresh":
        await analyze_user(uid, context, True)

async def minute_job(context):
    for uid in list(SESSIONS):
        await analyze_user(uid, context)

def main():
    check_config()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    app.job_queue.run_repeating(minute_job, interval=INTERVAL, first=INTERVAL)
    log.info("Sparks Forex AI Market Analyzer started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
