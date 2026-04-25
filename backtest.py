"""
Backtest — 9/20 EMA Crossover + 50 EMA Trend Filter
SL/Targets: Asymmetric % based (target closer than SL for high win rate)

Usage:
    python backtest.py
    python backtest.py --start 2025-01-01
    python backtest.py --symbol NIFTY --start 2025-06-01
    python backtest.py --period 60d
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, time as dtime
import time as _time
import requests

from config import (
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
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)


def fetch(yf_symbol: str, period: str = "60d", interval: str = "15m",
          start_date: str = None) -> pd.DataFrame:
    """
    Fetch OHLCV data. If start_date is given, downloads in 55-day chunks
    (yfinance limits 15m data to 60 days per request) and stitches together.
    """
    if start_date:
        return _fetch_chunked(yf_symbol, start_date, interval)
    else:
        df = yf.Ticker(yf_symbol).history(period=period, interval=interval)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        return df


def _fetch_chunked(yf_symbol: str, start_date: str, interval: str = "15m") -> pd.DataFrame:
    """Download 15m data in 55-day chunks from start_date to today."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.now()
    chunk_days = 55  # stay under yfinance's 60-day limit
    all_chunks = []

    current = start
    chunk_num = 0
    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        chunk_num += 1
        print(f"  Downloading chunk {chunk_num}: {current.strftime('%Y-%m-%d')} → {chunk_end.strftime('%Y-%m-%d')}")

        try:
            df = yf.Ticker(yf_symbol).history(
                start=current.strftime("%Y-%m-%d"),
                end=chunk_end.strftime("%Y-%m-%d"),
                interval=interval,
            )
            if not df.empty:
                all_chunks.append(df)
        except Exception as e:
            print(f"  Warning: chunk failed ({e}), skipping...")

        current = chunk_end
        if current < end:
            _time.sleep(1)  # rate-limit friendly

    if not all_chunks:
        return pd.DataFrame()

    combined = pd.concat(all_chunks)
    combined = combined[~combined.index.duplicated(keep="first")]  # remove overlaps
    combined = combined.sort_index()
    combined = combined[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"  Total: {len(combined)} candles from {combined.index[0]} to {combined.index[-1]}")
    return combined


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema_trend"] = df["Close"].ewm(span=EMA_TREND, adjust=False).mean()

    df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (
        df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
    )
    df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (
        df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
    )

    return df


def backtest(df: pd.DataFrame, label: str):
    trades = []
    i = EMA_TREND + 10
    cooldown = 0
    filter_stats = {
        "total_crossovers": 0,
        "skipped_time": 0,
        "skipped_cooldown": 0,
        "skipped_trend": 0,
        "skipped_rr": 0,
        "passed_all": 0,
    }

    while i < len(df):
        row = df.iloc[i]
        close = row["Close"]

        if row["cross_up"]:
            direction = "BUY"
        elif row["cross_down"]:
            direction = "SELL"
        else:
            i += 1
            continue

        filter_stats["total_crossovers"] += 1

        # ── FILTER 1: Time-of-day ──
        t = df.index[i].time() if hasattr(df.index[i], "time") else df.index[i]
        if t < dtime(*NO_TRADE_BEFORE) or t > dtime(*NO_TRADE_AFTER):
            filter_stats["skipped_time"] += 1
            i += 1
            continue

        # ── FILTER 2: Cooldown ──
        if cooldown > 0:
            cooldown -= 1
            filter_stats["skipped_cooldown"] += 1
            i += 1
            continue

        # ── FILTER 3: 50 EMA trend ──
        start_idx = max(0, i - TREND_BARS)
        ema_vals = df["ema_trend"].iloc[start_idx: i + 1]
        if len(ema_vals) >= 2:
            slope = ema_vals.iloc[-1] - ema_vals.iloc[0]
            if direction == "BUY" and slope <= 0:
                filter_stats["skipped_trend"] += 1
                i += 1
                continue
            if direction == "SELL" and slope >= 0:
                filter_stats["skipped_trend"] += 1
                i += 1
                continue

        # ── SL / TARGET (asymmetric %) ──
        if direction == "BUY":
            sl = close * (1 - SL_PCT)
            target1 = close * (1 + T1_PCT)
            target2 = close * (1 + T2_PCT)
        else:
            sl = close * (1 + SL_PCT)
            target1 = close * (1 - T1_PCT)
            target2 = close * (1 - T2_PCT)

        risk = abs(close - sl)
        if risk == 0:
            i += 1
            continue

        rr1 = abs(close - target1) / risk
        if rr1 < MIN_RR_RATIO:
            filter_stats["skipped_rr"] += 1
            i += 1
            continue

        filter_stats["passed_all"] += 1

        # ── Trade simulation ──
        entry_price = close
        entry_time = df.index[i]

        outcome = "OPEN"
        exit_price = None
        exit_time = None
        pnl_pts = None

        for j in range(i + 1, min(i + 40, len(df))):
            future = df.iloc[j]

            if direction == "BUY":
                if future["Low"] <= sl:
                    outcome = "SL_HIT"
                    exit_price = sl
                    exit_time = df.index[j]
                    pnl_pts = sl - entry_price
                    break
                elif future["High"] >= target2:
                    outcome = "T2_HIT"
                    exit_price = target2
                    exit_time = df.index[j]
                    pnl_pts = target2 - entry_price
                    break
                elif future["High"] >= target1:
                    outcome = "T1_HIT"
                    exit_price = target1
                    exit_time = df.index[j]
                    pnl_pts = target1 - entry_price
                    break
            else:  # SELL
                if future["High"] >= sl:
                    outcome = "SL_HIT"
                    exit_price = sl
                    exit_time = df.index[j]
                    pnl_pts = entry_price - sl
                    break
                elif future["Low"] <= target2:
                    outcome = "T2_HIT"
                    exit_price = target2
                    exit_time = df.index[j]
                    pnl_pts = entry_price - target2
                    break
                elif future["Low"] <= target1:
                    outcome = "T1_HIT"
                    exit_price = target1
                    exit_time = df.index[j]
                    pnl_pts = entry_price - target1
                    break

        if outcome == "OPEN":
            exit_idx = min(i + 40, len(df) - 1)
            exit_price = df.iloc[exit_idx]["Close"]
            exit_time = df.index[exit_idx]
            pnl_pts = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)

        # Cooldown after SL
        if outcome == "SL_HIT":
            cooldown = COOLDOWN_BARS

        trades.append({
            "symbol": label,
            "direction": direction,
            "entry_time": entry_time,
            "entry_price": round(entry_price, 2),
            "sl": round(sl, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "rr1": round(rr1, 2),
            "outcome": outcome,
            "exit_price": round(exit_price, 2) if exit_price else None,
            "exit_time": exit_time,
            "pnl_pts": round(pnl_pts, 2) if pnl_pts is not None else 0,
        })

        i = df.index.get_loc(exit_time) + 1 if exit_time is not None and exit_time in df.index else i + 1

    return pd.DataFrame(trades), filter_stats


def print_report(trades_df: pd.DataFrame, label: str, filter_stats: dict):
    print("\n" + "=" * 60)
    print(f"FILTER BREAKDOWN — {label}")
    print("=" * 60)
    print(f"Total Crossovers       : {filter_stats['total_crossovers']}")
    print(f"  Skipped (Time)       : {filter_stats['skipped_time']}")
    print(f"  Skipped (Cooldown)   : {filter_stats['skipped_cooldown']}")
    print(f"  Skipped (50EMA Trend): {filter_stats['skipped_trend']}")
    print(f"  Skipped (RR Ratio)   : {filter_stats['skipped_rr']}")
    print(f"  ✅ TRADED            : {filter_stats['passed_all']}")

    if trades_df.empty:
        print(f"\n{label}: No trades.")
        return

    total = len(trades_df)
    wins = trades_df[trades_df["pnl_pts"] > 0]
    losses = trades_df[trades_df["pnl_pts"] <= 0]

    win_rate = len(wins) / total * 100
    total_pnl = trades_df["pnl_pts"].sum()
    avg_win = wins["pnl_pts"].mean() if not wins.empty else 0
    avg_loss = losses["pnl_pts"].mean() if not losses.empty else 0
    pf = (wins["pnl_pts"].sum()) / abs(losses["pnl_pts"].sum()) if not losses.empty and losses["pnl_pts"].sum() != 0 else float("inf")

    cumulative = trades_df["pnl_pts"].cumsum()
    max_dd = (cumulative - cumulative.cummax()).min()

    t1_hits = len(trades_df[trades_df["outcome"] == "T1_HIT"])
    t2_hits = len(trades_df[trades_df["outcome"] == "T2_HIT"])
    sl_hits = len(trades_df[trades_df["outcome"] == "SL_HIT"])
    open_exits = len(trades_df[trades_df["outcome"] == "OPEN"])

    print("\n" + "=" * 60)
    print(f"BACKTEST REPORT — {label}")
    print(f"Strategy: {EMA_FAST}/{EMA_SLOW} EMA Cross + 50 EMA Trend")
    print(f"SL: {SL_PCT*100:.1f}% | T1: {T1_PCT*100:.2f}% | T2: {T2_PCT*100:.1f}%")
    print("=" * 60)

    print(f"Total Trades    : {total}")
    print(f"Wins            : {len(wins)}")
    print(f"Losses          : {len(losses)}")
    print(f"Win Rate        : {win_rate:.2f}%")
    print(f"Profit Factor   : {pf:.2f}")
    print(f"Total PnL       : {total_pnl:.2f} pts")
    print(f"Avg Win         : {avg_win:.2f} pts")
    print(f"Avg Loss        : {avg_loss:.2f} pts")
    print(f"Max Drawdown    : {max_dd:.2f} pts")

    print(f"\nOutcome Breakdown:")
    print(f"  T1 Hit        : {t1_hits}")
    print(f"  T2 Hit        : {t2_hits}")
    print(f"  SL Hit        : {sl_hits}")
    print(f"  Open/Time Exit: {open_exits}")

    if win_rate >= 75:
        print(f"\n🎯 TARGET ACHIEVED: Win Rate {win_rate:.1f}% >= 75%")
    elif win_rate >= 65:
        print(f"\n📊 CLOSE: Win Rate {win_rate:.1f}% — try reducing T1_PCT in config.py")
    else:
        print(f"\n⚠️  Win Rate {win_rate:.1f}% — below target")

    print("\nAll Trades:")
    print(trades_df.to_string(index=False))

    out_file = f"backtest_{label}.csv"
    trades_df.to_csv(out_file, index=False)
    print(f"\nSaved → {out_file}")


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
            print(f"Telegram error: {resp.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=["NIFTY", "BANKNIFTY", "BOTH"], default="BOTH")
    parser.add_argument("--period", default="60d",
                        help="Period for quick backtest (e.g. 60d). Ignored if --start is used.")
    parser.add_argument("--start", default=None,
                        help="Start date YYYY-MM-DD (e.g. 2025-01-01). Downloads 15m data in chunks.")
    args = parser.parse_args()

    # Send test notification for backtest
    msg = (
        "🧪 <b>BACKTEST RUNNING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: <code>Backtest Test Mode</code>\n"
        f"Symbol: <code>{args.symbol}</code>\n"
        f"Period: <code>{args.period if not args.start else args.start + ' to Today'}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>This is a test notification to verify Telegram alerts are working.</i>"
    )
    send_telegram(msg)
    print("\n[Telegram] Sent test notification!")

    symbols = {
        "NIFTY": "^NSEI",
        "BANKNIFTY": "^NSEBANK",
    }

    targets = list(symbols.items()) if args.symbol == "BOTH" else [(args.symbol, symbols[args.symbol])]

    for label, yf_sym in targets:
        print(f"\nFetching {label}...")
        df = fetch(yf_sym, period=args.period, start_date=args.start)

        if df.empty:
            print(f"No data for {label}")
            continue

        print(f"Data: {len(df)} candles from {df.index[0]} to {df.index[-1]}")
        df = add_indicators(df)
        trades, filter_stats = backtest(df, label)
        print_report(trades, label, filter_stats)


if __name__ == "__main__":
    main()