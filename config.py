"""
Configuration — edit this file before running.
"""

import os

# ─────────────────────────────────────────────
# TELEGRAM  (get these from @BotFather on Telegram)
# Step 1: Message @BotFather → /newbot → copy the token
# Step 2: Message @userinfobot → copy your chat_id
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8254456458:AAEIznbwNHKf-v7l3jDwgntuQNrGm9J07ek")
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
# STRATEGY PARAMETERS
# ─────────────────────────────────────────────

# ── EMA Crossover ──
EMA_FAST        = 9       # Fast EMA period
EMA_SLOW        = 20      # Slow EMA period
MIN_RR_RATIO    = 1.5     # Minimum Risk:Reward to send alert

# ── Trend Filter (50 EMA slope) ──
EMA_TREND       = 50      # Trend-direction EMA period
TREND_SLOPE_BARS = 3      # Number of bars to check slope direction

# ── RSI Confirmation ──
RSI_PERIOD      = 14      # RSI lookback period
RSI_BUY_MIN     = 55      # RSI must be ABOVE this for BUY signals
RSI_SELL_MAX    = 45      # RSI must be BELOW this for SELL signals

# ── Volume Spike Filter ──
VOL_AVG_PERIOD  = 20      # Period for volume moving average
VOL_SPIKE_MULT  = 1.2     # Volume must be >= this * avg to confirm signal

# ── Candle Body Strength Filter ──
# Crossover candle body must be >= this fraction of the total range (high-low)
# Filters out weak doji/spinning-top candles that give false crossovers
BODY_RATIO_MIN  = 0.40

# ── ATR Expansion Filter ──
# Current ATR must be > avg ATR of last N bars (market is actually moving)
ATR_PERIOD      = 14      # ATR calculation period
ATR_MULTIPLIER  = 2       # ATR multiplier for SL/target bands
ATR_AVG_PERIOD  = 20      # Period for average ATR comparison
ATR_EXPAND_MULT = 1.0     # Current ATR must be >= this * avg ATR

# ── Time-of-Day Restrictions ──
# Skip the chaotic opening and low-liquidity close
NO_TRADE_BEFORE = (9, 30)   # No signals before 9:30 AM IST (skip first 15-min candle)
NO_TRADE_AFTER  = (14, 45)  # No signals after 2:45 PM IST (avoid last-hour whipsaws)

# ── Cooldown After SL ──
# After a stop-loss hit, wait N candles before taking next signal on same symbol
# Prevents whipsaw chains (e.g., 3 consecutive SLs in ranging market)
COOLDOWN_BARS   = 3


# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 5 * 60   # Scan every 5 minutes
MARKET_OPEN           = (9, 15)  # 9:15 AM IST
MARKET_CLOSE          = (15, 25) # 3:25 PM IST (5 min before square-off)
