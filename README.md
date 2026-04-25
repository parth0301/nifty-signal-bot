# NIFTY Signal Bot 📈
**Strategy:** 9 EMA / 20 EMA crossover + ATR(7, multiplier 2)  
**Instruments:** NIFTY 50 · BANKNIFTY Options  
**Timeframe:** 15 Minutes  
**Alerts:** Telegram (live, 24/7)  
**Data:** Free — yfinance + NSE India (no API key needed)

---

## What This Bot Does

- Scans NIFTY and BANKNIFTY every 5 minutes during market hours (9:15–15:25 IST)
- Detects 9/20 EMA crossovers on the 15m chart
- Calculates ATR(7)-based Stop Loss and Targets
- Sends a Telegram alert **to you** with full trade details
- **You decide** whether to take the entry — bot never auto-trades

### Each Telegram Alert Includes:
- BUY / SELL signal with direction
- Entry price zone
- Stop Loss (ATR-based, multiplier 2)
- Target 1 and Target 2 with R:R ratios
- ATM CE or PE option to buy
- Signal strength (Strong / Moderate / Weak)
- Live NSE option chain snapshot (ATM LTP)

---

## Setup — Step by Step

### Step 1: Create Your Telegram Bot (5 minutes)

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Follow prompts → you'll get a **bot token** like `7123456789:AAF...`
4. Search **@userinfobot** → send any message → copy your **chat_id**

### Step 2: Install Python Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Configure the Bot

Open `config.py` and fill in:

```python
TELEGRAM_BOT_TOKEN = "7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxx"
TELEGRAM_CHAT_ID   = "123456789"
```

Everything else is already set to your exact strategy parameters.

### Step 4: Run Backtest First

```bash
python backtest.py
```

This tests the 9/20 EMA + ATR strategy on 60 days of historical 15m data.  
Results saved to `backtest_NIFTY.csv` and `backtest_BANKNIFTY.csv`.

### Step 5: Run the Bot

```bash
python bot.py
```

You'll get a Telegram message confirming it's live. Done.

---

## Deploy 24/7 (Free Options)

### Option A: Railway.app ⭐ Recommended

1. Go to [railway.app](https://railway.app) → sign up free
2. New Project → Deploy from GitHub repo
3. Push this folder to a GitHub repo first:
   ```bash
   git init
   git add .
   git commit -m "nifty bot"
   gh repo create nifty-bot --private --push --source=.
   ```
4. In Railway: add environment variables:
   - `TELEGRAM_BOT_TOKEN` = your token
   - `TELEGRAM_CHAT_ID` = your chat id
5. Deploy → bot runs 24/7 for free (500 hrs/month free tier)

### Option B: Render.com

1. Go to [render.com](https://render.com) → New → Background Worker
2. Connect your GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add environment variables same as above

### Option C: Your Own PC / Laptop

Keep it running with:
```bash
# On Mac/Linux:
nohup python bot.py &

# On Windows (PowerShell):
Start-Process python -ArgumentList "bot.py" -NoNewWindow
```

---

## Strategy Logic

```
Signal: 9 EMA crosses ABOVE 20 EMA → BUY (look for CE)
Signal: 9 EMA crosses BELOW 20 EMA → SELL (look for PE)

Stop Loss  = entry_price - (2 × ATR7)   [for BUY]
Target 1   = entry_price + (3 × ATR7)   → R:R 1:1.5
Target 2   = entry_price + (5 × ATR7)   → R:R 1:2.5

Signal filtered out if RR < 1.5
```

---

## File Structure

```
nifty-signal-bot/
├── bot.py            ← Main signal engine (runs 24/7)
├── backtest.py       ← Historical backtest
├── config.py         ← Your settings (edit this)
├── requirements.txt  ← pip install -r requirements.txt
├── railway.toml      ← Railway deployment config
├── Procfile          ← For Render / Heroku
└── README.md         ← This file
```

---

## Customising

All tweakable values are in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `EMA_FAST` | 9 | Fast EMA period |
| `EMA_SLOW` | 20 | Slow EMA period |
| `ATR_PERIOD` | 7 | ATR period |
| `ATR_MULTIPLIER` | 2 | SL multiplier |
| `MIN_RR_RATIO` | 1.5 | Skip signals below this RR |
| `SCAN_INTERVAL_SECONDS` | 300 | Scan every 5 minutes |

---

## Important Notes

- yfinance 15m data has a 60-day lookback limit — backtest uses max 60 days
- NSE option chain scraping may occasionally fail if NSE changes their website; bot handles this gracefully and still sends the signal
- Always paper trade first before using signals with real money
- This is a signal tool — not financial advice

---

## Adding More Features Later

- [ ] Telegram `/status` command to check if bot is alive
- [ ] Multiple timeframe confirmation (5m + 15m)
- [ ] Volume filter (only signal if volume spike)
- [ ] OI change from option chain (smart money indicator)
- [ ] Day-of-week filter (avoid Monday/expiry days)
