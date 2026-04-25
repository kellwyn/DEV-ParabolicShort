"""
Stock Universe Builder - Full Export to CSV / XLSX
====================================================
Fetches every listed stock from NYSE, S&P 500, NASDAQ, and Russell 2000,
filters to market cap >= $100M, and exports to CSV + XLSX.

Two-phase approach for reliability at scale:
  Phase 1  NASDAQ Screener API  -- bulk market cap + price for all 12,000+ symbols
            (one API call per exchange, returns everything at once, no rate limits)
  Phase 2  yfinance fast_info   -- sector + industry for filtered subset only
            (only ~2,000-4,000 stocks after $100M cap filter)

Categories:
  MARKET_LEADER   market cap > $100B    (playbook threshold >= 50%)
  MID_LARGE_CAP   $10B  to $100B        (playbook threshold >= 100%)
  SMALL_CAP       $100M to $10B         (playbook threshold >= 200%)

Output columns:
  ticker, name, price, market_cap_b, market_cap_display,
  category, exchange, indices, in_sp500, in_nasdaq100, in_russell2000,
  sector, industry

Export:
  universe_YYYYMMDD.csv    -- all qualified stocks, flat file
  universe_YYYYMMDD.xlsx   -- 5 sheets: All / Market Leaders / Mid-Large / Small Cap / Summary

Usage:
  python build_universe_csv.py                   # default: all sources, cap >= $100M
  python build_universe_csv.py --min-cap 500     # raise filter to >= $500M
  python build_universe_csv.py --workers 10      # yfinance enrichment threads (default 10)
  python build_universe_csv.py --no-russell      # skip Russell 2000
  python build_universe_csv.py --no-enrich       # skip sector/industry (fastest)
  python build_universe_csv.py --out-dir data    # custom output path
"""

import argparse
import datetime
import io
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ─── Category thresholds ───────────────────────────────────────────────────────
MARKET_LEADER_B = 100.0   # > $100B
MID_LARGE_MIN_B = 10.0    # $10B – $100B
SMALL_CAP_MIN_B = 0.1     # $100M – $10B (filter floor, 0.1 = $100M)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

CAT_LABELS = {
    "MARKET_LEADER": "Market Leaders (>$100B)",
    "MID_LARGE_CAP": "Mid-Large Cap ($10B-$100B)",
    "SMALL_CAP":     "Small Cap ($100M-$10B)",
}


# ─── Phase 1: Bulk data from NASDAQ Screener API ───────────────────────────────

def _parse_nasdaq_cap(cap_str: str | None) -> float | None:
    """Convert NASDAQ API market cap string to float $B.  '$1.234B' -> 1.234"""
    if not cap_str or cap_str.strip() in ("", "N/A", "--"):
        return None
    s = str(cap_str).strip().replace("$", "").replace(",", "")
    try:
        if s.endswith("T"):   return float(s[:-1]) * 1000.0
        if s.endswith("B"):   return float(s[:-1])
        if s.endswith("M"):   return float(s[:-1]) / 1000.0
        if s.endswith("K"):   return float(s[:-1]) / 1_000_000.0
        return float(s) / 1e9
    except ValueError:
        return None


def _parse_price(price_str: str | None) -> float | None:
    if not price_str:
        return None
    try:
        return float(str(price_str).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def fetch_nasdaq_screener(exchange: str) -> list[dict]:
    """
    Calls the NASDAQ Screener API for one exchange.
    Returns list of {ticker, name, price, market_cap_b, exchange} dicts.
    exchange: 'nasdaq' | 'nyse' | 'amex'
    """
    url = (
        f"https://api.nasdaq.com/api/screener/stocks"
        f"?tableonly=true&limit=10000&offset=0&exchange={exchange}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("table", {}).get("rows") or []
        result = []
        for row in rows:
            ticker = str(row.get("symbol", "")).strip()
            if not ticker or len(ticker) > 5 or any(c in ticker for c in ["$", "^", " "]):
                continue
            result.append({
                "ticker":       ticker,
                "name":         str(row.get("name", "")).strip(),
                "price":        _parse_price(row.get("lastsale")),
                "market_cap_b": _parse_nasdaq_cap(row.get("marketCap")),
                "exchange":     exchange.upper(),
            })
        return result
    except Exception as e:
        print(f"  [WARN] NASDAQ Screener API ({exchange}) failed: {e}")
        return []


def fetch_all_screener_data() -> dict[str, dict]:
    """
    Fetch price + market cap for all NASDAQ, NYSE, and AMEX stocks via screener API.
    Returns {ticker: {name, price, market_cap_b, exchange}}.
    """
    all_rows: list[dict] = []
    for exch in ("nasdaq", "nyse", "amex"):
        rows = fetch_nasdaq_screener(exch)
        all_rows.extend(rows)
        print(f"  {exch.upper():<8}: {len(rows)} symbols fetched")
        time.sleep(0.5)

    # Deduplicate — keep first occurrence (NASDAQ priority over NYSE for dual-listed)
    seen: dict[str, dict] = {}
    for row in all_rows:
        if row["ticker"] not in seen:
            seen[row["ticker"]] = row
    return seen


# ─── Index membership fetchers ─────────────────────────────────────────────────

def fetch_sp500() -> set[str]:
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        df = pd.read_html(io.StringIO(r.text))[0]
        tickers = set(df["Symbol"].str.replace(".", "-", regex=False).tolist())
        print(f"  S&P 500      : {len(tickers)} members")
        return tickers
    except Exception as e:
        print(f"  [WARN] S&P 500 fetch failed: {e}")
        return set()


def fetch_nasdaq100() -> set[str]:
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        for t in tables:
            if "Ticker" in t.columns:
                tickers = set(t["Ticker"].dropna().tolist())
                print(f"  Nasdaq-100   : {len(tickers)} members")
                return tickers
    except Exception as e:
        print(f"  [WARN] Nasdaq-100 failed: {e}")
    return set()


def fetch_russell2000() -> set[str]:
    try:
        url = (
            "https://www.ishares.com/us/products/239710/IWM/"
            "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
        )
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text), skiprows=9, on_bad_lines="skip")
        col = next(
            (c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()),
            None
        )
        if col:
            tickers = set(
                t.strip() for t in df[col].dropna().astype(str)
                if t.strip() and t.strip() != "-" and len(t.strip()) <= 5
            )
            print(f"  Russell 2000 : {len(tickers)} members")
            return tickers
    except Exception as e:
        print(f"  [WARN] Russell 2000 failed: {e}")
    return set()


# ─── Phase 2: yfinance sector/industry enrichment ─────────────────────────────

def enrich_one(ticker: str) -> dict:
    """Fetch sector + industry for a single ticker. Returns {} on failure."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "sector":   info.get("sector",   "") or "",
            "industry": info.get("industry", "") or "",
        }
    except Exception:
        return {}


def enrich_sector_industry(
    tickers: list[str],
    workers: int = 10,
    delay: float = 0.2,
) -> dict[str, dict]:
    """
    Parallel-fetch sector + industry for a list of tickers.
    Lower worker count (10) is more reliable than 25+ for yfinance.
    """
    results: dict[str, dict] = {}

    def _fetch(t: str) -> tuple[str, dict]:
        data = enrich_one(t)
        time.sleep(delay)
        return t, data

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, t): t for t in tickers}
        for fut in tqdm(as_completed(futures), total=len(futures),
                        unit="ticker", ncols=80, desc="  enriching"):
            try:
                ticker, data = fut.result()
                results[ticker] = data
            except Exception:
                pass

    return results


# ─── Classification ────────────────────────────────────────────────────────────

def classify(cap_b: float | None, min_cap_b: float) -> str:
    if cap_b is None:
        return "UNKNOWN"
    if cap_b < min_cap_b:
        return "BELOW_FILTER"
    if cap_b >= MARKET_LEADER_B:
        return "MARKET_LEADER"
    if cap_b >= MID_LARGE_MIN_B:
        return "MID_LARGE_CAP"
    return "SMALL_CAP"


# ─── Export helpers ────────────────────────────────────────────────────────────

def export_xlsx(df: pd.DataFrame, path: Path) -> None:
    export_cols = [
        "ticker", "name", "price", "market_cap_b", "market_cap_display",
        "category", "exchange", "indices",
        "in_sp500", "in_nasdaq100", "in_russell2000",
        "sector", "industry",
    ]
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df[export_cols].to_excel(writer, sheet_name="All Stocks", index=False)

            for cat, label in CAT_LABELS.items():
                sub = df[df["category"] == cat][export_cols]
                if len(sub):
                    sub.to_excel(writer, sheet_name=label[:31], index=False)

            # Summary sheet
            summary = []
            for cat, label in CAT_LABELS.items():
                sub = df[df["category"] == cat]
                summary.append({
                    "Category":          label,
                    "Count":             len(sub),
                    "Avg Cap ($B)":      round(sub["market_cap_b"].mean(), 2) if len(sub) else 0,
                    "Max Cap ($B)":      round(sub["market_cap_b"].max(), 2) if len(sub) else 0,
                    "Min Cap ($B)":      round(sub["market_cap_b"].min(), 2) if len(sub) else 0,
                    "In S&P 500":        int(sub["in_sp500"].sum()),
                    "In Nasdaq-100":     int(sub["in_nasdaq100"].sum()),
                    "In Russell 2000":   int(sub["in_russell2000"].sum()),
                })
            pd.DataFrame(summary).to_excel(writer, sheet_name="Summary", index=False)

            # Top sectors sheet
            top_sec = (
                df[df["sector"] != ""]
                .groupby(["sector", "category"])["ticker"]
                .count()
                .unstack(fill_value=0)
                .reset_index()
            )
            top_sec["total"] = top_sec.drop("sector", axis=1).sum(axis=1)
            top_sec.sort_values("total", ascending=False).to_excel(
                writer, sheet_name="Top Sectors", index=False
            )

        print(f"XLSX : {path}  ({len(df)} rows, 5 sheets)")
    except ImportError:
        print("[WARN] openpyxl not installed. Run: pip install openpyxl")
    except Exception as e:
        print(f"[WARN] XLSX export failed: {e}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Build full stock universe CSV/XLSX")
    ap.add_argument("--min-cap",    type=float, default=100.0,
                    help="Min market cap in $M (default: 100)")
    ap.add_argument("--workers",    type=int,   default=10,
                    help="Threads for sector/industry enrichment (default: 10)")
    ap.add_argument("--no-russell", action="store_true",
                    help="Skip Russell 2000")
    ap.add_argument("--no-enrich",  action="store_true",
                    help="Skip sector/industry enrichment (fastest, no yfinance calls)")
    ap.add_argument("--out-dir",    default="output",
                    help="Output directory (default: output)")
    args = ap.parse_args()

    min_cap_b = args.min_cap / 1000.0
    out_dir   = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today     = datetime.date.today().strftime("%Y%m%d")

    print("\n========================================")
    print("  Stock Universe Builder")
    print(f"  Filter  : market cap >= ${args.min_cap:.0f}M")
    print(f"  Workers : {args.workers}")
    print(f"  Enrich  : {'NO (--no-enrich)' if args.no_enrich else 'YES (sector/industry)'}")
    print("========================================\n")

    # ── Phase 1a: Index membership ────────────────────────────────────────────
    print("Fetching index memberships...")
    sp500_set     = fetch_sp500()
    nasdaq100_set = fetch_nasdaq100()
    russell_set   = set() if args.no_russell else fetch_russell2000()

    # ── Phase 1b: Bulk market cap + price from NASDAQ Screener API ────────────
    print("\nFetching bulk market cap + price (NASDAQ Screener API)...")
    screener_data = fetch_all_screener_data()
    print(f"  Total unique symbols: {len(screener_data)}")

    # Also include any SP500/Russell tickers not in screener data
    all_tickers = set(screener_data.keys()) | sp500_set | nasdaq100_set | russell_set

    # ── Filter: market cap >= min_cap ─────────────────────────────────────────
    print(f"\nFiltering to market cap >= ${args.min_cap:.0f}M...")
    qualified: list[dict] = []
    no_cap_data = 0

    for ticker in sorted(all_tickers):
        sd      = screener_data.get(ticker, {})
        cap_b   = sd.get("market_cap_b")
        cat     = classify(cap_b, min_cap_b)

        if cat in ("UNKNOWN", "BELOW_FILTER"):
            if cap_b is None:
                no_cap_data += 1
            continue

        # Index/exchange membership
        indices = []
        if ticker in sp500_set:       indices.append("S&P500")
        if ticker in nasdaq100_set:   indices.append("NDX100")
        if ticker in russell_set:     indices.append("RUT2K")

        exchange = sd.get("exchange", "")
        if not exchange:
            # Infer from index membership
            exchange = "NASDAQ" if ticker in nasdaq100_set else ""

        qualified.append({
            "ticker":         ticker,
            "name":           sd.get("name", ""),
            "price":          sd.get("price"),
            "market_cap_b":   round(cap_b, 4),
            "category":       cat,
            "exchange":       exchange,
            "indices":        ",".join(indices) if indices else exchange,
            "in_sp500":       ticker in sp500_set,
            "in_nasdaq100":   ticker in nasdaq100_set,
            "in_russell2000": ticker in russell_set,
            "sector":         "",
            "industry":       "",
        })

    print(f"  Qualified  : {len(qualified)}")
    print(f"  No cap data: {no_cap_data} (excluded)")

    # ── Phase 2: Enrich with sector/industry via yfinance ─────────────────────
    if not args.no_enrich and qualified:
        print(f"\nEnriching {len(qualified)} stocks with sector/industry "
              f"({args.workers} workers)...")
        tickers_to_enrich = [r["ticker"] for r in qualified]
        enriched = enrich_sector_industry(tickers_to_enrich, workers=args.workers)

        for row in qualified:
            e = enriched.get(row["ticker"], {})
            row["sector"]   = e.get("sector",   "")
            row["industry"] = e.get("industry", "")

    # ── Build DataFrame ────────────────────────────────────────────────────────
    df = pd.DataFrame(qualified)
    df.sort_values(
        ["category", "market_cap_b"],
        ascending=[True, False],
        inplace=True
    )
    df.reset_index(drop=True, inplace=True)
    df["market_cap_display"] = df["market_cap_b"].apply(
        lambda x: f"${x:.2f}B" if pd.notna(x) else ""
    )

    # ── Export CSV ────────────────────────────────────────────────────────────
    csv_path = out_dir / f"universe_{today}.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nCSV  : {csv_path}  ({len(df)} rows)")

    # ── Export XLSX ───────────────────────────────────────────────────────────
    xlsx_path = out_dir / f"universe_{today}.xlsx"
    export_xlsx(df, xlsx_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n========================================")
    print("  RESULTS SUMMARY")
    print("========================================")
    for cat, label in CAT_LABELS.items():
        sub = df[df["category"] == cat]
        if len(sub) == 0:
            continue
        print(f"\n  {label}")
        print(f"    Stocks     : {len(sub)}")
        print(f"    Cap range  : ${sub['market_cap_b'].min():.1f}B - "
              f"${sub['market_cap_b'].max():.1f}B")
        print(f"    In S&P 500 : {int(sub['in_sp500'].sum())}")
        print(f"    In NDX 100 : {int(sub['in_nasdaq100'].sum())}")
        print(f"    In RUT 2000: {int(sub['in_russell2000'].sum())}")
        if not args.no_enrich:
            top = (sub[sub["sector"] != ""]
                   .groupby("sector")["ticker"].count()
                   .nlargest(5))
            if len(top):
                secs = "  |  ".join(f"{s}: {n}" for s, n in top.items())
                print(f"    Top sectors: {secs}")

    print(f"\n  TOTAL QUALIFIED: {len(df)}")
    print(f"  CSV  : {csv_path}")
    print(f"  XLSX : {xlsx_path}")
    print("========================================\n")


if __name__ == "__main__":
    main()
