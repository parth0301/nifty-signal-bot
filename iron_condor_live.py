"""
Iron Condor Live Alert System for NIFTY 50
Sends Telegram alerts based on defined entry/exit/adjustment conditions.
Runs every 5 minutes via scheduler.
"""

import os
import time
import json
import logging
import datetime
import requests
import yfinance as yf
import pytz
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8254456458:AAEIznbwNHKf-v7l3jDwgntuQNrGm9J07ek")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "970123391")

NIFTY_TICKER = "^NSEI"
VIX_TICKER   = "^INDIAVIX"

LOT_SIZE     = 50
SPREAD_WIDTH = 300
WING_OFFSET  = 250   # distance from spot to short strike
VIX_LIMIT    = 16.0
GAP_LIMIT    = 0.007   # 0.7%
RANGE_LIMIT  = 0.004   # 0.4%
ADJUST_ZONE  = 100     # points from short strike

STATE_FILE   = Path("trade_state.json")
IST          = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("iron_condor.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            # Reset if it's a new day
            today = datetime.date.today().isoformat()
            if state.get("date") != today:
                log.info("New day — resetting trade state.")
                return default_state()
            return state
        except Exception as e:
            log.warning(f"Failed to load state: {e}")
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def default_state():
    return {
        "date": datetime.date.today().isoformat(),
        "status": "IDLE",        # IDLE | ACTIVE | CLOSED
        "trade": None,
        "adjust_call_sent": False,
        "adjust_put_sent": False,
    }


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram alert sent.")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────
def get_nifty_data():
    """Fetch spot price, previous close, and intraday data."""
    try:
        ticker = yf.Ticker(NIFTY_TICKER)

        # Spot price
        fast = ticker.fast_info
        spot = float(fast.last_price)

        # Previous close
        hist = ticker.history(period="5d", interval="1d")
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else spot

        # Today's open
        today_open = float(hist["Open"].iloc[-1]) if len(hist) >= 1 else spot

        # Intraday last 30 min (5-min bars)
        intraday = ticker.history(period="1d", interval="5m")
        now_ist = datetime.datetime.now(IST)
        thirty_ago = now_ist - datetime.timedelta(minutes=30)

        intraday.index = intraday.index.tz_convert(IST)
        recent = intraday[intraday.index >= thirty_ago]

        if len(recent) > 0:
            range_high = float(recent["High"].max())
            range_low  = float(recent["Low"].min())
            intraday_range_pct = (range_high - range_low) / spot
        else:
            range_high = range_low = spot
            intraday_range_pct = 0.0

        return {
            "spot": spot,
            "prev_close": prev_close,
            "today_open": today_open,
            "intraday_range_pct": intraday_range_pct,
        }
    except Exception as e:
        log.error(f"NIFTY data fetch failed: {e}")
        return None


def get_vix():
    """Fetch India VIX. Returns None if unavailable."""
    try:
        vix_ticker = yf.Ticker(VIX_TICKER)
        fast = vix_ticker.fast_info
        vix = float(fast.last_price)
        log.info(f"India VIX: {vix:.2f}")
        return vix
    except Exception as e:
        log.warning(f"VIX fetch failed (will skip filter): {e}")
        return None


# ─────────────────────────────────────────────
# STRIKE CALCULATION
# ─────────────────────────────────────────────
def round_to_50(value):
    return round(value / 50) * 50


def calculate_strikes(spot):
    lower_short = round_to_50(spot - WING_OFFSET)
    upper_short = round_to_50(spot + WING_OFFSET)
    lower_long  = round_to_50(lower_short - SPREAD_WIDTH)
    upper_long  = round_to_50(upper_short + SPREAD_WIDTH)
    return lower_short, upper_short, lower_long, upper_long


# ─────────────────────────────────────────────
# PREMIUM ESTIMATION
# ─────────────────────────────────────────────
def estimate_premiums(spot):
    short_prem = spot * 0.004   # ~0.4% of spot
    long_prem  = short_prem * 0.35  # ~35% of short

    sell_pe = short_prem
    sell_ce = short_prem
    buy_pe  = long_prem
    buy_ce  = long_prem

    net_credit = (sell_pe + sell_ce) - (buy_pe + buy_ce)
    return round(net_credit, 2)


def calculate_trade_metrics(net_credit):
    max_profit = net_credit * LOT_SIZE
    max_loss   = (SPREAD_WIDTH - net_credit) * LOT_SIZE
    target     = max_profit * 0.50
    stop_loss  = net_credit * 2 * LOT_SIZE
    return {
        "net_credit": round(net_credit, 2),
        "max_profit": round(max_profit, 2),
        "max_loss":   round(max_loss, 2),
        "target":     round(target, 2),
        "stop_loss":  round(stop_loss, 2),
    }


# ─────────────────────────────────────────────
# ENTRY CONDITIONS CHECK
# ─────────────────────────────────────────────
def check_entry_conditions(data, vix):
    now_ist  = datetime.datetime.now(IST)
    weekday  = now_ist.weekday()   # 0=Mon, 1=Tue, 2=Wed
    hour     = now_ist.hour
    minute   = now_ist.minute
    reasons  = []

    # 1. Day: Mon–Wed only
    if weekday not in (0, 1, 2):
        reasons.append("Not Mon–Wed")

    # 2. Time: 10:30 AM – 12:30 PM
    entry_start = now_ist.replace(hour=10, minute=30, second=0)
    entry_end   = now_ist.replace(hour=12, minute=30, second=0)
    if not (entry_start <= now_ist <= entry_end):
        reasons.append(f"Outside entry window (now {hour}:{minute:02d})")

    # 3. VIX < 16
    if vix is not None:
        if vix >= VIX_LIMIT:
            reasons.append(f"VIX too high: {vix:.2f}")
    else:
        log.warning("VIX unavailable — skipping VIX filter.")

    # 4. Gap condition
    gap_pct = abs(data["today_open"] - data["prev_close"]) / data["prev_close"]
    if gap_pct >= GAP_LIMIT:
        reasons.append(f"Gap too large: {gap_pct*100:.2f}%")

    # 5. 30-min range
    if data["intraday_range_pct"] >= RANGE_LIMIT:
        reasons.append(f"30-min range too wide: {data['intraday_range_pct']*100:.2f}%")

    return reasons   # empty = all conditions pass


# ─────────────────────────────────────────────
# ALERT FORMATTERS
# ─────────────────────────────────────────────
def format_entry_alert(spot, ls, us, ll, ul, metrics):
    return (
        f"🔵 <b>IRON CONDOR SETUP</b>\n\n"
        f"Spot: <b>{spot:.0f}</b>\n\n"
        f"PUT SIDE:\n"
        f"  SELL {ls} PE\n"
        f"  BUY  {ll} PE\n\n"
        f"CALL SIDE:\n"
        f"  SELL {us} CE\n"
        f"  BUY  {ul} CE\n\n"
        f"Range: <b>{ls} – {us}</b>\n\n"
        f"Net Credit : ₹{metrics['net_credit']:.2f} pts\n"
        f"Max Profit : ₹{metrics['max_profit']:,.0f}\n"
        f"Max Loss   : ₹{metrics['max_loss']:,.0f}\n"
        f"Target     : ₹{metrics['target']:,.0f}\n"
        f"Stop Loss  : ₹{metrics['stop_loss']:,.0f}"
    )


def format_adjust_call():
    return "🟡 <b>ADJUST CALL SIDE</b> – Price near upper range\nConsider rolling up the call spread."


def format_adjust_put():
    return "🟡 <b>ADJUST PUT SIDE</b> – Price near lower range\nConsider rolling down the put spread."


def format_exit_alert(reason):
    return f"🔴 <b>EXIT TRADE NOW</b>\n\nReason: {reason}"


# ─────────────────────────────────────────────
# MAIN LOOP LOGIC
# ─────────────────────────────────────────────
def run_cycle():
    state = load_state()
    now_ist = datetime.datetime.now(IST)
    log.info(f"--- Cycle start: {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST | Status: {state['status']} ---")

    # ── FETCH DATA ──
    data = get_nifty_data()
    if data is None:
        log.error("Could not fetch NIFTY data. Skipping cycle.")
        return

    spot = data["spot"]
    vix  = get_vix()
    log.info(f"Spot: {spot:.2f}  |  VIX: {vix}")

    # ── CLOSED STATE: nothing to do ──
    if state["status"] == "CLOSED":
        log.info("Trade already closed today. No action.")
        return

    # ── ACTIVE STATE: monitor trade ──
    if state["status"] == "ACTIVE":
        trade = state["trade"]
        ls = trade["lower_short"]
        us = trade["upper_short"]

        # Time exit
        exit_time = now_ist.replace(hour=14, minute=45, second=0)
        if now_ist >= exit_time:
            send_telegram(format_exit_alert("Time Exit (2:45 PM)"))
            state["status"] = "CLOSED"
            save_state(state)
            log.info("Time exit triggered.")
            return

        # Adjustment zone check
        if spot >= (us - ADJUST_ZONE) and not state["adjust_call_sent"]:
            send_telegram(format_adjust_call())
            state["adjust_call_sent"] = True
            log.info("Adjust CALL alert sent.")

        if spot <= (ls + ADJUST_ZONE) and not state["adjust_put_sent"]:
            send_telegram(format_adjust_put())
            state["adjust_put_sent"] = True
            log.info("Adjust PUT alert sent.")

        # Strong breakout exit
        if spot > us + SPREAD_WIDTH or spot < ls - SPREAD_WIDTH:
            send_telegram(format_exit_alert("Strong Breakout"))
            state["status"] = "CLOSED"
            save_state(state)
            log.info("Breakout exit triggered.")
            return

        save_state(state)
        return

    # ── IDLE STATE: check for entry ──
    reasons = check_entry_conditions(data, vix)
    if reasons:
        log.info(f"Entry conditions NOT met: {'; '.join(reasons)}")
        return

    log.info("✅ All entry conditions met — sending ENTRY alert.")

    ls, us, ll, ul = calculate_strikes(spot)
    net_credit = estimate_premiums(spot)
    metrics    = calculate_trade_metrics(net_credit)
    msg        = format_entry_alert(spot, ls, us, ll, ul, metrics)
    send_telegram(msg)

    state["status"] = "ACTIVE"
    state["trade"]  = {
        "lower_short": ls,
        "upper_short": us,
        "lower_long":  ll,
        "upper_long":  ul,
        "metrics":     metrics,
        "entry_spot":  spot,
    }
    save_state(state)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Iron Condor Alert System starting...")
    send_telegram("🟢 Iron Condor Alert System <b>STARTED</b>\nMonitoring NIFTY every 5 minutes.")

    while True:
        try:
            run_cycle()
        except Exception as e:
            log.exception(f"Unexpected error in cycle: {e}")
        time.sleep(300)   # 5 minutes
