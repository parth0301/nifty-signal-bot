"""
Backtest — EMA Crossover Strategy (No ATR)
Usage:
    python backtest.py
    python backtest.py --symbol NIFTY
    python backtest.py --period 60d
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# ── config ────────────────────────
from config import EMA_FAST, EMA_SLOW, MIN_RR_RATIO


def fetch(yf_symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    df = yf.Ticker(yf_symbol).history(period=period, interval=interval)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (
        df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
    )
    df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (
        df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
    )

    return df


def backtest(df: pd.DataFrame, label: str) -> pd.DataFrame:
    trades = []
    i = EMA_SLOW + 5

    while i < len(df):
        row = df.iloc[i]
        close = row["Close"]

        if row["cross_up"]:
            direction = "BUY"
            sl = close * 0.997      # 0.3% SL
            target1 = close * 1.006 # 0.6%
            target2 = close * 1.012 # 1.2%

        elif row["cross_down"]:
            direction = "SELL"
            sl = close * 1.003
            target1 = close * 0.994
            target2 = close * 0.988

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

        # Trade simulation
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
            exit_price = df.iloc[min(i + 40, len(df) - 1)]["Close"]
            exit_time = df.index[min(i + 40, len(df) - 1)]
            pnl_pts = (
                exit_price - entry_price
                if direction == "BUY"
                else entry_price - exit_price
            )

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
            "pnl_pts": round(pnl_pts, 2) if pnl_pts else 0,
        })

        i = df.index.get_loc(exit_time) + 1 if exit_time in df.index else i + 1

    return pd.DataFrame(trades)


def print_report(trades_df: pd.DataFrame, label: str):
    if trades_df.empty:
        print(f"\n{label}: No trades found.")
        return

    total = len(trades_df)
    wins = trades_df[trades_df["pnl_pts"] > 0]
    losses = trades_df[trades_df["pnl_pts"] <= 0]

    win_rate = len(wins) / total * 100
    total_pnl = trades_df["pnl_pts"].sum()

    avg_win = wins["pnl_pts"].mean() if not wins.empty else 0
    avg_loss = losses["pnl_pts"].mean() if not losses.empty else 0

    pf = (wins["pnl_pts"].sum()) / abs(losses["pnl_pts"].sum()) if not losses.empty else float("inf")

    cumulative = trades_df["pnl_pts"].cumsum()
    max_dd = (cumulative - cumulative.cummax()).min()

    print("\n" + "=" * 50)
    print(f"BACKTEST REPORT — {label}")
    print(f"Strategy: {EMA_FAST}/{EMA_SLOW} EMA Crossover")
    print("=" * 50)

    print(f"Total Trades   : {total}")
    print(f"Win Rate       : {win_rate:.2f}%")
    print(f"Profit Factor  : {pf:.2f}")
    print(f"Total PnL      : {total_pnl:.2f}")
    print(f"Max Drawdown   : {max_dd:.2f}")

    print("\nLast 10 Trades:")
    print(trades_df.tail(10).to_string(index=False))

    out_file = f"backtest_{label}.csv"
    trades_df.to_csv(out_file, index=False)
    print(f"\nSaved → {out_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=["NIFTY", "BANKNIFTY", "BOTH"], default="BOTH")
    parser.add_argument("--period", default="60d")
    args = parser.parse_args()

    symbols = {
        "NIFTY": "^NSEI",
        "BANKNIFTY": "^NSEBANK",
    }

    targets = list(symbols.items()) if args.symbol == "BOTH" else [(args.symbol, symbols[args.symbol])]

    for label, yf_sym in targets:
        print(f"\nFetching {label}...")
        df = fetch(yf_sym, period=args.period)

        if df.empty:
            print(f"No data for {label}")
            continue

        df = add_indicators(df)
        trades = backtest(df, label)
        print_report(trades, label)


if __name__ == "__main__":
    main()