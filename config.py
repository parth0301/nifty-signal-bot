"""
Configuration — edit this file before running.
"""

import os

# ─────────────────────────────────────────────
# TELEGRAM  (get these from @BotFather on Telegram)
# Step 1: Message @BotFather → /newbot → copy the token
# Step 2: Message @userinfobot → copy your chat_id
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8254456458: AAEIznbwNHKf-v7l3jDwgntuQNrGm9J07ek")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "970123391")


# ─────────────────────────────────────────────
# SYMBOLS
# yf_symbol   → Yahoo Finance ticker
# nse_symbol  → NSE option chain API name
# label       → Display name in alerts
# ─────────────────────────────────────────────
SYMBOLS = [
    {
        "yf_symbol":  "^NSEI",
        "nse_symbol": "NIFTY",
        "label":      "NIFTY 50",
    },
    {
        "yf_symbol":  "^NSEBANK",
        "nse_symbol": "BANKNIFTY",
        "label":      "BANKNIFTY",
    },
]


# ─────────────────────────────────────────────
# STRATEGY PARAMETERS  (your exact settings)
# ─────────────────────────────────────────────
EMA_FAST        = 9       # Fast EMA period
EMA_SLOW        = 20      # Slow EMA period
ATR_PERIOD      = 7       # ATR period
ATR_MULTIPLIER  = 2       # ATR multiplier for SL/bands
MIN_RR_RATIO    = 1.5     # Minimum Risk:Reward to send alert


# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 5 * 60   # Scan every 5 minutes
MARKET_OPEN           = (9, 15)  # 9:15 AM IST
MARKET_CLOSE          = (15, 25) # 3:25 PM IST (5 min before square-off)
