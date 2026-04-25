"""
Backtest — EMA Crossover Strategy with Full Filter Suite
Filters: 50 EMA Trend | RSI | Volume | Candle Body | ATR Expansion | Time-of-Day | Cooldown

Usage:
    python backtest.py
    python backtest.py --symbol NIFTY
    python backtest.py --period 60d
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time as dtime

# ── config ────────────────────────
from config import (
    EMA_FAST,
    EMA_SLOW,
    EMA_TREND,
    TREND_SLOPE_BARS,
    RSI_PERIOD,
    RSI_BUY_MIN,
    RSI_SELL_MAX,
    VOL_AVG_PERIOD,
    VOL_SPIKE_MULT,
    BODY_RATIO_MIN,
    ATR_PERIOD,
    ATR_MULTIPLIER,
    ATR_AVG_PERIOD,
    ATR_EXPAND_MULT,
    NO_TRADE_BEFORE,
    NO_TRADE_AFTER,
    COOLDOWN_BARS,
    MIN_RR_RATIO,
)


def fetch(yf_symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    df = yf.Ticker(yf_symbol).history(period=period, interval=interval)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


def compute_rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


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

    # ── Core EMAs ──
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema_trend"] = df["Close"].ewm(span=EMA_TREND, adjust=False).mean()

    # ── ATR ──
    df["atr"] = compute_atr(df, ATR_PERIOD)
    df["atr_avg"] = df["atr"].rolling(ATR_AVG_PERIOD).mean()

    # ── RSI ──
    df["rsi"] = compute_rsi(df["Close"], RSI_PERIOD)

    # ── Volume average ──
    df["vol_avg"] = df["Volume"].rolling(VOL_AVG_PERIOD).mean()

    # ── Candle body ratio ──
    candle_range = df["High"] - df["Low"]
    candle_range = candle_range.replace(0, np.nan)
    df["body_ratio"] = (df["Close"] - df["Open"]).abs() / candle_range
    df["body_ratio"] = df["body_ratio"].fillna(0)

    # ── Crossover detection ──
    df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (
        df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
    )
    df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (
        df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
    )

    return df


# ─────────────────────────────────────────────
# FILTER FUNCTIONS  (same logic as bot.py)
# ─────────────────────────────────────────────

def passes_time_filter(candle_time) -> bool:
    """No trades in first 15 min or after cutoff."""
    t = candle_time.time() if hasattr(candle_time, "time") else candle_time
    if t < dtime(*NO_TRADE_BEFORE):
        return False
    if t > dtime(*NO_TRADE_AFTER):
        return False
    return True


def passes_trend_filter(df: pd.DataFrame, idx: int, direction: str) -> bool:
    """50 EMA slope must agree with signal direction over last N bars."""
    start = max(0, idx - TREND_SLOPE_BARS)
    ema_vals = df["ema_trend"].iloc[start: idx + 1]
    if len(ema_vals) < 2:
        return False
    slope = ema_vals.iloc[-1] - ema_vals.iloc[0]
    if direction == "BUY" and slope <= 0:
        return False
    if direction == "SELL" and slope >= 0:
        return False
    return True


def passes_rsi_filter(rsi_value: float, direction: str) -> bool:
    if direction == "BUY" and rsi_value < RSI_BUY_MIN:
        return False
    if direction == "SELL" and rsi_value > RSI_SELL_MAX:
        return False
    return True


def passes_volume_filter(current_vol: float, avg_vol: float) -> bool:
    if pd.isna(avg_vol) or avg_vol == 0:
        return True
    return current_vol >= VOL_SPIKE_MULT * avg_vol


def passes_body_filter(body_ratio: float) -> bool:
    return body_ratio >= BODY_RATIO_MIN


def passes_atr_expansion(current_atr: float, avg_atr: float) -> bool:
    if pd.isna(avg_atr) or avg_atr == 0:
        return True
    return current_atr >= ATR_EXPAND_MULT * avg_atr


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────

def backtest(df: pd.DataFrame, label: str) -> pd.DataFrame:
    trades = []
    i = max(EMA_TREND + 10, ATR_AVG_PERIOD + ATR_PERIOD)
    cooldown = 0
    filter_stats = {
        "total_crossovers": 0,
        "skipped_time": 0,
        "skipped_cooldown": 0,
        "skipped_trend": 0,
        "skipped_rsi": 0,
        "skipped_volume": 0,
        "skipped_body": 0,
        "skipped_atr": 0,
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
        if not passes_time_filter(df.index[i]):
            filter_stats["skipped_time"] += 1
            i += 1
            continue

        # ── FILTER 2: Cooldown after SL ──
        if cooldown > 0:
            cooldown -= 1
            filter_stats["skipped_cooldown"] += 1
            i += 1
            continue

        # ── FILTER 3: 50 EMA trend slope ──
        if not passes_trend_filter(df, i, direction):
            filter_stats["skipped_trend"] += 1
            i += 1
            continue

        # ── FILTER 4: RSI ──
        if not passes_rsi_filter(row["rsi"], direction):
            filter_stats["skipped_rsi"] += 1
            i += 1
            continue

        # ── FILTER 5: Volume spike ──
        if not passes_volume_filter(row["Volume"], row["vol_avg"]):
            filter_stats["skipped_volume"] += 1
            i += 1
            continue

        # ── FILTER 6: Candle body strength ──
        if not passes_body_filter(row["body_ratio"]):
            filter_stats["skipped_body"] += 1
            i += 1
            continue

        # ── FILTER 7: ATR expansion ──
        if not passes_atr_expansion(row["atr"], row["atr_avg"]):
            filter_stats["skipped_atr"] += 1
            i += 1
            continue

        # ── SL / TARGET calculation (ATR-based) ──
        atr = row["atr"]
        if direction == "BUY":
            sl = close - (ATR_MULTIPLIER * atr)
            target1 = close + (ATR_MULTIPLIER * atr * 1.5)
            target2 = close + (ATR_MULTIPLIER * atr * 2.5)
        else:
            sl = close + (ATR_MULTIPLIER * atr)
            target1 = close - (ATR_MULTIPLIER * atr * 1.5)
            target2 = close - (ATR_MULTIPLIER * atr * 2.5)

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
            exit_price = df.iloc[min(i + 40, len(df) - 1)]["Close"]
            exit_time = df.index[min(i + 40, len(df) - 1)]
            pnl_pts = (
                exit_price - entry_price
                if direction == "BUY"
                else entry_price - exit_price
            )

        # ── Set cooldown if SL hit ──
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
            "rsi": round(row["rsi"], 1),
            "body_ratio": round(row["body_ratio"], 2),
            "outcome": outcome,
            "exit_price": round(exit_price, 2) if exit_price else None,
            "exit_time": exit_time,
            "pnl_pts": round(pnl_pts, 2) if pnl_pts else 0,
        })

        i = df.index.get_loc(exit_time) + 1 if exit_time in df.index else i + 1

    return pd.DataFrame(trades), filter_stats


def print_report(trades_df: pd.DataFrame, label: str, filter_stats: dict):
    # ── Filter breakdown ──
    print("\n" + "=" * 55)
    print(f"FILTER BREAKDOWN — {label}")
    print("=" * 55)
    print(f"Total Crossovers Detected : {filter_stats['total_crossovers']}")
    print(f"  Skipped (Time-of-Day)   : {filter_stats['skipped_time']}")
    print(f"  Skipped (Cooldown)      : {filter_stats['skipped_cooldown']}")
    print(f"  Skipped (Trend 50 EMA)  : {filter_stats['skipped_trend']}")
    print(f"  Skipped (RSI)           : {filter_stats['skipped_rsi']}")
    print(f"  Skipped (Volume)        : {filter_stats['skipped_volume']}")
    print(f"  Skipped (Candle Body)   : {filter_stats['skipped_body']}")
    print(f"  Skipped (ATR Expansion) : {filter_stats['skipped_atr']}")
    print(f"  Skipped (RR Ratio)      : {filter_stats['skipped_rr']}")
    print(f"  ✅ PASSED ALL FILTERS   : {filter_stats['passed_all']}")

    if trades_df.empty:
        print(f"\n{label}: No trades after filtering.")
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

    t1_hits = len(trades_df[trades_df["outcome"] == "T1_HIT"])
    t2_hits = len(trades_df[trades_df["outcome"] == "T2_HIT"])
    sl_hits = len(trades_df[trades_df["outcome"] == "SL_HIT"])
    open_exits = len(trades_df[trades_df["outcome"] == "OPEN"])

    print("\n" + "=" * 55)
    print(f"BACKTEST REPORT — {label}")
    print(f"Strategy: {EMA_FAST}/{EMA_SLOW} EMA Crossover + 7 Filters")
    print("=" * 55)

    print(f"Total Trades   : {total}")
    print(f"Wins           : {len(wins)}")
    print(f"Losses         : {len(losses)}")
    print(f"Win Rate       : {win_rate:.2f}%")
    print(f"Profit Factor  : {pf:.2f}")
    print(f"Total PnL      : {total_pnl:.2f} pts")
    print(f"Avg Win        : {avg_win:.2f} pts")
    print(f"Avg Loss       : {avg_loss:.2f} pts")
    print(f"Max Drawdown   : {max_dd:.2f} pts")

    print(f"\nOutcome Breakdown:")
    print(f"  T1 Hit   : {t1_hits}")
    print(f"  T2 Hit   : {t2_hits}")
    print(f"  SL Hit   : {sl_hits}")
    print(f"  Open Exit: {open_exits}")

    if win_rate >= 75:
        print(f"\n🎯 TARGET ACHIEVED: Win Rate {win_rate:.1f}% >= 75%")
    else:
        print(f"\n⚠️  Win Rate {win_rate:.1f}% — below 75% target. Tune filters in config.py.")

    print("\nAll Trades:")
    print(trades_df.to_string(index=False))

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
        trades, filter_stats = backtest(df, label)
        print_report(trades, label, filter_stats)


if __name__ == "__main__":
    main()