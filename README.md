# VoidAI — Setup Guide

## 1. Edit bot.py — Set your tokens (top of file)

```python
BOT_TOKEN       = "PASTE_YOUR_TELEGRAM_BOT_TOKEN"
BOOTSTRAP_ADMIN = YOUR_TELEGRAM_USER_ID   # numeric, e.g. 123456789
```

## 2. Run

```bash
bash setup_run.sh
```

Or manually:
```bash
pip install -r requirements.txt
python bot.py
```

## 3. Add API Keys (via Telegram as admin)

```
/addkey gemini  YOUR_GEMINI_KEY
/addkey grok    YOUR_GROK_KEY
/addkey mistral YOUR_MISTRAL_KEY
```

The message is deleted automatically to protect your key.

## 4. Admin Commands

| Command | What it does |
|---------|-------------|
| `/admin` | Open admin panel |
| `/addkey gemini\|grok\|mistral KEY` | Add API key |
| `/delkey gemini\|grok\|mistral INDEX` | Delete key by number |
| `/listkeys` | List all keys (masked) |
| `/addadmin USER_ID` | Add/remove admin |
| `/ban USER_ID` | Ban/unban user |
| `/limit MULTIPLIER` | Set global usage multiplier |
| `/limit USER_ID MULTIPLIER` | Set per-user multiplier |
| `/stats` | Usage stats |

## 5. User Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome + quick menu |
| `/switch` | Change AI model |
| `/new` | Clear chat history |
| `/web query` | Force web search |
| `/voice` | Voice reply settings |
| `/help` | Help menu |

## Models

- **Void Lite** — Fast, no web search
- **Void Flash** — Balanced (default)
- **Void Pro** — Auto web search on every message

## AI Provider Priority

1. Gemini (tries all models: 2.0-flash → 2.0-flash-lite → 1.5-flash)
2. Grok
3. Mistral

For **images**: Gemini vision → Mistral vision

## Voice Replies

Powered by Microsoft Edge TTS (free, no API key needed).
10 Indian voices available (Hindi, English, Tamil, Telugu, Kannada).

## Notes

- `void_config.json` is created automatically next to `bot.py`
- All settings (keys, bans, limits) persist in that file
- Rate limits are per-hour rolling windows
- Keys giving 403 (invalid) are permanently skipped; 429 (rate limit) retries next message
