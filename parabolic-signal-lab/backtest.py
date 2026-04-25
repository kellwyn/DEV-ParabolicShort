"""
Parabolic Short Strategy — Backtester
======================================
Uses yfinance 60-min intraday data (up to 730 days) to simulate the full
playbook on each qualifying setup found by screener.py.

What it tests:
  Filter 3  Exhaustion day: price loses intraday VWAP, lower highs form
  Entry     VWAP_REJECTION | VWAP_LOSS | BREAK_STRUCTURE (configurable)
  Exit      Stop = day high + buffer | TP1 at R-multiple | EOD close
  Risk mgmt Partial close at TP1, stop to break-even, max 2 attempts/day

Results reported by stock category:
  MARKET_LEADER   — Large-cap (≥50% move threshold)
  THEMATIC        — Mid-cap hype/sector runners (≥100%)
  IPO_SMALL_CAP   — Small-cap / recent IPOs (≥200%)

Usage:
  # Run screener first, then backtest its output CSV:
  python screener.py --universe sp500 --out-dir output
  python backtest.py --candidates output/parabolic_candidates_YYYYMMDD.csv

  # Or backtest specific tickers directly:
  python backtest.py --tickers SMCI NVDA MSTR --entry VWAP_REJECTION

  # Backtest all three entry types and compare:
  python backtest.py --candidates output/parabolic_candidates_*.csv --compare-entries
"""

import argparse
import csv
import datetime
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yfinance as yf
from tqdm import tqdm

# ─── Config ───────────────────────────────────────────────────────────────────
ENTRY_TYPES   = ("VWAP_REJECTION", "VWAP_LOSS", "BREAK_STRUCTURE")
MA_PERIOD     = 20
MA_LOOKBACK   = 90
ACCEL_RECENT  = 5
ACCEL_PRIOR   = 15
ACCEL_MULT    = 1.5
MAX_PULLBACKS = 3
PULLBACK_PCT  = 2.0

EOD_HOUR      = 15     # server-time hour for forced EOD close
EOD_MINUTE    = 45
STOP_BUF_PCT  = 0.001  # 0.1% buffer above stop anchor
VWAP_BAND_PCT = 0.003  # 0.3% band for VWAP rejection detection
TP1_RATIO     = 1.0    # Take TP1 at 1:1 R
TP1_CLOSE_PCT = 0.50   # Close 50% at TP1
MAX_ATTEMPTS  = 2      # Playbook: max 2 attempts per exhaustion window


# ─── Data structures ──────────────────────────────────────────────────────────
@dataclass
class Trade:
    ticker:     str
    date:       datetime.date
    category:   str
    entry_type: str
    entry_px:   float
    stop_px:    float
    tp1_px:     float
    exit_px:    float
    exit_reason:str   # TP1 | STOP | EOD | BE_STOP
    r_multiple: float
    pnl_pct:    float


@dataclass
class DayResult:
    """Result for a single exhaustion day simulation."""
    ticker:     str
    date:       datetime.date
    category:   str
    exhaustion: bool = False
    trades:     list[Trade] = field(default_factory=list)


# ─── VWAP ─────────────────────────────────────────────────────────────────────
def compute_vwap_series(df_day: pd.DataFrame) -> pd.Series:
    """Compute cumulative intraday VWAP (D1 reset) from H1 bars."""
    tp  = (df_day["High"] + df_day["Low"] + df_day["Close"]) / 3.0
    vol = df_day["Volume"].replace(0, 1).astype(float)
    cum_pv  = (tp * vol).cumsum()
    cum_vol = vol.cumsum()
    return cum_pv / cum_vol


# ─── Core: analyse one exhaustion day ─────────────────────────────────────────
def simulate_day(
    ticker:     str,
    date:       datetime.date,
    df_day:     pd.DataFrame,
    category:   str,
    entry_type: str,
    day_high_d1:float,
) -> DayResult:
    """
    Simulate a single trading day on H1 bars.
    Returns a DayResult with trade(s) if any were taken.
    """
    result = DayResult(ticker=ticker, date=date, category=category)
    if len(df_day) < 3:
        return result

    vwap   = compute_vwap_series(df_day)
    highs  = df_day["High"].values
    lows   = df_day["Low"].values
    closes = df_day["Close"].values
    opens  = df_day["Open"].values
    times  = df_day.index

    # Exhaustion condition: price crossed below VWAP at some point today
    vwap_crossed = False
    bars_below   = 0
    max_high_so_far = highs[0]

    # Track intraday structure
    bar_highs: list[float] = []
    bar_lows:  list[float] = []

    attempts   = 0
    in_trade   = False
    entry_px   = 0.0
    stop_px    = 0.0
    tp1_px     = 0.0
    be_moved   = False
    tp1_hit    = False
    stop_at_be = False

    for i, (h, l, c, o, vw) in enumerate(
        zip(highs, lows, closes, opens, vwap.values)
    ):
        bar_time = times[i]

        # ── EOD check ──────────────────────────────────────────────────────
        is_eod = (hasattr(bar_time, "hour") and
                  (bar_time.hour > EOD_HOUR or
                   (bar_time.hour == EOD_HOUR and bar_time.minute >= EOD_MINUTE)))

        if in_trade and is_eod:
            pnl_pct    = (entry_px - c) / entry_px * 100.0
            r_multiple = (entry_px - c) / (stop_px - entry_px) if stop_px > entry_px else 0
            result.trades.append(Trade(
                ticker=ticker, date=date, category=category,
                entry_type=entry_type, entry_px=entry_px,
                stop_px=stop_px, tp1_px=tp1_px, exit_px=c,
                exit_reason="EOD", r_multiple=round(r_multiple, 2),
                pnl_pct=round(pnl_pct, 2)
            ))
            in_trade = False
            break

        # ── Update VWAP tracking ────────────────────────────────────────────
        if c < vw:
            bars_below  += 1
            vwap_crossed = True
        else:
            bars_below   = 0

        # Track structure
        if i > 0:
            bar_highs.append(highs[i-1])
            bar_lows.append(lows[i-1])

        # Lower high + lower low check
        has_lhll = (
            len(bar_highs) >= 2 and
            bar_highs[-1] < bar_highs[-2] and
            bar_lows[-1]  < bar_lows[-2]
        )

        # ── Manage open position ────────────────────────────────────────────
        if in_trade:
            # Stop hit
            if h >= stop_px:
                pnl_pct    = (entry_px - stop_px) / entry_px * 100.0
                r_multiple = -1.0 if not stop_at_be else 0.0
                result.trades.append(Trade(
                    ticker=ticker, date=date, category=category,
                    entry_type=entry_type, entry_px=entry_px,
                    stop_px=stop_px, tp1_px=tp1_px, exit_px=stop_px,
                    exit_reason="BE_STOP" if stop_at_be else "STOP",
                    r_multiple=round(r_multiple, 2),
                    pnl_pct=round(pnl_pct, 2)
                ))
                in_trade = False
                attempts += 1
                continue

            # TP1 hit
            if not tp1_hit and l <= tp1_px:
                # Close 50%, move stop to BE
                tp1_hit   = True
                stop_px   = entry_px  # break-even
                stop_at_be = True

            continue

        # ── Entry signals (only when not in trade, below VWAP, within window) ──
        if attempts >= MAX_ATTEMPTS:
            break
        if not vwap_crossed:
            continue
        if bars_below < 1:
            continue
        if is_eod:
            break

        triggered = False
        if entry_type == "VWAP_REJECTION" and i > 0:
            # Previous bar: opened below VWAP, tagged it (high within band), closed below
            band    = vwap.values[i-1] * VWAP_BAND_PCT
            prev_vw = vwap.values[i-1]
            triggered = (
                opens[i-1] < prev_vw and
                highs[i-1] >= prev_vw - band and
                closes[i-1] < prev_vw
            )

        elif entry_type == "VWAP_LOSS" and i > 1:
            # Previous bar closed below VWAP for the first time AND LH+LL
            triggered = (
                closes[i-1] < vwap.values[i-1] and
                closes[i-2] >= vwap.values[i-2] and
                has_lhll
            )

        elif entry_type == "BREAK_STRUCTURE":
            triggered = has_lhll and c < vw

        if not triggered:
            continue

        # ── Execute entry ────────────────────────────────────────────────────
        # Stop = day high (D1) + buffer, or most recent intraday high + buffer
        stop_anchor = max(day_high_d1, h)
        entry_price = c
        stop_price  = stop_anchor * (1.0 + STOP_BUF_PCT)
        risk_dist   = stop_price - entry_price

        if risk_dist <= 0:
            continue

        tp1_price  = entry_price - risk_dist * TP1_RATIO
        entry_px   = entry_price
        stop_px    = stop_price
        tp1_px     = tp1_price
        in_trade   = True
        tp1_hit    = False
        stop_at_be = False
        result.exhaustion = True

    return result


# ─── Backtest a single ticker ──────────────────────────────────────────────────
def backtest_ticker(
    ticker:     str,
    category:   str,
    entry_type: str,
    move_pct:   float,
    threshold:  float,
) -> list[Trade]:
    """
    Downloads 60-min intraday data and daily data for ticker.
    Finds all qualifying parabolic periods and simulates each exhaustion day.
    """
    try:
        # Daily data — used for setup detection (Filters 1 + 2)
        hist_d = yf.download(ticker, period="2y", interval="1d",
                              auto_adjust=True, progress=False)
        if hist_d is None or len(hist_d) < MA_PERIOD + 20:
            return []

        # 60-min intraday — used for Filter 3 + entry/exit simulation
        # yfinance provides up to 730 days of 60-min data
        hist_h = yf.download(ticker, period="730d", interval="60m",
                              auto_adjust=True, progress=False)
        if hist_h is None or len(hist_h) < 20:
            return []

        closes_d = hist_d["Close"].values.astype(float)
        opens_d  = hist_d["Open"].values.astype(float)
        highs_d  = hist_d["High"].values.astype(float)
        lows_d   = hist_d["Low"].values.astype(float)
        n        = len(closes_d)

        # Build 20MA series
        ma20 = np.array([
            closes_d[max(0, i - MA_PERIOD + 1):i + 1].mean()
            if i >= MA_PERIOD - 1 else np.nan
            for i in range(n)
        ])

        all_trades: list[Trade] = []

        # Scan each D1 bar looking for parabolic setup + next exhaustion day
        for d_idx in range(MA_PERIOD + 20, n - 1):
            # Find last MA base touch before this bar
            touch_idx = -1
            for j in range(d_idx - 1, MA_PERIOD - 1, -1):
                if not np.isnan(ma20[j]) and closes_d[j] <= ma20[j]:
                    touch_idx = j
                    break
            if touch_idx < 0:
                continue

            base_close = closes_d[touch_idx]
            if base_close <= 0:
                continue

            move = ((closes_d[d_idx] - base_close) / base_close) * 100.0
            if move < threshold:
                continue

            # Check structure quality (Filter 2)
            ranges      = highs_d[d_idx-ACCEL_RECENT:d_idx] - lows_d[d_idx-ACCEL_RECENT:d_idx]
            prior_ranges = highs_d[d_idx-ACCEL_RECENT-ACCEL_PRIOR:d_idx-ACCEL_RECENT] - \
                           lows_d[d_idx-ACCEL_RECENT-ACCEL_PRIOR:d_idx-ACCEL_RECENT]
            accel = (prior_ranges.mean() > 0 and
                     ranges.mean() >= ACCEL_MULT * prior_ranges.mean())

            pullbacks = sum(
                1 for i in range(touch_idx + 1, d_idx)
                if opens_d[i] > 0 and
                (closes_d[i] - opens_d[i]) / opens_d[i] * 100.0 <= -PULLBACK_PCT
            )
            f2_pass = accel or (pullbacks <= MAX_PULLBACKS)
            if not f2_pass:
                continue

            # Check the next 1–2 D1 bars for an exhaustion day
            bar_date_d = hist_d.index[d_idx].date()

            for look_ahead in range(1, 3):  # tomorrow + day after
                if d_idx + look_ahead >= n:
                    break
                exhaustion_date = hist_d.index[d_idx + look_ahead].date()
                day_high_d1     = highs_d[d_idx + look_ahead]

                # Extract the H1 bars for that day
                try:
                    df_day = hist_h[hist_h.index.date == exhaustion_date].copy()
                except Exception:
                    continue
                if len(df_day) < 3:
                    continue

                day_result = simulate_day(
                    ticker=ticker,
                    date=exhaustion_date,
                    df_day=df_day,
                    category=category,
                    entry_type=entry_type,
                    day_high_d1=day_high_d1,
                )

                all_trades.extend(day_result.trades)

                # Stop looking ahead if we found an exhaustion day
                if day_result.exhaustion:
                    break

        return all_trades

    except Exception as e:
        print(f"  [{ticker}] error: {e}")
        return []


# ─── Reporting ─────────────────────────────────────────────────────────────────
def report(all_trades: list[Trade], entry_type: str) -> None:
    if not all_trades:
        print("No trades to report.")
        return

    df = pd.DataFrame([t.__dict__ for t in all_trades])

    print(f"\n{'═'*65}")
    print(f"  BACKTEST RESULTS — Entry: {entry_type}")
    print(f"  Period: {df['date'].min()}  to  {df['date'].max()}")
    print(f"{'═'*65}")

    cats = ["MARKET_LEADER", "THEMATIC", "IPO_SMALL_CAP"]
    for cat in cats:
        grp = df[df["category"] == cat]
        if grp.empty:
            continue
        wins   = (grp["r_multiple"] > 0).sum()
        total  = len(grp)
        avg_r  = grp["r_multiple"].mean()
        win_r  = grp.loc[grp["r_multiple"] > 0, "r_multiple"].mean()
        loss_r = grp.loc[grp["r_multiple"] <= 0, "r_multiple"].mean()
        exits  = grp["exit_reason"].value_counts().to_dict()

        label = {
            "MARKET_LEADER": "Market Leaders",
            "THEMATIC":      "Thematic / Mid-cap",
            "IPO_SMALL_CAP": "IPO / Small-cap",
        }[cat]

        print(f"\n  {label}")
        print(f"  {'─'*50}")
        print(f"  Trades     : {total}")
        print(f"  Win rate   : {wins}/{total}  ({wins/total*100:.1f}%)")
        print(f"  Avg R      : {avg_r:.2f}")
        print(f"  Avg win R  : {win_r:.2f}" if wins > 0 else "  Avg win R  : —")
        print(f"  Avg loss R : {loss_r:.2f}" if total - wins > 0 else "  Avg loss R : —")
        print(f"  Exit reasons: {exits}")

        top5 = grp.nlargest(5, "r_multiple")[["ticker","date","r_multiple","pnl_pct","exit_reason"]]
        print(f"\n  Top 5 trades:")
        print(top5.to_string(index=False, float_format="{:.2f}".format))

    # Overall
    wins  = (df["r_multiple"] > 0).sum()
    total = len(df)
    print(f"\n{'─'*65}")
    print(f"  OVERALL  {wins}/{total} wins  ({wins/total*100:.1f}%)  avg R={df['r_multiple'].mean():.2f}")
    print(f"{'═'*65}")


def save_trades(trades: list[Trade], out_dir: Path) -> None:
    if not trades:
        return
    today = datetime.date.today().strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    path  = out_dir / f"backtest_trades_{today}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].__dict__.keys())
        writer.writeheader()
        writer.writerows(t.__dict__ for t in trades)
    print(f"\nTrades CSV: {path}")


# ─── CLI ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parabolic Short Backtester")
    p.add_argument("--candidates", help="CSV from screener.py output")
    p.add_argument("--tickers",    nargs="+", help="Specific tickers to backtest")
    p.add_argument("--category",   default="MARKET_LEADER,$THEMATIC,IPO_SMALL_CAP",
                   help="Comma-separated categories to include")
    p.add_argument("--entry",      default="VWAP_REJECTION",
                   choices=ENTRY_TYPES)
    p.add_argument("--compare-entries", action="store_true",
                   help="Run all 3 entry types and compare results")
    p.add_argument("--out-dir",    default="output")
    p.add_argument("--delay",      type=float, default=1.0,
                   help="Seconds between ticker downloads")
    return p.parse_args()


def load_candidates(csv_path: str) -> list[dict]:
    with open(csv_path) as f:
        return list(csv.DictReader(f))


def main() -> None:
    args = parse_args()

    # Build ticker list
    if args.tickers:
        candidates = [
            {"ticker": t, "category": "THEMATIC",
             "move_pct": "100", "threshold_pct": "100"}
            for t in args.tickers
        ]
    elif args.candidates:
        candidates = load_candidates(args.candidates)
    else:
        print("Provide --candidates CSV or --tickers list.")
        sys.exit(1)

    entry_types = ENTRY_TYPES if args.compare_entries else [args.entry]
    out_dir     = Path(args.out_dir)

    for entry_type in entry_types:
        print(f"\n{'─'*65}")
        print(f"Backtesting {len(candidates)} candidates  entry={entry_type}")
        print(f"{'─'*65}")

        all_trades: list[Trade] = []
        for cand in tqdm(candidates, unit="ticker", ncols=80):
            ticker    = cand["ticker"]
            category  = cand.get("category", "THEMATIC")
            move_pct  = float(cand.get("move_pct",  100))
            threshold = float(cand.get("threshold_pct", 100))

            trades = backtest_ticker(
                ticker=ticker,
                category=category,
                entry_type=entry_type,
                move_pct=move_pct,
                threshold=threshold,
            )
            all_trades.extend(trades)
            time.sleep(args.delay)

        report(all_trades, entry_type)
        save_trades(all_trades, out_dir)


if __name__ == "__main__":
    main()
