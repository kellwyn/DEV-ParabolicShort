"""
Universe Builder — Parabolic Short Strategy
============================================
Fetches all listed stocks from NASDAQ, NYSE, and S&P 500.
Classifies each stock into one of three categories from the playbook:

  MARKET_LEADER  Large-cap  > $10B   Parabolic threshold: ≥ 50%
  THEMATIC       Mid-cap   $2–10B    Parabolic threshold: ≥ 100%
  IPO_SMALL_CAP  Small-cap  < $2B    Parabolic threshold: ≥ 200%

Thematic stocks are further tagged by sector to identify hype-driven
runners (AI/Tech, Biotech, Clean Energy, Quantum) which tend to produce
the sharpest parabolic reversals per the playbook.

Data sources (all free, no API key):
  - NASDAQ FTP  : ftp.nasdaqtrader.com  (official exchange listings)
  - Wikipedia   : S&P 500 constituent list
  - yfinance    : market cap + sector enrichment

Usage:
  from universe import Universe
  u = Universe()
  tickers = u.get_all()               # all tickers
  leaders = u.get_by_category("MARKET_LEADER")
  thematic = u.get_thematic_only()    # hot-sector mid-caps
  small    = u.get_by_category("IPO_SMALL_CAP")
"""

import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import requests
import pandas as pd
import yfinance as yf
from tqdm import tqdm

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
CACHE_FILE    = Path(__file__).parent / ".universe_cache.json"
CACHE_TTL_HRS = 24

# Market cap boundaries (USD billions)
LARGE_CAP_B   = 10.0
MID_CAP_B     = 2.0

# Sectors that produce hype/theme-driven parabolic moves
THEMATIC_SECTORS = {
    "Technology", "Health Care", "Healthcare",
    "Consumer Discretionary", "Communication Services",
    "Industrials",  # for defence/aerospace themes
}

THEMATIC_KEYWORDS = [
    "ai", "artificial intelligence", "quantum", "semiconductor",
    "biotech", "genomics", "gene", "crispr", "immunotherapy",
    "electric vehicle", " ev ", "battery", "autonomous",
    "crypto", "blockchain", "digital asset",
    "space", "drone", "robotics", "clean energy", "solar", "fusion",
]

# NASDAQ FTP — official free symbol directories
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL  = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# NASDAQ screener API (backup — broader, includes extra metadata)
NASDAQ_API_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=5000&offset=0&exchange={exchange}"
)

HEADERS = {"User-Agent": "Mozilla/5.0"}


# ─── Universe ─────────────────────────────────────────────────────────────────
class Universe:
    """
    Builds and caches the full tradeable stock universe across
    NASDAQ, NYSE, and S&P 500, classified by cap category.
    """

    def __init__(self, refresh: bool = False, enrich_sample: int = 500):
        """
        Args:
            refresh:       Force re-fetch even if cache is fresh.
            enrich_sample: Number of random stocks to enrich with yfinance
                           market-cap data during a full rebuild (cap data
                           is expensive — we classify the rest by price proxy).
        """
        self._enrich_sample = enrich_sample
        self._stocks: list[dict] = []
        self._load(refresh)

    # ── Public API ─────────────────────────────────────────────────────────────
    def get_all(self, min_price: float = 1.0) -> list[str]:
        return [s["ticker"] for s in self._stocks if s.get("price", 0) >= min_price]

    def get_by_category(self, category: str, min_price: float = 1.0) -> list[str]:
        return [
            s["ticker"] for s in self._stocks
            if s.get("category") == category and s.get("price", 0) >= min_price
        ]

    def get_thematic_only(self, min_price: float = 2.0) -> list[str]:
        """Mid-cap stocks in hot sectors — highest parabolic reversal probability."""
        return [
            s["ticker"] for s in self._stocks
            if s.get("is_thematic") and s.get("price", 0) >= min_price
        ]

    def get_by_exchange(self, exchange: str) -> list[str]:
        ex = exchange.upper()
        return [s["ticker"] for s in self._stocks if s.get("exchange") == ex]

    def get_sp500(self) -> list[str]:
        return [s["ticker"] for s in self._stocks if s.get("in_sp500")]

    def threshold_for(self, ticker: str) -> float:
        """Return the move threshold % for the given ticker's cap category."""
        for s in self._stocks:
            if s["ticker"] == ticker:
                cat = s.get("category", "IPO_SMALL_CAP")
                return {"MARKET_LEADER": 50.0, "THEMATIC": 100.0}.get(cat, 200.0)
        return 200.0

    def meta(self, ticker: str) -> dict:
        for s in self._stocks:
            if s["ticker"] == ticker:
                return s
        return {}

    def summary(self) -> str:
        cats   = {}
        exch   = {}
        for s in self._stocks:
            cats[s.get("category", "?")] = cats.get(s.get("category", "?"), 0) + 1
            exch[s.get("exchange", "?")] = exch.get(s.get("exchange", "?"), 0) + 1
        lines = [
            f"Total    : {len(self._stocks)}",
            f"By cat   : {cats}",
            f"By exch  : {exch}",
            f"S&P 500  : {sum(1 for s in self._stocks if s.get('in_sp500'))}",
            f"Thematic : {sum(1 for s in self._stocks if s.get('is_thematic'))}",
        ]
        return "\n".join(lines)

    # ── Internal ───────────────────────────────────────────────────────────────
    def _load(self, refresh: bool) -> None:
        if not refresh and self._cache_valid():
            logger.info("Universe: loaded from cache (%s)", CACHE_FILE)
            data = json.loads(CACHE_FILE.read_text())
            self._stocks = data["stocks"]
            return
        logger.info("Universe: rebuilding from source...")
        self._build()
        self._save()

    def _cache_valid(self) -> bool:
        if not CACHE_FILE.exists():
            return False
        try:
            data  = json.loads(CACHE_FILE.read_text())
            ts    = datetime.fromisoformat(data.get("built_at", "2000-01-01"))
            return datetime.now() - ts < timedelta(hours=CACHE_TTL_HRS)
        except Exception:
            return False

    def _save(self) -> None:
        CACHE_FILE.write_text(json.dumps({
            "built_at": datetime.now().isoformat(),
            "stocks":   self._stocks,
        }, indent=2))
        logger.info("Universe: saved %d stocks to cache", len(self._stocks))

    def _build(self) -> None:
        sp500 = set(self._fetch_sp500())
        raw   = self._fetch_listings()

        # Deduplicate
        seen: set[str] = set()
        unique: list[dict] = []
        for r in raw:
            t = r["ticker"]
            if t not in seen:
                seen.add(t)
                unique.append(r)

        # Mark S&P 500 membership
        for s in unique:
            s["in_sp500"] = s["ticker"] in sp500

        # Classify cap category
        self._classify(unique)
        self._stocks = unique
        print(f"Universe built: {len(self._stocks)} stocks")
        print(self.summary())

    def _fetch_sp500(self) -> list[str]:
        try:
            df = pd.read_html(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            )[0]
            return df["Symbol"].str.replace(".", "-", regex=False).tolist()
        except Exception as e:
            logger.warning("S&P 500 fetch failed: %s", e)
            return []

    def _fetch_listings(self) -> list[dict]:
        stocks: list[dict] = []

        # ── NASDAQ FTP listings (most reliable) ─────────────────────────────
        try:
            stocks.extend(self._parse_nasdaq_ftp(NASDAQ_LISTED_URL, "NASDAQ"))
            stocks.extend(self._parse_nasdaq_ftp(OTHER_LISTED_URL,  "NYSE"))
            if stocks:
                logger.info("FTP listings: %d symbols", len(stocks))
                return stocks
        except Exception as e:
            logger.warning("NASDAQ FTP failed (%s), falling back to API", e)

        # ── NASDAQ screener API (fallback) ───────────────────────────────────
        for exchange in ("nasdaq", "nyse", "amex"):
            try:
                stocks.extend(self._fetch_nasdaq_api(exchange))
                time.sleep(0.5)
            except Exception as e:
                logger.warning("NASDAQ API %s failed: %s", exchange, e)

        return stocks

    def _parse_nasdaq_ftp(self, url: str, exchange: str) -> list[dict]:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        lines = r.text.splitlines()
        # Skip header and trailing file creation line
        results = []
        for line in lines[1:]:
            if "|" not in line or line.startswith("File Creation"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            ticker = parts[0].strip()
            name   = parts[1].strip() if len(parts) > 1 else ""
            # Skip test symbols, warrants, rights, units
            if not ticker or any(c in ticker for c in ["$", "+", ".", " "]):
                continue
            if len(ticker) > 5:
                continue
            results.append({
                "ticker":     ticker,
                "name":       name,
                "exchange":   exchange,
                "category":   None,
                "is_thematic": False,
                "price":      0.0,
                "mkt_cap_b":  None,
            })
        return results

    def _fetch_nasdaq_api(self, exchange: str) -> list[dict]:
        url = NASDAQ_API_URL.format(exchange=exchange)
        r   = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("table", {}).get("rows", [])
        results = []
        for row in rows:
            ticker = row.get("symbol", "").strip()
            if not ticker or len(ticker) > 5:
                continue
            try:
                price = float(str(row.get("lastsale", "0")).replace("$", "").replace(",", ""))
            except ValueError:
                price = 0.0
            results.append({
                "ticker":     ticker,
                "name":       row.get("name", ""),
                "exchange":   exchange.upper(),
                "category":   None,
                "is_thematic": False,
                "price":      price,
                "mkt_cap_b":  None,
            })
        return results

    def _classify(self, stocks: list[dict]) -> None:
        """
        Classify each stock into MARKET_LEADER / THEMATIC / IPO_SMALL_CAP
        using yfinance market cap and sector data.
        Fetches in batches to avoid rate limiting.
        """
        # Sample a subset for enrichment — full universe is too large to enrich all at once
        sample_size = min(self._enrich_sample, len(stocks))
        # Prioritise S&P 500 members first for enrichment
        sp500 = [s for s in stocks if s.get("in_sp500")]
        others = [s for s in stocks if not s.get("in_sp500")]
        to_enrich = sp500 + others[:max(0, sample_size - len(sp500))]

        print(f"Enriching {len(to_enrich)} stocks with yfinance market cap data...")
        enriched: dict[str, dict] = {}

        # Batch download using yfinance
        batch_size = 50
        for i in tqdm(range(0, len(to_enrich), batch_size), desc="yf enrich"):
            batch   = [s["ticker"] for s in to_enrich[i:i + batch_size]]
            tickers = yf.Tickers(" ".join(batch))
            for ticker in batch:
                try:
                    info  = tickers.tickers[ticker].fast_info
                    cap_b = getattr(info, "market_cap", None)
                    cap_b = cap_b / 1e9 if cap_b else None
                    sec   = getattr(tickers.tickers[ticker].info, "sector", "") or ""
                    name  = getattr(tickers.tickers[ticker].info, "longName", "") or ""
                    enriched[ticker] = {"cap_b": cap_b, "sector": sec, "name": name}
                except Exception:
                    enriched[ticker] = {"cap_b": None, "sector": "", "name": ""}
            time.sleep(0.2)

        # Apply classifications
        for s in stocks:
            meta     = enriched.get(s["ticker"], {})
            cap_b    = meta.get("cap_b")
            sector   = meta.get("sector", "")
            name_low = (meta.get("name", "") or s.get("name", "")).lower()

            # Cap category
            if cap_b is not None:
                s["mkt_cap_b"] = round(cap_b, 2)
                if cap_b >= LARGE_CAP_B:
                    s["category"] = "MARKET_LEADER"
                elif cap_b >= MID_CAP_B:
                    s["category"] = "THEMATIC"
                else:
                    s["category"] = "IPO_SMALL_CAP"
            else:
                # Fallback: use price as rough proxy (not ideal, better than nothing)
                p = s.get("price", 0)
                s["category"] = (
                    "MARKET_LEADER" if p > 100 else
                    "THEMATIC"      if p > 10  else
                    "IPO_SMALL_CAP"
                )

            # Thematic flag — hot sector AND/OR name contains thematic keywords
            in_hot_sector = sector in THEMATIC_SECTORS
            has_keyword   = any(kw in name_low for kw in THEMATIC_KEYWORDS)
            s["is_thematic"] = in_hot_sector or has_keyword
            if sector:
                s["sector"] = sector


def build_universe(refresh: bool = False) -> Universe:
    """Convenience function — builds and returns the universe."""
    return Universe(refresh=refresh)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    u = Universe(refresh=True)
    print("\n" + u.summary())
    print("\nMarket Leaders (sample):", u.get_by_category("MARKET_LEADER")[:10])
    print("Thematic (sample):",       u.get_thematic_only()[:10])
    print("IPO/Small (sample):",      u.get_by_category("IPO_SMALL_CAP")[:10])
