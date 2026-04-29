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
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Track last signal per symbol to avoid duplicate alerts
# Key: symbol label, Value: candle_time of last signal sent
last_signal_time: dict[str, str] = {}

# Track cooldown per symbol
cooldown_remaining: dict[str, int] = {}


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_ohlcv(symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    """
    Fetch OHLCV data from yfinance.
    IMPORTANT: The last row from yfinance is the CURRENTLY FORMING candle.
    We drop it to only work with COMPLETED candles.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            log.warning(f"No data for {symbol}")
            return pd.DataFrame()
        df.index = df.index.tz_convert(IST)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.dropna(inplace=True)

        # Drop the last row — it's the LIVE (incomplete) candle.
        # EMA values on an incomplete candle are unreliable and cause
        # crossovers to appear/disappear mid-candle.
        if len(df) > 1:
            live_candle_time = df.index[-1]
            df = df.iloc[:-1]
            log.debug(f"{symbol}: Dropped live candle at {live_candle_time}, "
                      f"last completed candle: {df.index[-1]}")

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
    Scans the last 3 COMPLETED candles for an EMA crossover.
    Returns signal dict if crossover + 50 EMA trend agrees.
    Checks multiple candles to avoid missing signals between scans.
    """
    if len(df) < EMA_TREND + 10:
        log.debug(f"{label}: Not enough data ({len(df)} candles, need {EMA_TREND + 10})")
        return None

    # Log the current EMA state for debugging
    latest = df.iloc[-1]
    log.debug(
        f"{label} EMA STATE | Close: {latest['Close']:.2f} | "
        f"EMA{EMA_FAST}: {latest['ema_fast']:.2f} | "
        f"EMA{EMA_SLOW}: {latest['ema_slow']:.2f} | "
        f"EMA{EMA_TREND}: {latest['ema_trend']:.2f} | "
        f"Fast>Slow: {latest['ema_fast'] > latest['ema_slow']} | "
        f"CrossUp: {latest['cross_up']} | CrossDown: {latest['cross_down']}"
    )

    # Check the last 3 completed candles for crossovers (most recent first)
    lookback = min(3, len(df))
    for offset in range(lookback):
        idx = len(df) - 1 - offset
        row = df.iloc[idx]
        candle_time = df.index[idx]

        if row["cross_up"]:
            direction = "BUY"
            option_type = "CE"
        elif row["cross_down"]:
            direction = "SELL"
            option_type = "PE"
        else:
            continue

        log.info(f"{label}: Crossover {direction} detected on candle {candle_time} (offset={offset})")

        close = row["Close"]
        ema_fast = row["ema_fast"]
        ema_slow = row["ema_slow"]

        # ── FILTER 1: Time-of-day ──
        t = candle_time.time() if hasattr(candle_time, "time") else candle_time
        if t < dtime(*NO_TRADE_BEFORE) or t > dtime(*NO_TRADE_AFTER):
            log.info(f"{label}: {direction} skipped — outside trading window ({t})")
            continue

        # ── FILTER 2: Cooldown ──
        cd = cooldown_remaining.get(label, 0)
        if cd > 0:
            cooldown_remaining[label] = cd - 1
            log.info(f"{label}: {direction} skipped — cooldown ({cd} bars left)")
            continue

        # ── FILTER 3: 50 EMA trend ──
        ema_start_idx = max(0, idx - TREND_BARS)
        ema_trend_vals = df["ema_trend"].iloc[ema_start_idx: idx + 1]
        trend_slope = 0
        if len(ema_trend_vals) >= 2:
            trend_slope = ema_trend_vals.iloc[-1] - ema_trend_vals.iloc[0]
            if direction == "BUY" and trend_slope <= 0:
                log.info(f"{label}: BUY skipped — 50 EMA trending down (slope={trend_slope:.2f})")
                continue
            if direction == "SELL" and trend_slope >= 0:
                log.info(f"{label}: SELL skipped — 50 EMA trending up (slope={trend_slope:.2f})")
                continue

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
            continue

        # Signal strength
        ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100
        if ema_gap_pct > 0.3:
            strength = "STRONG 🔥"
        elif ema_gap_pct > 0.15:
            strength = "MODERATE ✅"
        else:
            strength = "WEAK ⚠️"

        log.info(
            f"{label}: ✅ SIGNAL PASSED ALL FILTERS | {direction} | "
            f"Close={close:.2f} | Trend slope={trend_slope:.2f} | RR={rr1:.2f}"
        )

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

    return None


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
    ts = sig["candle_time"].strftime("%d %b %Y  %H:%M IST")

    # Build the headline action — e.g. "BUY NIFTY 24500 CE"
    atm_strike = ""
    opt_ltp = ""
    expiry = ""
    if options:
        atm_strike = options.get("atm_strike", "")
        ce_ltp = options.get("ce_ltp", "N/A")
        pe_ltp = options.get("pe_ltp", "N/A")
        opt_ltp = ce_ltp if sig["option_type"] == "CE" else pe_ltp
        expiry = options.get("expiry", "N/A")

    # Clean instrument name (NIFTY 50 → NIFTY)
    instrument = sym_label.replace(" 50", "")

    # Headline: "BUY NIFTY 24500 CE" or "SELL BANKNIFTY 52000 PE"
    if atm_strike:
        action_line = f"{sig['signal']} {instrument} {atm_strike} {sig['option_type']}"
    else:
        action_line = f"{sig['signal']} {instrument} {sig['option_type']}"

    msg = (
        f"{arrow}{arrow}{arrow}\n"
        f"<b>{action_line}</b>\n"
        f"{arrow}{arrow}{arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ <b>Time:</b> <code>{ts}</code>\n"
        f"📊 <b>Instrument:</b> <code>{instrument}</code>\n"
        f"📈 <b>Direction:</b> <code>{sig['direction']}</code>\n"
        f"💡 <b>Strength:</b> {sig['strength']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    # Option details
    if atm_strike:
        msg += (
            f"\n<b>📋 What to Buy</b>\n"
            f"Strike: <code>{instrument} {atm_strike} {sig['option_type']}</code>\n"
            f"LTP: <code>₹{opt_ltp}</code>\n"
            f"Expiry: <code>{expiry}</code>\n"
        )

    # Entry + SL + Targets
    msg += (
        f"\n<b>🎯 Levels</b>\n"
        f"Entry (Spot): <code>₹{sig['close']:,.2f}</code>\n"
        f"Stop Loss:    <code>₹{sig['sl']:,.2f}</code>\n"
        f"Target 1:     <code>₹{sig['target1']:,.2f}</code>\n"
        f"Target 2:     <code>₹{sig['target2']:,.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Signal only — manage your own risk.</i>"
    )
    return msg


def send_startup_message():
    now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    msg = (
        "🤖 <b>Signal Bot is LIVE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Started: <code>{now}</code>\n"
        f"Strategy: <code>{EMA_FAST}/{EMA_SLOW} EMA + 50 EMA Trend</code>\n"
        "Timeframe: <code>15 Min</code>\n"
        "Market Hours: <code>09:15 – 15:25 IST</code>\n"
        f"Scan Interval: <code>Every {SCAN_INTERVAL_SECONDS // 60} minutes</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Waiting for signals... 🎯"
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
            log.warning(f"{label}: No data returned from yfinance")
            continue

        log.info(f"{label}: Got {len(df)} completed candles, latest: {df.index[-1]}")
        df = add_indicators(df)
        sig = evaluate_signal(df, label)

        if sig is None:
            log.info(f"{label}: No signal this cycle")
            continue

        # Deduplicate: only send one alert per candle time per symbol
        candle_key = str(sig['candle_time'])
        if last_signal_time.get(label) == candle_key:
            log.debug(f"{label}: Signal for candle {candle_key} already sent, skipping")
            continue

        last_signal_time[label] = candle_key
        log.info(f"🚨 NEW SIGNAL: {label} {sig['signal']} on candle {candle_key}")

        # Fetch option chain
        options = get_nse_option_chain(nse_sym)

        msg = format_signal_message(label, sig, options)
        send_telegram(msg)
        log.info(f"✅ Telegram sent: {label} {sig['signal']}")


def main():
    log.info("Bot starting up...")
    send_startup_message()

    scan_count = 0
    last_heartbeat_hour = -1
    first_market_scan_done = False

    while True:
        try:
            now = datetime.now(IST)

            if is_market_open():
                scan_count += 1
                log.info(f"--- Scan #{scan_count} at {now.strftime('%H:%M:%S IST')} ---")

                # First scan of the day: send diagnostic to Telegram
                if not first_market_scan_done:
                    first_market_scan_done = True
                    diag_msg = (
                        "🔍 <b>First Scan — Bot Diagnostic</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏱ Time: <code>{now.strftime('%d %b %Y  %H:%M IST')}</code>\n"
                    )
                    # Quick data check
                    for cfg in SYMBOLS:
                        test_df = fetch_ohlcv(cfg["yf_symbol"])
                        if test_df.empty:
                            diag_msg += f"❌ <code>{cfg['label']}</code>: No data from yfinance\n"
                        else:
                            latest_time = test_df.index[-1]
                            diag_msg += (
                                f"✅ <code>{cfg['label']}</code>: {len(test_df)} candles, "
                                f"latest: <code>{latest_time}</code>\n"
                            )
                    diag_msg += (
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "<i>If you see ❌ above, yfinance is blocked on Railway.</i>"
                    )
                    send_telegram(diag_msg)
                    log.info("Sent first-scan diagnostic to Telegram")

                run_scan()

                # Hourly heartbeat (once per hour during market hours)
                current_hour = now.hour
                if current_hour != last_heartbeat_hour:
                    last_heartbeat_hour = current_hour
                    hb_msg = (
                        f"💓 <b>Heartbeat</b> — <code>{now.strftime('%H:%M IST')}</code>\n"
                        f"Scans so far: <code>{scan_count}</code> | Status: <code>Running ✅</code>"
                    )
                    send_telegram(hb_msg)
                    log.info(f"Sent hourly heartbeat (hour={current_hour})")

            else:
                # Reset first scan flag at market close so it triggers next day
                if first_market_scan_done and now.hour >= 16:
                    first_market_scan_done = False
                log.info(f"Market closed ({now.strftime('%H:%M IST')}). Sleeping...")

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            # Send error to Telegram so user knows something broke
            try:
                err_msg = f"🚨 <b>Bot Error</b>\n<code>{str(e)[:200]}</code>"
                send_telegram(err_msg)
            except:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
