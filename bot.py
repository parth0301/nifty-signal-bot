"""
NIFTY / BANKNIFTY Options Signal Bot
Strategy: 9 EMA / 20 EMA crossover + 50 EMA trend filter
SL/Target: Asymmetric % based (target closer than SL for high win rate)
Timeframe: 15 minutes
Data: yfinance (free, no API key)
Alerts: Telegram
"""

import os
import time
import logging
from datetime import datetime, time as dtime
import pytz

import pandas as pd
import numpy as np
import yfinance as yf
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SYMBOLS,
    EMA_FAST,
    EMA_SLOW,
    EMA_TREND,
    TREND_BARS,
    SL_PCT,
    T1_PCT,
    T2_PCT,
    MIN_RR_RATIO,
    NO_TRADE_BEFORE,
    NO_TRADE_AFTER,
    COOLDOWN_BARS,
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

# Track last signal per symbol to avoid duplicate alerts
last_signal: dict[str, str] = {}

# Track cooldown per symbol
cooldown_remaining: dict[str, int] = {}


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_ohlcv(symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    """Fetch OHLCV data from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            log.warning(f"No data for {symbol}")
            return pd.DataFrame()
        df.index = df.index.tz_convert(IST)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.dropna(inplace=True)
        return df
    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()


def get_nse_option_chain(symbol: str) -> dict:
    """
    Fetch live option chain from NSE India (free, no API key).
    Returns ATM strike and CE/PE last traded prices.
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
    sym = "NIFTY" if "NIFTY" in symbol and "BANK" not in symbol else "BANKNIFTY"
    url = url_map.get(sym)
    if not url:
        return {}

    try:
        session = requests.Session()
        # Seed cookies
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        resp = session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        underlying = data["records"]["underlyingValue"]
        expiry = data["records"]["expiryDates"][0]  # nearest expiry

        # Find ATM strike
        strikes = [r["strikePrice"] for r in data["records"]["data"] if r.get("expiryDate") == expiry]
        atm = min(strikes, key=lambda s: abs(s - underlying))

        # Get ATM CE and PE prices
        ce_ltp, pe_ltp = None, None
        for rec in data["records"]["data"]:
            if rec.get("expiryDate") == expiry and rec["strikePrice"] == atm:
                if "CE" in rec:
                    ce_ltp = rec["CE"].get("lastPrice")
                if "PE" in rec:
                    pe_ltp = rec["PE"].get("lastPrice")

        return {
            "underlying": underlying,
            "expiry": expiry,
            "atm_strike": atm,
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
        }
    except Exception as e:
        log.warning(f"NSE option chain fetch failed: {e}")
        return {}


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = compute_ema(df["Close"], EMA_FAST)
    df["ema_slow"] = compute_ema(df["Close"], EMA_SLOW)
    df["ema_trend"] = compute_ema(df["Close"], EMA_TREND)

    # Crossover detection
    df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    return df


# ─────────────────────────────────────────────
# SIGNAL LOGIC
# ─────────────────────────────────────────────

def evaluate_signal(df: pd.DataFrame, label: str = "") -> dict | None:
    """
    Returns signal dict if EMA crossover + 50 EMA trend agrees.
    Only 3 filters: time, trend, cooldown.
    """
    if len(df) < EMA_TREND + 10:
        return None

    latest = df.iloc[-1]
    close = latest["Close"]
    ema_fast = latest["ema_fast"]
    ema_slow = latest["ema_slow"]
    candle_time = df.index[-1]

    # ── Crossover detection ──
    if latest["cross_up"]:
        direction = "BUY"
        option_type = "CE"
    elif latest["cross_down"]:
        direction = "SELL"
        option_type = "PE"
    else:
        return None

    # ── FILTER 1: Time-of-day ──
    t = candle_time.time() if hasattr(candle_time, "time") else candle_time
    if t < dtime(*NO_TRADE_BEFORE) or t > dtime(*NO_TRADE_AFTER):
        log.info(f"{label}: {direction} skipped — outside trading window")
        return None

    # ── FILTER 2: Cooldown ──
    cd = cooldown_remaining.get(label, 0)
    if cd > 0:
        cooldown_remaining[label] = cd - 1
        log.info(f"{label}: {direction} skipped — cooldown ({cd} bars left)")
        return None

    # ── FILTER 3: 50 EMA trend ──
    ema_trend_vals = df["ema_trend"].iloc[-(TREND_BARS + 1):]
    if len(ema_trend_vals) >= 2:
        slope = ema_trend_vals.iloc[-1] - ema_trend_vals.iloc[0]
        if direction == "BUY" and slope <= 0:
            log.info(f"{label}: BUY skipped — 50 EMA trending down")
            return None
        if direction == "SELL" and slope >= 0:
            log.info(f"{label}: SELL skipped — 50 EMA trending up")
            return None

    # ── Build signal (asymmetric SL/targets) ──
    if direction == "BUY":
        sl = close * (1 - SL_PCT)
        target1 = close * (1 + T1_PCT)
        target2 = close * (1 + T2_PCT)
    else:
        sl = close * (1 + SL_PCT)
        target1 = close * (1 - T1_PCT)
        target2 = close * (1 - T2_PCT)

    risk = abs(close - sl)
    reward1 = abs(close - target1)
    rr1 = reward1 / risk if risk > 0 else 0

    if rr1 < MIN_RR_RATIO:
        log.info(f"{label}: {direction} skipped — RR {rr1:.2f} below {MIN_RR_RATIO}")
        return None

    # Signal strength
    ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100
    if ema_gap_pct > 0.3:
        strength = "STRONG 🔥"
    elif ema_gap_pct > 0.15:
        strength = "MODERATE ✅"
    else:
        strength = "WEAK ⚠️"

    return {
        "signal": direction,
        "direction": "BULLISH" if direction == "BUY" else "BEARISH",
        "option_type": option_type,
        "close": close,
        "sl": sl,
        "target1": target1,
        "target2": target2,
        "rr1": rr1,
        "rr2": abs(close - target2) / risk if risk > 0 else 0,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_gap_pct": ema_gap_pct,
        "strength": strength,
        "candle_time": candle_time,
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


def format_signal_message(sym_label: str, sig: dict, options: dict) -> str:
    arrow = "🟢" if sig["signal"] == "BUY" else "🔴"
    opt_sym = f"{sym_label} ATM {sig['option_type']}"

    opt_info = ""
    if options:
        atm = options.get("atm_strike", "N/A")
        ce_ltp = options.get("ce_ltp", "N/A")
        pe_ltp = options.get("pe_ltp", "N/A")
        ltp = ce_ltp if sig["option_type"] == "CE" else pe_ltp
        opt_info = (
            f"\n\n<b>📋 Options Snapshot</b>"
            f"\nATM Strike: <code>{atm}</code>"
            f"\n{sig['option_type']} LTP: <code>₹{ltp}</code>"
            f"\nExpiry: <code>{options.get('expiry', 'N/A')}</code>"
        )

    ts = sig["candle_time"].strftime("%d %b %Y  %H:%M IST")

    msg = (
        f"{arrow} <b>{sig['signal']} SIGNAL — {sym_label}</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Candle: <code>{ts}</code>\n"
        f"📊 Timeframe: 15 Min\n"
        f"📈 Direction: <b>{sig['direction']}</b>\n"
        f"💡 Signal Strength: {sig['strength']}\n"
        f"\n<b>🎯 Entry Zone</b>\n"
        f"Spot Price: <code>₹{sig['close']:,.2f}</code>\n"
        f"9 EMA: <code>₹{sig['ema_fast']:,.2f}</code>\n"
        f"20 EMA: <code>₹{sig['ema_slow']:,.2f}</code>\n"
        f"EMA Gap: <code>{sig['ema_gap_pct']:.2f}%</code>\n"
        f"\n<b>🛡 Risk Management</b>\n"
        f"Stop Loss: <code>₹{sig['sl']:,.2f}</code> ({SL_PCT*100:.1f}%)\n"
        f"Target 1: <code>₹{sig['target1']:,.2f}</code> ({T1_PCT*100:.2f}%)\n"
        f"Target 2: <code>₹{sig['target2']:,.2f}</code> ({T2_PCT*100:.1f}%)\n"
        f"\n<b>🔖 Option to Buy</b>\n"
        f"{opt_sym}"
        f"{opt_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Signal only — YOU decide the entry. Always manage risk.</i>"
    )
    return msg


def send_startup_message():
    msg = (
        "🤖 <b>NIFTY Signal Bot is LIVE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy: <code>{EMA_FAST}/{EMA_SLOW} EMA + 50 EMA Trend</code>\n"
        f"SL: <code>{SL_PCT*100:.1f}%</code> | T1: <code>{T1_PCT*100:.2f}%</code> | T2: <code>{T2_PCT*100:.1f}%</code>\n"
        "Timeframe: <code>15 Minutes</code>\n"
        "Instruments: <code>NIFTY | BANKNIFTY</code>\n"
        "Market Hours: <code>09:15 – 15:25 IST</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Scanning every 5 minutes. 🎯"
    )
    send_telegram(msg)


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
    log.info("Starting scan cycle...")
    for cfg in SYMBOLS:
        sym = cfg["yf_symbol"]
        label = cfg["label"]
        nse_sym = cfg["nse_symbol"]

        log.info(f"Scanning {label} ({sym})")
        df = fetch_ohlcv(sym)
        if df.empty:
            continue

        df = add_indicators(df)
        sig = evaluate_signal(df, label)

        if sig is None:
            log.info(f"{label}: No signal")
            continue

        # Deduplicate
        sig_key = f"{label}_{sig['signal']}_{sig['candle_time']}"
        if last_signal.get(label) == sig_key:
            log.info(f"{label}: Signal already sent, skipping")
            continue

        last_signal[label] = sig_key

        # Fetch option chain
        options = get_nse_option_chain(nse_sym)

        msg = format_signal_message(label, sig, options)
        send_telegram(msg)
        log.info(f"Signal sent: {label} {sig['signal']}")


def main():
    log.info("Bot starting up...")
    send_startup_message()

    while True:
        try:
            if is_market_open():
                run_scan()
            else:
                now = datetime.now(IST)
                log.info(f"Market closed ({now.strftime('%H:%M IST')}). Sleeping...")

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
