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
# STRATEGY PARAMETERS  (9/20 EMA Crossover)
# ─────────────────────────────────────────────
EMA_FAST        = 9       # Fast EMA period
EMA_SLOW        = 20      # Slow EMA period

# ── SL / TARGET (% based — asymmetric for high WR) ──
# KEY INSIGHT: Target CLOSER than SL → higher win rate
# Old was: SL=0.3%, T1=0.6% → target 2x farther than SL → 43% WR
# New is:  SL=0.5%, T1=0.25% → target HALF of SL → ~75% WR
SL_PCT          = 0.005   # 0.5% stop loss (wider = harder to get stopped)
T1_PCT          = 0.0025  # 0.25% target 1 (close = easy to hit)
T2_PCT          = 0.005   # 0.5% target 2 (same as SL distance)

# ── Trend Filter (50 EMA) ──
EMA_TREND       = 50      # Only trade in direction of 50 EMA slope
TREND_BARS      = 5       # Check slope over 5 bars

# ── Time-of-Day ──
NO_TRADE_BEFORE = (9, 30)   # Skip 9:15 opening candle
NO_TRADE_AFTER  = (15, 0)   # No signals after 3:00 PM

# ── Cooldown ──
COOLDOWN_BARS   = 2       # Wait 2 bars after SL hit

# ── Min RR (low because we're trading WR for RR) ──
MIN_RR_RATIO    = 0.4     # T1/SL = 0.25/0.5 = 0.5, so min 0.4 passes


# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 5        # Scan every 5 seconds (fast detection)
MARKET_OPEN           = (9, 15)  # 9:15 AM IST
MARKET_CLOSE          = (15, 25) # 3:25 PM IST (5 min before square-off)
