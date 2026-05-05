# Trading Bot v2 — Setup Guide

This is an upgraded version of your trading bot with three programs you can run:

| File | What it does |
|---|---|
| `bot.py` | Main bot — trades SPY (or your chosen stock) on MA crossover, sends open/close PDF reports |
| `scanner.py` | RSI scanner — finds oversold stocks with double-bottom + bullish divergence, optional auto-buy on Telegram "yes" |
| `reports.py` | Can be run standalone to generate an on-demand PDF report |

All three share the same `config.py` and `utils.py`.

---

## What's new in v2

- ✅ **Better startup checks** — clear error messages if Alpaca keys are wrong (no more cryptic 401s)
- ✅ **Market open & close reports** — full PDF with positions, P/L, trades, plus Telegram summary
- ✅ **RSI scanner** — daily + hourly scans for oversold reversal candidates
- ✅ **Auto-buy on approval** — reply "yes" on Telegram to buy a signal
- ✅ **Organized logs and reports** — saved to `logs/` and `reports/` folders

---

## Setup steps

### 1. Replace your old files with the new ones
Copy these 6 files into your `C:\Users\tariq_r8atash\trading_bot` folder, replacing any existing versions:

- `bot.py`
- `scanner.py`
- `reports.py`
- `utils.py`
- `config.py`
- `requirements.txt`

### 2. Edit config.py with your real keys
Open `config.py` in Notepad. Fill in the 4 placeholders:

```python
ALPACA_API_KEY = "your paper key starting with PK"
ALPACA_SECRET_KEY = "your paper secret"
TELEGRAM_BOT_TOKEN = "your bot token from BotFather"
TELEGRAM_CHAT_ID = "your chat ID"
```

⚠️ **Important:** the Alpaca key MUST be from the **Paper Trading** dashboard (`/paper/` in URL). It must start with `PK`. If yours doesn't start with `PK`, regenerate it.

Save the file.

### 3. Install dependencies
Open PowerShell, navigate to your folder, and run:
```
cd C:\Users\tariq_r8atash\trading_bot
pip install -r requirements.txt
```

This adds `yfinance`, `reportlab`, and a few other libraries.

### 4. Run the bot
```
python bot.py
```

**On startup**, the bot now runs four checks and will tell you exactly what's wrong if anything fails:
- Config placeholders not replaced
- Alpaca key format wrong
- Alpaca authentication
- Telegram authentication

If all 4 pass, you'll see:
```
✅ Alpaca connection OK. Account equity: $100,000.00
✅ Telegram connection OK
```

And a Telegram message arrives.

### 5. (Optional) Run the scanner in a separate terminal
Open a **second** PowerShell window:
```
cd C:\Users\tariq_r8atash\trading_bot
python scanner.py
```

The scanner runs alongside the main bot. They both share the Alpaca account.

---

## How the RSI scanner works

It looks for stocks where ALL of these conditions are true:
1. Down at least **3%** today (configurable: `MIN_PCT_DROP`)
2. Daily volume above **500,000** (configurable: `MIN_VOLUME`)
3. RSI ≤ **30** (oversold)
4. RSI has touched 30 at least **twice** recently (double bottom on RSI)
5. **Bullish divergence**: price made a lower low while RSI made a higher low

When a signal is found, you get a Telegram message with the top candidate. Reply **"yes"** within 5 minutes to auto-buy it (5% of your equity by default).

### Scan schedule
- **Hourly** (during market hours): scans 1-hour timeframe
- **Daily** (after market close): scans daily timeframe

### Stock universe
Default: **S&P 500** (~500 stocks, ~2 minute scan).
Change `SCANNER_UNIVERSE` in `config.py` to:
- `"SP500"` — 500 stocks (fast)
- `"RUSSELL1000"` — 1,000 stocks (medium)
- `"ALL_US"` — 5,000+ stocks (slow, may rate-limit)

---

## Generating a report on demand

If you want a report right now (not waiting for market close):
```
python reports.py close
```
or
```
python reports.py open
```

The PDF goes into `reports/` and a summary is sent to Telegram.

---

## Folder structure after running

```
trading_bot/
├── bot.py
├── scanner.py
├── reports.py
├── utils.py
├── config.py
├── requirements.txt
├── logs/
│   └── bot.log
└── reports/
    ├── 2026-05-05_open_report.pdf
    └── 2026-05-05_close_report.pdf
```

---

## Troubleshooting

### "ALPACA_API_KEY must start with 'PK'"
You generated **live** keys instead of **paper** keys. Go to:
https://app.alpaca.markets/paper/dashboard/overview
Make sure URL has `/paper/`. Generate new keys from there.

### "ALPACA_API_KEY should be 20 characters but yours is X"
Extra spaces or missing characters in `config.py`. Re-copy the key carefully.

### "Telegram says: chat not found"
You haven't sent your bot a message yet. Open Telegram, find your bot, send "hi", run again.

### Scanner is slow / rate-limited
Switch `SCANNER_UNIVERSE` to `"SP500"` in config. Also, Yahoo Finance throttles heavy users — if you hit limits, wait an hour and try again.

### "ModuleNotFoundError: No module named 'yfinance'" (or 'reportlab')
Run `pip install -r requirements.txt` again.

---

## Important reminders

- **This is paper trading.** Real money trading is hard-locked off in the code. Don't attempt to switch to real money without months of consistent paper-trading success and a backtested strategy.
- **The RSI+divergence strategy is real but not magic.** Historical win rate ~50%. You can lose money even with good signals.
- **Auto-buy is optional.** If you don't reply "yes", nothing happens.
- **Always read the Telegram messages.** The bot tells you what it's doing.
