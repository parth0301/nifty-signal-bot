"""
NIFTY / BANKNIFTY Options Signal Bot
Strategy: 9 EMA / 20 EMA crossover + ATR(7, multiplier=2) for SL
Timeframe: 15 minutes
Data: yfinance (free, no API key)
Alerts: Telegram
"""

import os
import time
import logging
import asyncio
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
    ATR_PERIOD,
    ATR_MULTIPLIER,
    EMA_FAST,
    EMA_SLOW,
    SCAN_INTERVAL_SECONDS,
    MIN_RR_RATIO,
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


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = compute_ema(df["Close"], EMA_FAST)
    df["ema_slow"] = compute_ema(df["Close"], EMA_SLOW)
    df["atr"] = compute_atr(df, ATR_PERIOD)

    # ATR-based dynamic SL bands
    df["atr_upper"] = df["Close"] + ATR_MULTIPLIER * df["atr"]
    df["atr_lower"] = df["Close"] - ATR_MULTIPLIER * df["atr"]

    # Crossover detection
    df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    return df


# ─────────────────────────────────────────────
# SIGNAL LOGIC
# ─────────────────────────────────────────────

def evaluate_signal(df: pd.DataFrame) -> dict | None:
    """
    Returns signal dict if a fresh crossover is detected on the latest candle.
    """
    if len(df) < EMA_SLOW + 5:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = latest["Close"]
    atr = latest["atr"]
    ema_fast = latest["ema_fast"]
    ema_slow = latest["ema_slow"]

    # Trend strength: % gap between EMAs
    ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100

    signal = None
    direction = None

    if latest["cross_up"]:
        signal = "BUY"
        direction = "BULLISH"
        sl = close - (ATR_MULTIPLIER * atr)
        target1 = close + (ATR_MULTIPLIER * atr * 1.5)
        target2 = close + (ATR_MULTIPLIER * atr * 2.5)
        option_type = "CE"
    elif latest["cross_down"]:
        signal = "SELL"
        direction = "BEARISH"
        sl = close + (ATR_MULTIPLIER * atr)
        target1 = close - (ATR_MULTIPLIER * atr * 1.5)
        target2 = close - (ATR_MULTIPLIER * atr * 2.5)
        option_type = "PE"
    else:
        return None

    risk = abs(close - sl)
    reward1 = abs(close - target1)
    rr1 = reward1 / risk if risk > 0 else 0

    if rr1 < MIN_RR_RATIO:
        log.info(f"Signal {signal} skipped — RR {rr1:.2f} below minimum {MIN_RR_RATIO}")
        return None

    # Signal strength
    if ema_gap_pct > 0.3 and atr > df["atr"].rolling(20).mean().iloc[-1]:
        strength = "STRONG 🔥"
    elif ema_gap_pct > 0.15:
        strength = "MODERATE ✅"
    else:
        strength = "WEAK ⚠️"

    return {
        "signal": signal,
        "direction": direction,
        "option_type": option_type,
        "close": close,
        "sl": sl,
        "target1": target1,
        "target2": target2,
        "rr1": rr1,
        "rr2": abs(close - target2) / risk if risk > 0 else 0,
        "atr": atr,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_gap_pct": ema_gap_pct,
        "strength": strength,
        "candle_time": df.index[-1],
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
        f"\n<b>🛡 Risk Management (ATR-based)</b>\n"
        f"ATR(7): <code>{sig['atr']:,.2f}</code>\n"
        f"Stop Loss: <code>₹{sig['sl']:,.2f}</code>\n"
        f"Target 1: <code>₹{sig['target1']:,.2f}</code>  (RR 1:{sig['rr1']:.1f})\n"
        f"Target 2: <code>₹{sig['target2']:,.2f}</code>  (RR 1:{sig['rr2']:.1f})\n"
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
        "Strategy: <code>9 EMA / 20 EMA + ATR(7, x2)</code>\n"
        "Timeframe: <code>15 Minutes</code>\n"
        "Instruments: <code>NIFTY | BANKNIFTY</code>\n"
        "Market Hours: <code>09:15 – 15:25 IST</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Scanning every 5 minutes during market hours.\n"
        "Signals sent here automatically. You decide entry. 🎯"
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
        sig = evaluate_signal(df)

        if sig is None:
            log.info(f"{label}: No signal")
            continue

        # Deduplicate — don't re-send the same signal within same 15m candle
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
