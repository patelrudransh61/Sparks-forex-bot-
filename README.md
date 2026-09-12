# Sparks Forex — Gemini AI Market Analyzer

Telegram-only AI market analysis bot.

## AI change

This version replaces OpenRouter with the direct Google Gemini API.

The rest of the bot flow and user-facing features are kept the same.

## Railway Variables

Set these environment variables:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `GEMINI_MODEL` (optional; default: `gemini-2.5-flash`)
- `SIGNAL_CHANNEL_ID` (optional)
- `UPDATE_INTERVAL_SECONDS` (optional; default: `60`)
- `NEWS_LIMIT` (optional; default: `5`)

Do not commit your Gemini API key to GitHub.

## Run locally

```bash
pip install -r requirements.txt
python main.py
```

## Notes

- No OpenRouter dependency is used.
- Gemini is used for the AI analysis response.
- The bot remains Telegram-only.
- No MT5 connection and no automatic trade execution.
