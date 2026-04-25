"""
Backtest — 9 EMA / 20 EMA + ATR(7, x2) strategy
Usage:
    python backtest.py               → backtests both NIFTY and BANKNIFTY
    python backtest.py --symbol NIFTY
    python backtest.py --period 60d  → use 60 days of data
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# ── reuse indicator logic from bot ────────────────────────
from config import EMA_FAST, EMA_SLOW, ATR_PERIOD, ATR_MULTIPLIER, MIN_RR_RATIO


def fetch(yf_symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    df = yf.Ticker(yf_symbol).history(period=period, interval=interval)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"]  = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema20"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"]  - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    df["cross_up"]   = (df["ema9"] > df["ema20"]) & (df["ema9"].shift(1) <= df["ema20"].shift(1))
    df["cross_down"] = (df["ema9"] < df["ema20"]) & (df["ema9"].shift(1) >= df["ema20"].shift(1))
    return df


def backtest(df: pd.DataFrame, label: str) -> pd.DataFrame:
    trades = []
    i = EMA_SLOW + 5

    while i < len(df):
        row = df.iloc[i]
        close = row["Close"]
        atr   = row["atr"]

        if row["cross_up"]:
            direction  = "BUY"
            sl         = close - ATR_MULTIPLIER * atr
            target1    = close + ATR_MULTIPLIER * atr * 1.5
            target2    = close + ATR_MULTIPLIER * atr * 2.5
        elif row["cross_down"]:
            direction  = "SELL"
            sl         = close + ATR_MULTIPLIER * atr
            target1    = close - ATR_MULTIPLIER * atr * 1.5
            target2    = close - ATR_MULTIPLIER * atr * 2.5
        else:
            i += 1
            continue

        risk = abs(close - sl)
        if risk == 0:
            i += 1
            continue
        rr1 = abs(close - target1) / risk
        if rr1 < MIN_RR_RATIO:
            i += 1
            continue

        # Simulate outcome: walk forward candles until SL or Target hit
        entry_price = close
        entry_time  = df.index[i]
        outcome     = "OPEN"
        exit_price  = None
        exit_time   = None
        pnl_pts     = None

        for j in range(i + 1, min(i + 40, len(df))):  # max 40 candles forward
            future = df.iloc[j]
            if direction == "BUY":
                if future["Low"] <= sl:
                    outcome    = "SL_HIT"
                    exit_price = sl
                    exit_time  = df.index[j]
                    pnl_pts    = sl - entry_price
                    break
                elif future["High"] >= target2:
                    outcome    = "T2_HIT"
                    exit_price = target2
                    exit_time  = df.index[j]
                    pnl_pts    = target2 - entry_price
                    break
                elif future["High"] >= target1:
                    outcome    = "T1_HIT"
                    exit_price = target1
                    exit_time  = df.index[j]
                    pnl_pts    = target1 - entry_price
                    break
            else:  # SELL
                if future["High"] >= sl:
                    outcome    = "SL_HIT"
                    exit_price = sl
                    exit_time  = df.index[j]
                    pnl_pts    = entry_price - sl
                    break
                elif future["Low"] <= target2:
                    outcome    = "T2_HIT"
                    exit_price = target2
                    exit_time  = df.index[j]
                    pnl_pts    = entry_price - target2
                    break
                elif future["Low"] <= target1:
                    outcome    = "T1_HIT"
                    exit_price = target1
                    exit_time  = df.index[j]
                    pnl_pts    = entry_price - target1
                    break

        if outcome == "OPEN":
            exit_price = df.iloc[min(i + 40, len(df) - 1)]["Close"]
            exit_time  = df.index[min(i + 40, len(df) - 1)]
            pnl_pts    = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)

        trades.append({
            "symbol":      label,
            "direction":   direction,
            "entry_time":  entry_time,
            "entry_price": round(entry_price, 2),
            "sl":          round(sl, 2),
            "target1":     round(target1, 2),
            "target2":     round(target2, 2),
            "rr1":         round(rr1, 2),
            "atr":         round(atr, 2),
            "outcome":     outcome,
            "exit_price":  round(exit_price, 2) if exit_price else None,
            "exit_time":   exit_time,
            "pnl_pts":     round(pnl_pts, 2) if pnl_pts else 0,
        })

        # Skip ahead past this trade
        i = df.index.get_loc(exit_time) + 1 if exit_time in df.index else i + 1

    return pd.DataFrame(trades)


def print_report(trades_df: pd.DataFrame, label: str):
    if trades_df.empty:
        print(f"\n{label}: No trades found.")
        return

    total     = len(trades_df)
    wins      = trades_df[trades_df["pnl_pts"] > 0]
    losses    = trades_df[trades_df["pnl_pts"] <= 0]
    win_rate  = len(wins) / total * 100
    total_pnl = trades_df["pnl_pts"].sum()
    avg_win   = wins["pnl_pts"].mean() if not wins.empty else 0
    avg_loss  = losses["pnl_pts"].mean() if not losses.empty else 0
    t1_hits   = len(trades_df[trades_df["outcome"] == "T1_HIT"])
    t2_hits   = len(trades_df[trades_df["outcome"] == "T2_HIT"])
    sl_hits   = len(trades_df[trades_df["outcome"] == "SL_HIT"])
    best      = trades_df["pnl_pts"].max()
    worst     = trades_df["pnl_pts"].min()

    # Profit factor
    gross_profit = wins["pnl_pts"].sum() if not wins.empty else 0
    gross_loss   = abs(losses["pnl_pts"].sum()) if not losses.empty else 1
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown (cumulative PnL)
    cumulative = trades_df["pnl_pts"].cumsum()
    roll_max   = cumulative.cummax()
    drawdown   = (cumulative - roll_max)
    max_dd     = drawdown.min()

    sep = "=" * 52
    print(f"\n{sep}")
    print(f"  BACKTEST REPORT — {label}")
    print(f"  Strategy: {EMA_FAST}/{EMA_SLOW} EMA + ATR({ATR_PERIOD}, x{ATR_MULTIPLIER})")
    print(f"  Timeframe: 15 Minutes")
    print(sep)
    print(f"  Total Trades      : {total}")
    print(f"  Win Rate          : {win_rate:.1f}%")
    print(f"  Profit Factor     : {pf:.2f}")
    print(f"  Total PnL (pts)   : {total_pnl:+.2f}")
    print(f"  Avg Win (pts)     : {avg_win:+.2f}")
    print(f"  Avg Loss (pts)    : {avg_loss:+.2f}")
    print(f"  Best Trade (pts)  : {best:+.2f}")
    print(f"  Worst Trade (pts) : {worst:+.2f}")
    print(f"  Max Drawdown(pts) : {max_dd:+.2f}")
    print(f"  T1 Hits           : {t1_hits}  |  T2 Hits: {t2_hits}  |  SL Hits: {sl_hits}")
    print(sep)

    # Last 10 trades
    print(f"\n  Last {min(10, total)} Trades:")
    display_cols = ["entry_time", "direction", "entry_price", "sl", "target1", "outcome", "pnl_pts"]
    print(trades_df[display_cols].tail(10).to_string(index=False))
    print()

    # Save full results
    out_file = f"backtest_{label.replace(' ', '_')}.csv"
    trades_df.to_csv(out_file, index=False)
    print(f"  Full results saved → {out_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=["NIFTY", "BANKNIFTY", "BOTH"], default="BOTH")
    parser.add_argument("--period", default="60d", help="e.g. 30d, 60d (max 60d for 15m data)")
    args = parser.parse_args()

    symbols = {
        "NIFTY":     "^NSEI",
        "BANKNIFTY": "^NSEBANK",
    }

    targets = list(symbols.items()) if args.symbol == "BOTH" else [(args.symbol, symbols[args.symbol])]

    for label, yf_sym in targets:
        print(f"\nFetching {label} ({args.period})...")
        df = fetch(yf_sym, period=args.period)
        if df.empty:
            print(f"  No data for {label}. Skipping.")
            continue
        df = add_indicators(df)
        trades = backtest(df, label)
        print_report(trades, label)


if __name__ == "__main__":
    main()
