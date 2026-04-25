"""
Parabolic Short Strategy — Full Market Screener
================================================
Scans the NASDAQ, NYSE, and S&P 500 universe for stocks meeting all
playbook conditions, classified by market category:

  MARKET_LEADER  Large-cap > $10B  — threshold ≥ 50%   (stable names, 10–20% expected drop)
  THEMATIC       Mid-cap  $2–10B   — threshold ≥ 100%  (sector/theme runners, 20–40% drop)
  IPO_SMALL_CAP  Small-cap < $2B   — threshold ≥ 200%  (most aggressive, sharpest reversals)

Data: yfinance (free, no API key)
Enrichment: OctagonAI MCP (top candidates only — run /financial-analyst-master <TICKER>)

Usage:
  python screener.py                                    # scan S&P 500
  python screener.py --universe nasdaq                  # full NASDAQ
  python screener.py --universe all                     # NASDAQ + NYSE + S&P 500
  python screener.py --universe all --category THEMATIC # thematic only
  python screener.py --tickers NVDA SMCI MSTR           # specific tickers
  python screener.py --no-accel-filter                  # skip Filter 2 for wider results
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

# ─── Strategy constants ────────────────────────────────────────────────────────
MA_PERIOD       = 20    # D1 bars for base MA
MA_LOOKBACK     = 90    # max bars back to find last MA touch
ACCEL_RECENT    = 5     # candle range acceleration — recent window
ACCEL_PRIOR     = 15    # candle range acceleration — prior baseline
ACCEL_MULT      = 1.5   # recent avg range must be >= N× prior avg range
MAX_PULLBACKS   = 3     # max significant down-days (pullbacks) in the run
PULLBACK_PCT    = 2.0   # min % daily decline to count as a pressure-release pullback

# Market cap boundaries
LARGE_CAP_B     = 10.0
MID_CAP_B       = 2.0

# Default thresholds
DEFAULT_THRESHOLDS = {
    "MARKET_LEADER":  50.0,
    "THEMATIC":       100.0,
    "IPO_SMALL_CAP":  200.0,
}


# ─── Universe helpers ──────────────────────────────────────────────────────────
def get_sp500() -> list[str]:
    try:
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )[0]
        return df["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"[WARN] S&P 500 fetch failed: {e}")
        return []


def get_exchange_tickers(exchange: str) -> list[str]:
    """
    Fetch listed stocks from NASDAQ trader FTP directory.
    exchange: 'nasdaq' | 'nyse'
    """
    import requests
    urls = {
        "nasdaq": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "nyse":   "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    }
    url = urls.get(exchange.lower())
    if not url:
        return []
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        tickers = []
        for line in r.text.splitlines()[1:]:
            if "|" not in line or line.startswith("File Creation"):
                continue
            parts = line.split("|")
            ticker = parts[0].strip()
            # Skip test symbols, preferred shares, warrants, units
            if not ticker or any(c in ticker for c in ["$", "+", " "]) or len(ticker) > 5:
                continue
            tickers.append(ticker)
        return tickers
    except Exception as e:
        print(f"[WARN] {exchange} FTP fetch failed: {e}")
        return []


def build_universe(universe: str) -> list[str]:
    """Return deduplicated ticker list for the requested universe."""
    tickers: list[str] = []
    if universe in ("sp500", "all"):
        tickers += get_sp500()
    if universe in ("nasdaq", "all"):
        tickers += get_exchange_tickers("nasdaq")
    if universe in ("nyse", "all"):
        tickers += get_exchange_tickers("nyse")
    if universe == "sp500" and not tickers:
        print("[ERROR] Could not fetch S&P 500 tickers.")
        sys.exit(1)
    # Deduplicate preserving order
    seen: set[str] = set()
    unique = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


# ─── Cap classification ────────────────────────────────────────────────────────
def classify_cap(mkt_cap_usd: float | None) -> str:
    if mkt_cap_usd is None:
        return "IPO_SMALL_CAP"
    b = mkt_cap_usd / 1e9
    if b >= LARGE_CAP_B:
        return "MARKET_LEADER"
    if b >= MID_CAP_B:
        return "THEMATIC"
    return "IPO_SMALL_CAP"


# ─── Core analysis ────────────────────────────────────────────────────────────
def analyse_ticker(
    ticker: str,
    thresholds: dict[str, float],
    require_accel: bool = True,
    category_filter: str | None = None,
) -> dict | None:
    """
    Downloads D1 data for ticker and checks all three playbook filters.
    Returns a result dict if the stock qualifies, else None.

    Filter 1 — Size of Move:  price ≥ threshold% from last 20MA base touch
    Filter 2 — Aggressive Structure: candle range acceleration + low pullback count
    (Filter 3 — Exhaustion Day is checked in backtest.py using intraday data)
    """
    try:
        tk    = yf.Ticker(ticker)
        hist  = tk.history(period="6mo", interval="1d", auto_adjust=True)

        if hist is None or len(hist) < MA_PERIOD + ACCEL_PRIOR + ACCEL_RECENT + 5:
            return None

        closes = hist["Close"].values.astype(float)
        opens  = hist["Open"].values.astype(float)
        highs  = hist["High"].values.astype(float)
        lows   = hist["Low"].values.astype(float)
        n      = len(closes)

        # ── Market cap & cap category ──────────────────────────────────────
        info      = tk.fast_info
        cap_usd   = getattr(info, "market_cap", None)
        cap_b     = cap_usd / 1e9 if cap_usd else None
        category  = classify_cap(cap_usd)
        threshold = thresholds.get(category, 200.0)

        if category_filter and category != category_filter:
            return None

        # ── Filter 1: Size of move from last 20MA base touch ──────────────
        ma20 = np.array([
            closes[max(0, i - MA_PERIOD + 1):i + 1].mean()
            if i >= MA_PERIOD - 1 else np.nan
            for i in range(n)
        ])

        # Most recent bar where close was at or below the 20MA
        touch_idx = -1
        for i in range(n - 2, MA_PERIOD - 1, -1):
            if not np.isnan(ma20[i]) and closes[i] <= ma20[i]:
                touch_idx = i
                break

        if touch_idx < 0:
            return None

        base_close = closes[touch_idx]
        cur_close  = closes[-1]
        if base_close <= 0:
            return None

        move_pct = ((cur_close - base_close) / base_close) * 100.0
        if move_pct < threshold:
            return None

        bars_since_touch = (n - 1) - touch_idx

        # ── Filter 2: Aggressive structure ────────────────────────────────
        # 2a. Candle-range acceleration: recent bars significantly larger than prior
        ranges       = highs - lows
        recent_avg   = ranges[-ACCEL_RECENT:].mean()
        prior_start  = -(ACCEL_RECENT + ACCEL_PRIOR)
        prior_end    = -ACCEL_RECENT
        prior_avg    = ranges[prior_start:prior_end].mean()
        accelerating = prior_avg > 0 and recent_avg >= ACCEL_MULT * prior_avg

        # 2b. Pullback count: few significant down-days = pressure NOT released
        # Playbook: "other stocks move up but include regular pullbacks;
        #            these pullbacks release pressure, which reduces the chance"
        pullbacks = 0
        for i in range(touch_idx + 1, n):
            if opens[i] > 0:
                daily_chg = (closes[i] - opens[i]) / opens[i] * 100.0
                if daily_chg <= -PULLBACK_PCT:
                    pullbacks += 1

        few_pullbacks = pullbacks <= MAX_PULLBACKS
        filter2_pass  = accelerating or few_pullbacks

        if require_accel and not filter2_pass:
            return None

        # ── Sector / theme flag ────────────────────────────────────────────
        sector = ""
        try:
            sector = tk.info.get("sector", "") or ""
        except Exception:
            pass

        return {
            "ticker":          ticker,
            "category":        category,
            "mkt_cap_b":       round(cap_b, 2) if cap_b else None,
            "threshold_pct":   threshold,
            "move_pct":        round(move_pct, 1),
            "cur_price":       round(cur_close, 2),
            "base_price":      round(base_close, 2),
            "bars_since_touch":bars_since_touch,
            "accelerating":    accelerating,
            "pullbacks":       pullbacks,
            "filter2_pass":    filter2_pass,
            "sector":          sector,
        }

    except Exception:
        return None


# ─── Output ────────────────────────────────────────────────────────────────────
def print_table(results: list[dict]) -> None:
    if not results:
        print("\nNo qualifying symbols found.")
        return

    # Group by category
    groups = {
        "MARKET_LEADER":  [r for r in results if r["category"] == "MARKET_LEADER"],
        "THEMATIC":       [r for r in results if r["category"] == "THEMATIC"],
        "IPO_SMALL_CAP":  [r for r in results if r["category"] == "IPO_SMALL_CAP"],
    }

    labels = {
        "MARKET_LEADER": f"MARKET LEADERS  (≥50% threshold)  —  expect 10–20% drop",
        "THEMATIC":      f"THEMATIC / MID-CAP  (≥100%)  —  expect 20–40% drop",
        "IPO_SMALL_CAP": f"IPO / SMALL-CAP  (≥200%)  —  most aggressive reversals",
    }

    hdr = f"{'#':<4} {'Ticker':<8} {'Cap $B':>8} {'Move%':>8} {'Need':>6} {'Price':>8} {'Base':>8} {'Bars':>5} {'F2':>4} {'Sector'}"
    sep = "─" * len(hdr)

    for cat, rows in groups.items():
        if not rows:
            continue
        print(f"\n{'═'*len(hdr)}")
        print(f"  {labels[cat]}  ({len(rows)} found)")
        print(f"{'═'*len(hdr)}")
        print(hdr)
        print(sep)
        for i, r in enumerate(rows, 1):
            cap_str = f"${r['mkt_cap_b']:.1f}" if r["mkt_cap_b"] else "n/a"
            print(
                f"{i:<4} {r['ticker']:<8} {cap_str:>8} "
                f"{r['move_pct']:>7.1f}% {r['threshold_pct']:>5.0f}% "
                f"{r['cur_price']:>8.2f} {r['base_price']:>8.2f} "
                f"{r['bars_since_touch']:>5} "
                f"{'✓' if r['filter2_pass'] else '✗':>4}  "
                f"{r['sector']}"
            )
        print(sep)

    print(f"\nTotal qualifying: {len(results)}")
    print("\nNext step: run /financial-analyst-master <TICKER> on top candidates for OctagonAI enrichment")


def save_outputs(results: list[dict], out_dir: Path) -> None:
    if not results:
        return
    today    = datetime.date.today().strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / f"parabolic_candidates_{today}.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    wl_path = out_dir / f"watchlist_{today}.txt"
    with open(wl_path, "w") as f:
        for r in results:
            f.write(f"{r['ticker']}\t{r['category']}\t{r['move_pct']}%\n")

    print(f"\nCSV     : {csv_path}")
    print(f"Watchlist: {wl_path}")


# ─── CLI ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parabolic Short Screener")
    p.add_argument("--universe",  default="sp500",
                   choices=["sp500", "nasdaq", "nyse", "all"],
                   help="Stock universe to scan")
    p.add_argument("--tickers",   nargs="+", help="Override with specific tickers")
    p.add_argument("--category",  choices=["MARKET_LEADER", "THEMATIC", "IPO_SMALL_CAP"],
                   help="Filter to one category only")
    p.add_argument("--no-accel-filter", action="store_true",
                   help="Skip Filter 2 (wider results)")
    p.add_argument("--delay",     type=float, default=0.1,
                   help="Seconds between requests")
    p.add_argument("--out-dir",   default="output",
                   help="Output directory for CSV / watchlist")
    return p.parse_args()


def main() -> None:
    args    = parse_args()
    tickers = args.tickers if args.tickers else build_universe(args.universe)

    thresholds = DEFAULT_THRESHOLDS.copy()

    print(f"\nParabolic Short Screener")
    print(f"Universe : {args.universe.upper()}  ({len(tickers)} symbols)")
    print(f"Category : {args.category or 'ALL'}")
    print(f"Filter 2 : {'OFF' if args.no_accel_filter else 'ON'}")
    print(f"Thresholds: Leader≥{thresholds['MARKET_LEADER']}%  "
          f"Thematic≥{thresholds['THEMATIC']}%  "
          f"Small≥{thresholds['IPO_SMALL_CAP']}%\n")

    results: list[dict] = []

    for ticker in tqdm(tickers, unit="sym", ncols=80):
        res = analyse_ticker(
            ticker,
            thresholds,
            require_accel=not args.no_accel_filter,
            category_filter=args.category,
        )
        if res:
            results.append(res)
        time.sleep(args.delay)

    # Sort by move % descending within each category
    results.sort(key=lambda r: (r["category"], -r["move_pct"]))

    print_table(results)
    save_outputs(results, Path(args.out_dir))


if __name__ == "__main__":
    main()
