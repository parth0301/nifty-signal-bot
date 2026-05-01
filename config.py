"""
Configuration — Iron Condor Signal Bot

Strategy: Daily Iron Condor (sell premium both sides, profit in range)
Signals: Entry / Adjustment / Exit alerts via Telegram
"""

import os

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8254456458:AAEIznbwNHKf-v7l3jDwgntuQNrGm9J07ek")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "970123391")


# ─────────────────────────────────────────────
# SYMBOLS  (market-specific strike config)
# ─────────────────────────────────────────────
SYMBOLS = [
    {
        "yf_symbol":     "^NSEI",
        "nse_symbol":    "NIFTY",
        "label":         "NIFTY",
        "lot_size":      25,
        "strike_gap":    50,       # strikes are multiples of 50
        "sell_offset":   250,      # sell strike = ATM ± 250
        "spread_width":  300,      # buy strike  = sell ± 300
        "expiry_weekday": 3,       # Thursday = 3 (Mon=0)
    },
    {
        "yf_symbol":     "^NSEBANK",
        "nse_symbol":    "BANKNIFTY",
        "label":         "BANKNIFTY",
        "lot_size":      15,
        "strike_gap":    100,      # strikes are multiples of 100
        "sell_offset":   300,      # sell strike = ATM ± 300
        "spread_width":  300,      # buy strike  = sell ± 300
        "expiry_weekday": 2,       # Wednesday = 2 (Mon=0)
    },
]


# ─────────────────────────────────────────────
# IRON CONDOR TIMING
# ─────────────────────────────────────────────
IC_ENTRY_TIME       = (9, 45)     # Send ENTRY alert at 9:45 AM IST
IC_EXIT_TIME        = (15, 15)    # Send EXIT alert at 3:15 PM IST
IC_ADJUST_BUFFER    = 50          # Adjustment alert when spot is within 50 pts of sell strike

# ── Target / SL ──
IC_TARGET_PCT       = 0.50        # Target = 50% of max profit


# ─────────────────────────────────────────────
# TIMING
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 5 * 60    # Scan every 5 minutes
MARKET_OPEN           = (9, 15)   # 9:15 AM IST
MARKET_CLOSE          = (15, 25)  # 3:25 PM IST
