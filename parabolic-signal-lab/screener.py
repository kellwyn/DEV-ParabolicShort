"""
Parabolic Short Strategy — Full Market Screener
================================================
Scans the US stock universe for parabolic move setups matching
the Marios Stamatoudis playbook criteria:

  Filter 1 – Size of move from last 20-day MA base touch
    Small-cap  (market cap < $2B)  : ≥ 200%
    Mid-cap    ($2B – $10B)        : ≥ 100%
    Large-cap  (> $10B)            : ≥  50%

  Filter 2 – Aggressive structure (slope acceleration proxy)
    Average of last 5 daily ranges ≥ N × average of prior 15 ranges

Output
------
  • Console table of qualifying symbols ranked by move %
  • CSV file: parabolic_candidates_YYYYMMDD.csv
  • Optional: mt5_watchlist.txt  (one symbol per line, paste into MT5)

Usage
-----
  pip install yfinance pandas numpy tqdm
  python screener.py
  python screener.py --universe sp500          # S&P 500 only
  python screener.py --universe russell2000    # Russell 2000
  python screener.py --universe custom --file tickers.txt
  python screener.py --small-cap-pct 150 --mid-cap-pct 80
"""

import argparse
import csv
import datetime
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from tqdm import tqdm


# ─── Cap-category thresholds ──────────────────────────────────────────────────
SMALL_CAP_MAX_B   = 2.0    # market cap < $2B  → small-cap
MID_CAP_MAX_B     = 10.0   # market cap < $10B → mid-cap
                            # else              → large-cap

# ─── Default move thresholds (can be overridden via CLI) ──────────────────────
DEFAULT_SMALL_CAP_PCT  = 200.0
DEFAULT_MID_CAP_PCT    = 100.0
DEFAULT_LARGE_CAP_PCT  = 50.0

# ─── Structure acceleration filter ───────────────────────────────────────────
ACCEL_RECENT_BARS  = 5     # recent bars for range average
ACCEL_PRIOR_BARS   = 15    # prior bars for baseline range average
ACCEL_MULTIPLIER   = 1.5   # recent avg range must be ≥ N × prior avg range

MA_PERIOD          = 20    # days
MA_LOOKBACK        = 90    # max daily bars to look back for last MA touch


# ─── Stock universe helpers ───────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    """Fetch current S&P 500 tickers from Wikipedia."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", header=0
        )
        return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"[WARN] Could not fetch S&P 500 list: {e}")
        return []


def get_russell2000_tickers() -> list[str]:
    """
    Russell 2000 tickers — iShares IWM holdings are the canonical source.
    Falls back to a curated small-cap list if the download fails.
    """
    try:
        url = (
            "https://www.ishares.com/us/products/239710/IWM/"
            "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
        )
        df = pd.read_csv(url, skiprows=9, on_bad_lines="skip")
        col = [c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()]
        if col:
            tickers = df[col[0]].dropna().astype(str).tolist()
            return [t.strip() for t in tickers if t.strip() and t.strip() != "-"]
    except Exception as e:
        print(f"[WARN] Could not fetch Russell 2000 list: {e}")
    return []


def get_nasdaq100_tickers() -> list[str]:
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100", header=0
        )
        for t in tables:
            if "Ticker" in t.columns:
                return t["Ticker"].dropna().tolist()
    except Exception as e:
        print(f"[WARN] Could not fetch Nasdaq-100 list: {e}")
    return []


def load_custom_tickers(filepath: str) -> list[str]:
    p = Path(filepath)
    if not p.exists():
        print(f"[ERROR] Ticker file not found: {filepath}")
        sys.exit(1)
    tickers = []
    with open(p) as f:
        for line in f:
            t = line.strip().upper()
            if t and not t.startswith("#"):
                tickers.append(t)
    return tickers


# ─── Core analysis ────────────────────────────────────────────────────────────

def analyse_ticker(ticker: str, cfg: dict) -> dict | None:
    """
    Returns a result dict if the symbol qualifies, else None.
    Downloads D1 data via yfinance (cached per session via yf internal cache).
    """
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period="6mo", interval="1d", auto_adjust=True)

        if hist is None or len(hist) < MA_PERIOD + ACCEL_RECENT_BARS + ACCEL_PRIOR_BARS:
            return None

        closes = hist["Close"].values.astype(float)
        highs  = hist["High"].values.astype(float)
        lows   = hist["Low"].values.astype(float)
        n      = len(closes)

        # ── 20-day SMA ───────────────────────────────────────────
        ma = np.array([
            closes[i - MA_PERIOD + 1 : i + 1].mean()
            if i >= MA_PERIOD - 1 else np.nan
            for i in range(n)
        ])

        # ── Last 20MA base touch (most recent bar where close ≤ MA) ─
        touch_bar = -1
        for i in range(n - 2, MA_PERIOD - 1, -1):
            if np.isnan(ma[i]):
                continue
            if closes[i] <= ma[i]:
                touch_bar = i
                break

        if touch_bar < 0:
            return None

        base_close  = closes[touch_bar]
        cur_close   = closes[-1]
        if base_close <= 0:
            return None

        move_pct = ((cur_close - base_close) / base_close) * 100.0

        # ── Market cap & threshold ────────────────────────────────
        info       = tk.fast_info
        mkt_cap_b  = getattr(info, "market_cap", None)
        mkt_cap_b  = mkt_cap_b / 1e9 if mkt_cap_b else None

        if mkt_cap_b is not None:
            if mkt_cap_b < SMALL_CAP_MAX_B:
                cap_label   = "SMALL"
                threshold   = cfg["small_cap_pct"]
            elif mkt_cap_b < MID_CAP_MAX_B:
                cap_label   = "MID"
                threshold   = cfg["mid_cap_pct"]
            else:
                cap_label   = "LARGE"
                threshold   = cfg["large_cap_pct"]
        else:
            # Unknown cap — use small-cap threshold (conservative)
            cap_label   = "UNKNOWN"
            threshold   = cfg["small_cap_pct"]

        if move_pct < threshold:
            return None

        # ── Filter 2: Structure acceleration ─────────────────────
        ranges       = highs - lows
        recent_avg   = ranges[-ACCEL_RECENT_BARS:].mean()
        prior_start  = -(ACCEL_RECENT_BARS + ACCEL_PRIOR_BARS)
        prior_end    = -ACCEL_RECENT_BARS
        prior_avg    = ranges[prior_start:prior_end].mean()
        accelerating = (prior_avg > 0 and recent_avg >= ACCEL_MULTIPLIER * prior_avg)

        bars_since_touch = (n - 1) - touch_bar

        return {
            "ticker":           ticker,
            "cap":              cap_label,
            "mkt_cap_b":        round(mkt_cap_b, 2) if mkt_cap_b else None,
            "move_pct":         round(move_pct, 1),
            "threshold_pct":    threshold,
            "cur_close":        round(cur_close, 2),
            "base_close":       round(base_close, 2),
            "bars_since_touch": bars_since_touch,
            "accelerating":     accelerating,
            "recent_range_avg": round(recent_avg, 4),
            "prior_range_avg":  round(prior_avg, 4),
        }

    except Exception:
        return None


# ─── Output helpers ───────────────────────────────────────────────────────────

def print_table(results: list[dict]) -> None:
    if not results:
        print("\nNo qualifying symbols found.")
        return

    hdr = f"{'#':<4} {'Ticker':<8} {'Cap':<8} {'Mkt Cap':>9} {'Move%':>8} {'Thresh':>7} {'Price':>8} {'Base':>8} {'Bars':>5} {'Accel':<6}"
    print("\n" + "═" * len(hdr))
    print("  PARABOLIC SHORT CANDIDATES")
    print("═" * len(hdr))
    print(hdr)
    print("─" * len(hdr))
    for i, r in enumerate(results, 1):
        cap_str = f"${r['mkt_cap_b']:.1f}B" if r["mkt_cap_b"] else "n/a"
        accel   = "YES" if r["accelerating"] else "no"
        print(
            f"{i:<4} {r['ticker']:<8} {r['cap']:<8} {cap_str:>9} "
            f"{r['move_pct']:>7.1f}% {r['threshold_pct']:>6.0f}% "
            f"{r['cur_close']:>8.2f} {r['base_close']:>8.2f} "
            f"{r['bars_since_touch']:>5} {accel:<6}"
        )
    print("═" * len(hdr))
    print(f"  {len(results)} qualifying symbols\n")


def save_csv(results: list[dict], out_dir: Path) -> Path:
    today    = datetime.date.today().strftime("%Y%m%d")
    out_path = out_dir / f"parabolic_candidates_{today}.csv"
    if not results:
        return out_path
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    return out_path


def save_mt5_watchlist(results: list[dict], out_dir: Path) -> Path:
    """One ticker per line — paste into MT5 Market Watch → Symbols → Import."""
    today    = datetime.date.today().strftime("%Y%m%d")
    out_path = out_dir / f"mt5_watchlist_{today}.txt"
    with open(out_path, "w") as f:
        for r in results:
            # MT5 brokers often use suffixes like .US — adjust here if needed
            f.write(r["ticker"] + "\n")
    return out_path


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parabolic Short Strategy screener")
    p.add_argument(
        "--universe",
        choices=["sp500", "nasdaq100", "russell2000", "custom"],
        default="sp500",
        help="Stock universe to scan (default: sp500)",
    )
    p.add_argument("--file",    default="tickers.txt", help="Ticker file for --universe custom")
    p.add_argument("--small-cap-pct", type=float, default=DEFAULT_SMALL_CAP_PCT)
    p.add_argument("--mid-cap-pct",   type=float, default=DEFAULT_MID_CAP_PCT)
    p.add_argument("--large-cap-pct", type=float, default=DEFAULT_LARGE_CAP_PCT)
    p.add_argument("--no-accel-filter", action="store_true",
                   help="Skip structure acceleration check (Filter 2)")
    p.add_argument("--out-dir", default=".", help="Output directory for CSV / watchlist")
    p.add_argument("--delay",   type=float, default=0.15,
                   help="Seconds between yfinance requests (rate-limit courtesy)")
    return p.parse_args()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    cfg = {
        "small_cap_pct": args.small_cap_pct,
        "mid_cap_pct":   args.mid_cap_pct,
        "large_cap_pct": args.large_cap_pct,
    }

    # ── Load universe ─────────────────────────────────────────────
    print(f"\nLoading universe: {args.universe} ...")
    if args.universe == "sp500":
        tickers = get_sp500_tickers()
    elif args.universe == "nasdaq100":
        tickers = get_nasdaq100_tickers()
    elif args.universe == "russell2000":
        tickers = get_russell2000_tickers()
    else:
        tickers = load_custom_tickers(args.file)

    if not tickers:
        print("[ERROR] No tickers loaded. Exiting.")
        sys.exit(1)

    print(f"Scanning {len(tickers)} symbols — thresholds: "
          f"Small≥{cfg['small_cap_pct']}%  Mid≥{cfg['mid_cap_pct']}%  Large≥{cfg['large_cap_pct']}%")
    print(f"Structure accel filter: {'OFF' if args.no_accel_filter else 'ON'} "
          f"(recent {ACCEL_RECENT_BARS}d avg ≥ {ACCEL_MULTIPLIER}× prior {ACCEL_PRIOR_BARS}d avg)\n")

    # ── Scan ──────────────────────────────────────────────────────
    results = []
    errors  = 0

    for ticker in tqdm(tickers, unit="sym", ncols=80):
        res = analyse_ticker(ticker, cfg)
        if res is not None:
            # Optionally require structure acceleration (Filter 2)
            if not args.no_accel_filter and not res["accelerating"]:
                continue
            results.append(res)
        time.sleep(args.delay)

    # Sort by move % descending
    results.sort(key=lambda x: x["move_pct"], reverse=True)

    # ── Print & save ──────────────────────────────────────────────
    print_table(results)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = save_csv(results, out_dir)
    wl_path  = save_mt5_watchlist(results, out_dir)

    if results:
        print(f"CSV saved      : {csv_path}")
        print(f"MT5 watchlist  : {wl_path}")
        print("\nNext step: in MetaTrader 5 → Market Watch (Ctrl+M)")
        print("  Right-click → Show All, then search for each symbol,")
        print("  OR use the mt5_watchlist file as a reference to add symbols manually.")
    else:
        print("No results to save.")


if __name__ == "__main__":
    main()
