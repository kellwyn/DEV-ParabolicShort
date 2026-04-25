"""
Stock Universe Builder - Full Export to CSV / XLSX
====================================================
Fetches every listed stock from NYSE, S&P 500, NASDAQ, and Russell 2000,
enriches with yfinance data, filters to market cap > $100M, and exports.

Categories (from playbook, adjusted for full universe):
  MARKET_LEADER   market cap > $100B   (mega/large-cap — threshold >=50%)
  MID_LARGE_CAP   $10B  - $100B        (thematic/sector runners — >=100%)
  SMALL_CAP       $100M - $10B         (small-cap / IPO — >=200%)

Output columns:
  ticker, name, price, market_cap_b, category, exchange,
  in_sp500, in_nasdaq100, in_russell2000, sector, industry

Export:
  universe_YYYYMMDD.csv           (all stocks, single file)
  universe_YYYYMMDD.xlsx          (multi-sheet: All + per category + Summary)
  If total > 500k rows split into parts automatically.

Usage:
  python build_universe_csv.py                  # all 4 sources, default settings
  python build_universe_csv.py --workers 30     # more parallel threads (faster)
  python build_universe_csv.py --min-cap 500    # raise filter to $500M+
  python build_universe_csv.py --no-russell     # skip Russell 2000 (faster)
  python build_universe_csv.py --out-dir data   # custom output directory
"""

import argparse
import csv
import datetime
import io
import logging
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from tqdm import tqdm

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

# ─── Category boundaries ($B) ──────────────────────────────────────────────────
MARKET_LEADER_B  = 100.0   # > $100B
MID_LARGE_MIN_B  = 10.0    # $10B - $100B
SMALL_CAP_MIN_B  = 0.1     # $100M - $10B (filter floor)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ─── Index / Exchange symbol fetchers ──────────────────────────────────────────

def fetch_sp500() -> set[str]:
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        df = pd.read_html(io.StringIO(r.text))[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"  S&P 500       : {len(tickers)} tickers")
        return set(tickers)
    except Exception as e:
        print(f"  [WARN] S&P 500 failed: {e}")
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
                tickers = t["Ticker"].dropna().tolist()
                print(f"  Nasdaq-100    : {len(tickers)} tickers")
                return set(tickers)
    except Exception as e:
        print(f"  [WARN] Nasdaq-100 failed: {e}")
    return set()


def fetch_russell2000() -> set[str]:
    """IShares IWM holdings = Russell 2000 components."""
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
            tickers = [
                t.strip() for t in df[col].dropna().astype(str).tolist()
                if t.strip() and t.strip() != "-" and len(t.strip()) <= 5
            ]
            print(f"  Russell 2000  : {len(tickers)} tickers")
            return set(tickers)
    except Exception as e:
        print(f"  [WARN] Russell 2000 failed: {e}")
    return set()


def fetch_exchange_listings() -> dict[str, dict]:
    """
    Returns {ticker: {exchange, name}} for all NASDAQ and NYSE listed stocks.
    Source: NASDAQ Trader FTP (official, free).
    """
    result: dict[str, dict] = {}
    sources = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",  "NASDAQ"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",   "NYSE"),
    ]
    for url, exch in sources:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            count = 0
            for line in r.text.splitlines()[1:]:
                if "|" not in line or line.startswith("File Creation"):
                    continue
                parts = line.split("|")
                ticker = parts[0].strip()
                if not ticker or any(c in ticker for c in ["$", "+", " "]) or len(ticker) > 5:
                    continue
                name = parts[1].strip() if len(parts) > 1 else ""
                result[ticker] = {"exchange": exch, "name": name}
                count += 1
            print(f"  {exch:<14}: {count} tickers")
        except Exception as e:
            print(f"  [WARN] {exch} FTP failed: {e}")
    return result


# ─── yfinance enrichment ───────────────────────────────────────────────────────

def enrich_batch(tickers: list[str]) -> list[dict]:
    """
    Fetch price, market cap, sector, industry for a batch of tickers.
    Uses fast_info for speed + info for sector/industry.
    Returns one dict per ticker (partial data on failure).
    """
    results = []
    for ticker in tickers:
        row: dict = {"ticker": ticker, "price": None, "market_cap_b": None,
                     "sector": "", "industry": "", "name_yf": ""}
        try:
            tk = yf.Ticker(ticker)
            fi = tk.fast_info

            price  = getattr(fi, "last_price",  None)
            cap    = getattr(fi, "market_cap",  None)
            row["price"]       = round(float(price), 4) if price else None
            row["market_cap_b"]= round(float(cap) / 1e9, 4) if cap else None

            # Sector + industry from info (cached after fast_info call)
            try:
                info = tk.info
                row["sector"]   = info.get("sector",       "") or ""
                row["industry"] = info.get("industry",     "") or ""
                row["name_yf"]  = info.get("longName",     "") or \
                                  info.get("shortName",    "") or ""
            except Exception:
                pass
        except Exception:
            pass
        results.append(row)
        time.sleep(0.05)  # gentle rate limit per ticker within batch
    return results


# ─── Classification ────────────────────────────────────────────────────────────

def classify(cap_b: float | None) -> str:
    if cap_b is None:
        return "UNKNOWN"
    if cap_b > MARKET_LEADER_B:
        return "MARKET_LEADER"
    if cap_b > MID_LARGE_MIN_B:
        return "MID_LARGE_CAP"
    if cap_b >= SMALL_CAP_MIN_B:
        return "SMALL_CAP"
    return "BELOW_FILTER"


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Build full stock universe CSV/XLSX")
    p.add_argument("--min-cap",    type=float, default=100.0,
                   help="Minimum market cap in $M (default: 100)")
    p.add_argument("--workers",    type=int,   default=20,
                   help="Parallel threads for yfinance fetching (default: 20)")
    p.add_argument("--batch-size", type=int,   default=5,
                   help="Tickers per thread batch (default: 5)")
    p.add_argument("--no-russell", action="store_true",
                   help="Skip Russell 2000 (faster run)")
    p.add_argument("--out-dir",    default="output",
                   help="Output directory")
    args = p.parse_args()

    min_cap_b = args.min_cap / 1000.0  # convert $M to $B
    out_dir   = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today     = datetime.date.today().strftime("%Y%m%d")

    print("\n=== Stock Universe Builder ===")
    print(f"Filter  : market cap >= ${args.min_cap:.0f}M")
    print(f"Workers : {args.workers}  |  batch size: {args.batch_size}")
    print("\nFetching index/exchange memberships...")

    # ── 1. Fetch all symbol sets ───────────────────────────────────────────────
    sp500_set     = fetch_sp500()
    nasdaq100_set = fetch_nasdaq100()
    russell_set   = set() if args.no_russell else fetch_russell2000()
    listings      = fetch_exchange_listings()  # {ticker: {exchange, name}}

    # Build master ticker set (union of all sources)
    all_tickers: set[str] = set(listings.keys()) | sp500_set | nasdaq100_set | russell_set
    print(f"\nTotal unique tickers (all sources): {len(all_tickers)}")

    # ── 2. Build metadata map (index membership + exchange from FTP) ───────────
    meta: dict[str, dict] = {}
    for t in all_tickers:
        listing = listings.get(t, {})
        meta[t] = {
            "ticker":         t,
            "exchange":       listing.get("exchange", ""),
            "name_listing":   listing.get("name",     ""),
            "in_sp500":       t in sp500_set,
            "in_nasdaq100":   t in nasdaq100_set,
            "in_russell2000": t in russell_set,
        }

    # ── 3. Parallel yfinance enrichment ───────────────────────────────────────
    tickers_list = sorted(all_tickers)
    batches = [
        tickers_list[i:i + args.batch_size]
        for i in range(0, len(tickers_list), args.batch_size)
    ]

    print(f"\nEnriching {len(tickers_list)} tickers in {len(batches)} batches "
          f"({args.workers} workers)...")
    print("This takes ~10-20 minutes for a full universe run.\n")

    yf_data: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich_batch, batch): batch for batch in batches}
        for fut in tqdm(as_completed(futures), total=len(futures),
                        unit="batch", ncols=80):
            try:
                rows = fut.result()
                for row in rows:
                    yf_data[row["ticker"]] = row
            except Exception as e:
                pass  # batch failed; missing tickers get None market cap

    # ── 4. Merge and filter ────────────────────────────────────────────────────
    print("\nMerging and filtering...")
    records = []
    skipped_below_filter = 0
    skipped_no_data      = 0

    for ticker in tickers_list:
        m  = meta[ticker]
        yf = yf_data.get(ticker, {})

        cap_b    = yf.get("market_cap_b")
        price    = yf.get("price")
        category = classify(cap_b)

        if category == "BELOW_FILTER":
            skipped_below_filter += 1
            continue
        if category == "UNKNOWN" and cap_b is None:
            skipped_no_data += 1
            continue

        # Use yfinance name if available, else listing name
        name = yf.get("name_yf") or m.get("name_listing") or ""

        # Index membership string
        indices = []
        if m["in_sp500"]:       indices.append("S&P500")
        if m["in_nasdaq100"]:   indices.append("NDX100")
        if m["in_russell2000"]: indices.append("RUT2K")
        if not indices and m["exchange"]:
            indices.append(m["exchange"])

        records.append({
            "ticker":         ticker,
            "name":           name,
            "price":          price,
            "market_cap_b":   cap_b,
            "category":       category,
            "exchange":       m["exchange"],
            "indices":        ",".join(indices),
            "in_sp500":       m["in_sp500"],
            "in_nasdaq100":   m["in_nasdaq100"],
            "in_russell2000": m["in_russell2000"],
            "sector":         yf.get("sector",   ""),
            "industry":       yf.get("industry", ""),
        })

    print(f"  Qualified   : {len(records)}")
    print(f"  Below ${args.min_cap:.0f}M  : {skipped_below_filter}")
    print(f"  No data     : {skipped_no_data}")

    df = pd.DataFrame(records)
    df.sort_values(["category", "market_cap_b"], ascending=[True, False], inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Friendly market cap display
    df["market_cap_display"] = df["market_cap_b"].apply(
        lambda x: f"${x:.2f}B" if x is not None else ""
    )

    # ── 5. Export CSV ──────────────────────────────────────────────────────────
    csv_path = out_dir / f"universe_{today}.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nCSV  : {csv_path}  ({len(df)} rows)")

    # ── 6. Export XLSX (multi-sheet) ──────────────────────────────────────────
    xlsx_path = out_dir / f"universe_{today}.xlsx"
    export_columns = [
        "ticker", "name", "price", "market_cap_b", "market_cap_display",
        "category", "exchange", "indices",
        "in_sp500", "in_nasdaq100", "in_russell2000",
        "sector", "industry"
    ]

    cat_labels = {
        "MARKET_LEADER": "Market Leaders (>$100B)",
        "MID_LARGE_CAP": "Mid-Large Cap ($10B-$100B)",
        "SMALL_CAP":     "Small Cap ($100M-$10B)",
    }

    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            # All stocks sheet
            df[export_columns].to_excel(writer, sheet_name="All Stocks", index=False)

            # Per-category sheets
            for cat, label in cat_labels.items():
                cat_df = df[df["category"] == cat][export_columns]
                if len(cat_df) > 0:
                    sheet_name = label[:31]  # Excel max 31 chars
                    cat_df.to_excel(writer, sheet_name=sheet_name, index=False)

            # Summary sheet
            summary_rows = []
            for cat, label in cat_labels.items():
                sub = df[df["category"] == cat]
                summary_rows.append({
                    "Category":         label,
                    "Count":            len(sub),
                    "Avg Market Cap $B": round(sub["market_cap_b"].mean(), 2) if len(sub) else 0,
                    "Max Market Cap $B": round(sub["market_cap_b"].max(), 2) if len(sub) else 0,
                    "In S&P 500":       sub["in_sp500"].sum(),
                    "In NDX 100":       sub["in_nasdaq100"].sum(),
                    "In Russell 2000":  sub["in_russell2000"].sum(),
                })

            # Top sectors
            top_sectors = (
                df[df["sector"] != ""]
                .groupby("sector")
                .agg(count=("ticker","count"), avg_cap=("market_cap_b","mean"))
                .sort_values("count", ascending=False)
                .head(20)
                .reset_index()
            )

            pd.DataFrame(summary_rows).to_excel(
                writer, sheet_name="Summary", index=False
            )
            top_sectors.to_excel(
                writer, sheet_name="Top Sectors", index=False
            )

        print(f"XLSX : {xlsx_path}  (5 sheets)")

    except ImportError:
        print("[WARN] openpyxl not installed — skipping XLSX. Run: pip install openpyxl")

    # ── 7. Print summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  UNIVERSE SUMMARY")
    print("=" * 60)
    for cat, label in cat_labels.items():
        sub = df[df["category"] == cat]
        print(f"  {label}")
        print(f"    Count    : {len(sub)}")
        if len(sub):
            print(f"    Cap range: ${sub['market_cap_b'].min():.1f}B - ${sub['market_cap_b'].max():.1f}B")
            print(f"    In S&P500: {sub['in_sp500'].sum()}")
            print(f"    Top sectors:")
            top = sub[sub["sector"]!=""].groupby("sector")["ticker"].count().nlargest(5)
            for sec, cnt in top.items():
                print(f"      {sec}: {cnt}")
        print()
    print(f"  TOTAL QUALIFIED: {len(df)}")
    print("=" * 60)
    print(f"\nFiles saved to: {out_dir}/")


if __name__ == "__main__":
    main()
