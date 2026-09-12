# Sparks Forex — Gemini AI Market Analyzer

Telegram-only AI market analysis bot.

This version replaces OpenRouter with the direct Google Gemini API while
keeping the existing bot structure and features.

## Railway Variables

```text
TELEGRAM_BOT_TOKEN=your_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
SIGNAL_CHANNEL_ID=
UPDATE_INTERVAL_SECONDS=60
NEWS_LIMIT=5
```

`SIGNAL_CHANNEL_ID` is optional.

Do not put API keys or bot tokens in GitHub source code.

## Run locally

```bash
pip install -r requirements.txt
python main.py
```
