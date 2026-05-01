"""
NIFTY / BANKNIFTY Iron Condor Signal Bot

Strategy: Daily Iron Condor (sell premium both sides)
  - Enter at 9:45 AM → 4-leg Iron Condor
  - Monitor throughout day → Adjustment alerts if breached
  - Exit at 3:15 PM → Close all legs

Signals are EXECUTION-READY (exact strikes, expiry, P&L)
Directly usable in Zerodha Kite.

Data: yfinance (spot price) + NSE API (option premiums)
Alerts: Telegram
"""

import os
import time
import logging
from datetime import datetime, timedelta, time as dtime, date
import pytz

import pandas as pd
import numpy as np
import yfinance as yf
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SYMBOLS,
    IC_ENTRY_TIME,
    IC_EXIT_TIME,
    IC_ADJUST_BUFFER,
    IC_TARGET_PCT,
    SCAN_INTERVAL_SECONDS,
    MARKET_OPEN,
    MARKET_CLOSE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────
# DAILY STATE TRACKING (per symbol)
# ─────────────────────────────────────────────
# Tracks position lifecycle: entry → adjustments → exit
daily_state: dict[str, dict] = {}


def get_state(label: str) -> dict:
    """Get or create today's state for a symbol. Resets on new day."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if label not in daily_state or daily_state[label]["date"] != today:
        daily_state[label] = {
            "date": today,
            "entry_sent": False,
            "exit_sent": False,
            "put_adjusted": False,
            "call_adjusted": False,
            "spot_at_entry": None,
            "atm": None,
            "sell_put": None,
            "buy_put": None,
            "sell_call": None,
            "buy_call": None,
            "expiry_str": None,
            "max_profit": None,
            "max_loss": None,
            "target": None,
        }
    return daily_state[label]


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_spot(symbol: str) -> float | None:
    """Fetch current spot price from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="15m")
        if df.empty:
            log.warning(f"No data for {symbol}")
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        log.error(f"Error fetching spot {symbol}: {e}")
        return None


def fetch_nse_premiums(nse_symbol: str, strikes: list[int], expiry_str: str) -> dict:
    """
    Fetch option premiums for specific strikes from NSE option chain.
    Returns dict: { (strike, "CE"|"PE"): premium }
    Falls back to empty dict if NSE is blocked.
    """
    url_map = {
        "NIFTY": "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
        "BANKNIFTY": "https://www.nseindia.com/api/option-chain-indices?symbol=BANKNIFTY",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/",
    }
    url = url_map.get(nse_symbol)
    if not url:
        return {}

    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        resp = session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        premiums = {}
        for rec in data.get("records", {}).get("data", []):
            sp = rec.get("strikePrice")
            if sp not in strikes:
                continue
            if "CE" in rec:
                premiums[(sp, "CE")] = rec["CE"].get("lastPrice", 0)
            if "PE" in rec:
                premiums[(sp, "PE")] = rec["PE"].get("lastPrice", 0)

        log.info(f"NSE premiums fetched: {len(premiums)} prices for {nse_symbol}")
        return premiums

    except Exception as e:
        log.warning(f"NSE option chain fetch failed for {nse_symbol}: {e}")
        return {}


# ─────────────────────────────────────────────
# STRIKE CALCULATION
# ─────────────────────────────────────────────

def compute_atm(spot: float, strike_gap: int) -> int:
    """Round spot to nearest strike_gap → ATM strike."""
    return round(spot / strike_gap) * strike_gap


def compute_ic_strikes(atm: int, sell_offset: int, spread_width: int, strike_gap: int) -> dict:
    """
    Compute 4 Iron Condor legs from ATM.
    Returns dict with sell_put, buy_put, sell_call, buy_call.
    """
    sell_put = atm - sell_offset
    buy_put = sell_put - spread_width
    sell_call = atm + sell_offset
    buy_call = sell_call + spread_width

    # Ensure all are multiples of strike_gap
    sell_put = round(sell_put / strike_gap) * strike_gap
    buy_put = round(buy_put / strike_gap) * strike_gap
    sell_call = round(sell_call / strike_gap) * strike_gap
    buy_call = round(buy_call / strike_gap) * strike_gap

    return {
        "sell_put": sell_put,
        "buy_put": buy_put,
        "sell_call": sell_call,
        "buy_call": buy_call,
    }


def get_nearest_expiry(expiry_weekday: int) -> str:
    """
    Find nearest weekly expiry (by weekday).
    NIFTY = Thursday (3), BANKNIFTY = Wednesday (2).
    Returns formatted string like "08 May".
    Never returns today (avoid entering on expiry day).
    """
    today = datetime.now(IST).date()
    days_ahead = (expiry_weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # skip today's expiry
    expiry = today + timedelta(days=days_ahead)
    return expiry.strftime("%d %b")


# ─────────────────────────────────────────────
# P&L CALCULATION
# ─────────────────────────────────────────────

def calculate_ic_pnl(premiums: dict, strikes: dict, lot_size: int, spread_width: int) -> dict:
    """
    Calculate max profit, max loss, target from option premiums.
    Returns dict with max_profit, max_loss, target (all in ₹).
    """
    sp_premium = premiums.get((strikes["sell_put"], "PE"), 0)
    bp_premium = premiums.get((strikes["buy_put"], "PE"), 0)
    sc_premium = premiums.get((strikes["sell_call"], "CE"), 0)
    bc_premium = premiums.get((strikes["buy_call"], "CE"), 0)

    net_credit = (sp_premium + sc_premium) - (bp_premium + bc_premium)

    if net_credit <= 0:
        # Fallback: estimate net credit as ~30% of spread width
        net_credit = spread_width * 0.30
        log.warning(f"Net credit was ≤0, using estimate: {net_credit:.0f} pts")

    max_profit = net_credit * lot_size
    max_loss = (spread_width - net_credit) * lot_size
    target = max_profit * IC_TARGET_PCT

    return {
        "net_credit": round(net_credit, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "target": round(target, 2),
        "premiums": {
            "sell_put": sp_premium,
            "buy_put": bp_premium,
            "sell_call": sc_premium,
            "buy_call": bc_premium,
        },
        "premiums_available": sp_premium > 0 or sc_premium > 0,
    }


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def format_entry_message(label: str, spot: float, atm: int, strikes: dict,
                         expiry: str, pnl: dict) -> str:
    """Format the ENTRY ALERT for Iron Condor."""
    now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")

    premium_note = ""
    if not pnl["premiums_available"]:
        premium_note = "\n⚠️ <i>Premiums unavailable — check LTP on Kite before executing.</i>\n"

    msg = (
        "🔵🔵🔵\n"
        "<b>ENTRY ALERT — IRON CONDOR</b>\n"
        "🔵🔵🔵\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{label}</b> | <code>{now}</code>\n"
        f"Spot: <code>₹{spot:,.2f}</code> | ATM: <code>{atm}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"\n<b>PUT SIDE:</b>\n"
        f"  SELL <code>{label} {strikes['sell_put']} PE</code> ({expiry})\n"
        f"  BUY  <code>{label} {strikes['buy_put']} PE</code> ({expiry})\n"
        f"\n<b>CALL SIDE:</b>\n"
        f"  SELL <code>{label} {strikes['sell_call']} CE</code> ({expiry})\n"
        f"  BUY  <code>{label} {strikes['buy_call']} CE</code> ({expiry})\n"
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Range: <code>{strikes['sell_put']} – {strikes['sell_call']}</code>\n"
        f"Spread: <code>{abs(strikes['sell_call'] - strikes['buy_call'])} pts</code>\n"
        f"\nMax Profit: <code>₹{pnl['max_profit']:,.0f}</code>\n"
        f"Max Loss:   <code>₹{pnl['max_loss']:,.0f}</code>\n"
        f"Target:     <code>₹{pnl['target']:,.0f}</code> ({IC_TARGET_PCT*100:.0f}%)\n"
        f"Stop Loss:  <code>₹{pnl['max_loss']:,.0f}</code>\n"
        f"{premium_note}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Execute all 4 legs simultaneously on Kite.</i>"
    )
    return msg


def format_adjustment_message(label: str, side: str, strikes: dict,
                              spot: float, expiry: str) -> str:
    """Format the ADJUSTMENT ALERT."""
    now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")

    if side == "CALL":
        exit_sell = f"{label} {strikes['sell_call']} CE"
        exit_buy = f"{label} {strikes['buy_call']} CE"
        reason = f"Spot ₹{spot:,.2f} approaching SELL {strikes['sell_call']} CE"
    else:
        exit_sell = f"{label} {strikes['sell_put']} PE"
        exit_buy = f"{label} {strikes['buy_put']} PE"
        reason = f"Spot ₹{spot:,.2f} approaching SELL {strikes['sell_put']} PE"

    msg = (
        "🟡🟡🟡\n"
        f"<b>ADJUST {side} SIDE</b>\n"
        "🟡🟡🟡\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{label}</b> | <code>{now}</code>\n"
        f"⚠️ {reason}\n"
        f"\n<b>EXIT these legs:</b>\n"
        f"  SELL <code>{exit_sell}</code>\n"
        f"  BUY  <code>{exit_buy}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Close the breached side immediately on Kite.</i>"
    )
    return msg


def format_exit_message(label: str) -> str:
    """Format the EXIT ALL alert."""
    now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")

    msg = (
        "🔴🔴🔴\n"
        "<b>EXIT ALL POSITIONS</b>\n"
        "🔴🔴🔴\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{label}</b> | <code>{now}</code>\n"
        "\nClose all 4 legs immediately.\n"
        "Book current P&L.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Square off all positions on Kite NOW.</i>"
    )
    return msg


def send_startup_message():
    now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    symbols_str = " | ".join(cfg["label"] for cfg in SYMBOLS)
    msg = (
        "🤖 <b>Iron Condor Bot is LIVE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Started: <code>{now}</code>\n"
        "Strategy: <code>Iron Condor (sell premium)</code>\n"
        f"Instruments: <code>{symbols_str}</code>\n"
        f"Entry: <code>{IC_ENTRY_TIME[0]}:{IC_ENTRY_TIME[1]:02d} IST</code>\n"
        f"Exit: <code>{IC_EXIT_TIME[0]}:{IC_EXIT_TIME[1]:02d} IST</code>\n"
        f"Scan: <code>Every {SCAN_INTERVAL_SECONDS // 60} min</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Waiting for market open... 🎯"
    )
    send_telegram(msg)


# ─────────────────────────────────────────────
# CORE LOGIC
# ─────────────────────────────────────────────

def enter_iron_condor(cfg: dict, spot: float, state: dict):
    """Calculate IC strikes, fetch premiums, send ENTRY alert."""
    label = cfg["label"]

    atm = compute_atm(spot, cfg["strike_gap"])
    strikes = compute_ic_strikes(atm, cfg["sell_offset"], cfg["spread_width"], cfg["strike_gap"])
    expiry = get_nearest_expiry(cfg["expiry_weekday"])

    log.info(
        f"{label}: IC Entry — ATM={atm} | "
        f"SP={strikes['sell_put']} BP={strikes['buy_put']} | "
        f"SC={strikes['sell_call']} BC={strikes['buy_call']} | "
        f"Expiry={expiry}"
    )

    # Fetch premiums from NSE
    all_strikes = [strikes["sell_put"], strikes["buy_put"],
                   strikes["sell_call"], strikes["buy_call"]]
    premiums = fetch_nse_premiums(cfg["nse_symbol"], all_strikes, expiry)

    # Calculate P&L
    pnl = calculate_ic_pnl(premiums, strikes, cfg["lot_size"], cfg["spread_width"])

    # Update state
    state["entry_sent"] = True
    state["spot_at_entry"] = spot
    state["atm"] = atm
    state["sell_put"] = strikes["sell_put"]
    state["buy_put"] = strikes["buy_put"]
    state["sell_call"] = strikes["sell_call"]
    state["buy_call"] = strikes["buy_call"]
    state["expiry_str"] = expiry
    state["max_profit"] = pnl["max_profit"]
    state["max_loss"] = pnl["max_loss"]
    state["target"] = pnl["target"]

    # Send Telegram
    msg = format_entry_message(label, spot, atm, strikes, expiry, pnl)
    send_telegram(msg)
    log.info(f"✅ {label}: ENTRY alert sent")


def check_adjustments(cfg: dict, spot: float, state: dict):
    """Check if spot is approaching sell strikes → send adjustment alert."""
    label = cfg["label"]

    # Check CALL side breach
    if not state["call_adjusted"] and state["sell_call"]:
        call_threshold = state["sell_call"] - IC_ADJUST_BUFFER
        if spot >= call_threshold:
            log.info(f"⚠️ {label}: Spot {spot:.2f} near SELL CALL {state['sell_call']}")
            strikes = {
                "sell_call": state["sell_call"],
                "buy_call": state["buy_call"],
                "sell_put": state["sell_put"],
                "buy_put": state["buy_put"],
            }
            msg = format_adjustment_message(label, "CALL", strikes, spot, state["expiry_str"])
            send_telegram(msg)
            state["call_adjusted"] = True
            log.info(f"✅ {label}: CALL adjustment alert sent")

    # Check PUT side breach
    if not state["put_adjusted"] and state["sell_put"]:
        put_threshold = state["sell_put"] + IC_ADJUST_BUFFER
        if spot <= put_threshold:
            log.info(f"⚠️ {label}: Spot {spot:.2f} near SELL PUT {state['sell_put']}")
            strikes = {
                "sell_call": state["sell_call"],
                "buy_call": state["buy_call"],
                "sell_put": state["sell_put"],
                "buy_put": state["buy_put"],
            }
            msg = format_adjustment_message(label, "PUT", strikes, spot, state["expiry_str"])
            send_telegram(msg)
            state["put_adjusted"] = True
            log.info(f"✅ {label}: PUT adjustment alert sent")

    # If BOTH sides adjusted → send full exit
    if state["call_adjusted"] and state["put_adjusted"] and not state["exit_sent"]:
        log.info(f"🔴 {label}: Both sides breached — sending EXIT")
        msg = format_exit_message(label)
        send_telegram(msg)
        state["exit_sent"] = True


def send_exit(cfg: dict, state: dict):
    """Send EXIT alert at end of day."""
    label = cfg["label"]
    msg = format_exit_message(label)
    send_telegram(msg)
    state["exit_sent"] = True
    log.info(f"✅ {label}: EXIT alert sent (EOD)")


# ─────────────────────────────────────────────
# MARKET HOURS CHECK
# ─────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    t = now.time()
    return dtime(*MARKET_OPEN) <= t <= dtime(*MARKET_CLOSE)


# ─────────────────────────────────────────────
# MAIN SCAN LOOP
# ─────────────────────────────────────────────

def run_scan():
    now = datetime.now(IST)
    t = now.time()

    for cfg in SYMBOLS:
        label = cfg["label"]
        state = get_state(label)

        # Skip if already fully exited today
        if state["exit_sent"]:
            continue

        # Fetch current spot
        spot = fetch_spot(cfg["yf_symbol"])
        if spot is None:
            log.warning(f"{label}: Could not fetch spot price")
            continue

        log.info(f"{label}: Spot = {spot:,.2f}")

        # ── ENTRY: at or after entry time, once per day ──
        if t >= dtime(*IC_ENTRY_TIME) and not state["entry_sent"]:
            enter_iron_condor(cfg, spot, state)
            continue  # don't check adjustments on entry candle

        # ── MONITOR: check adjustments while position is open ──
        if state["entry_sent"] and not state["exit_sent"]:
            check_adjustments(cfg, spot, state)

        # ── EXIT: at exit time ──
        if t >= dtime(*IC_EXIT_TIME) and state["entry_sent"] and not state["exit_sent"]:
            send_exit(cfg, state)


def main():
    log.info("Bot starting up...")
    send_startup_message()

    first_scan_done = False

    while True:
        try:
            now = datetime.now(IST)

            if is_market_open():
                # First scan diagnostic
                if not first_scan_done:
                    first_scan_done = True
                    diag_msg = (
                        "🔍 <b>First Scan — Bot Diagnostic</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏱ Time: <code>{now.strftime('%d %b %Y  %H:%M IST')}</code>\n"
                    )
                    for cfg in SYMBOLS:
                        spot = fetch_spot(cfg["yf_symbol"])
                        if spot is None:
                            diag_msg += f"❌ <code>{cfg['label']}</code>: No data from yfinance\n"
                        else:
                            diag_msg += f"✅ <code>{cfg['label']}</code>: Spot = <code>₹{spot:,.2f}</code>\n"
                    diag_msg += (
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "<i>If you see ❌, yfinance is blocked on Railway.</i>"
                    )
                    send_telegram(diag_msg)

                run_scan()

            else:
                # Reset for next day
                if now.hour >= 16:
                    first_scan_done = False
                log.info(f"Market closed ({now.strftime('%H:%M IST')}). Sleeping...")

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            try:
                err_msg = f"🚨 <b>Bot Error</b>\n<code>{str(e)[:200]}</code>"
                send_telegram(err_msg)
            except:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
