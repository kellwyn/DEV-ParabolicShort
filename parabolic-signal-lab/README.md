# Parabolic Short — Signal Lab

Screens NASDAQ, NYSE, and S&P 500 for parabolic exhaustion setups per the
Marios Stamatoudis playbook. Three tools chain into a pipeline:

```
screener.py  →  backtest.py  →  enricher.py  →  /financial-analyst-master
(yfinance)      (yfinance)       (Octagon MCP)   (Claude Code skill)
```

---

## Setup

```cmd
pip install -r requirements.txt
```

Set your OctagonAI key in `.env` at the repo root:
```
OCTAGON_API_KEY=your_key_here
```

---

## Run the full pipeline (one command)

**Windows:**
```cmd
cd parabolic-signal-lab

:: S&P 500 (default, ~5-10 min)
run_pipeline.bat

:: Full NASDAQ + NYSE + S&P 500 (~45-60 min)
run_pipeline.bat all

:: Specify entry type
run_pipeline.bat sp500 VWAP_LOSS
```

**Mac / Linux:**
```bash
cd parabolic-signal-lab
chmod +x run_pipeline.sh

./run_pipeline.sh             # S&P 500
./run_pipeline.sh all         # all exchanges
./run_pipeline.sh all VWAP_REJECTION
```

---

## Step 1 — Screen

Scans for stocks meeting Filters 1 and 2 from the playbook:
- **Filter 1**: Price >= threshold% move from last 20-day MA base touch
- **Filter 2**: Aggressive structure (candle range acceleration, low pullback count)

Stock categories and thresholds:

| Category | Market Cap | Move Threshold | Expected Drop |
|---|---|---|---|
| MARKET_LEADER | > $10B | >= 50% | 10-20% |
| THEMATIC | $2-10B | >= 100% | 20-40% |
| IPO_SMALL_CAP | < $2B | >= 200% | 40%+ |

```cmd
:: S&P 500 only (fast)
python screener.py --universe sp500

:: Full NASDAQ
python screener.py --universe nasdaq

:: Full NYSE
python screener.py --universe nyse

:: All exchanges
python screener.py --universe all

:: Thematic stocks only
python screener.py --universe all --category THEMATIC

:: Specific tickers
python screener.py --tickers NVDA SMCI MSTR PLTR

:: Wider results (skip structure filter)
python screener.py --universe sp500 --no-accel-filter

:: Custom output directory
python screener.py --universe sp500 --out-dir my_output
```

**Output:** `output/parabolic_candidates_YYYYMMDD.csv` + `watchlist_YYYYMMDD.txt`

---

## Step 2 — Backtest

Uses yfinance 60-min intraday data (up to 730 days) to simulate Filter 3
(exhaustion day detection) and full entry/exit logic for each qualifying setup.

Three entry types from the playbook (safest to earliest):

| Entry | Condition | Risk |
|---|---|---|
| VWAP_REJECTION | Bar opened below VWAP, tagged it, closed back below | Lowest |
| VWAP_LOSS | Structure break + first close below VWAP together | Medium |
| BREAK_STRUCTURE | First lower high + lower low while below VWAP | Highest |

```cmd
:: Backtest screener output with default entry (VWAP_REJECTION)
python backtest.py --candidates output/parabolic_candidates_YYYYMMDD.csv

:: Specific entry type
python backtest.py --candidates output/parabolic_candidates_YYYYMMDD.csv --entry VWAP_LOSS

:: Compare all 3 entry types side by side
python backtest.py --candidates output/parabolic_candidates_YYYYMMDD.csv --compare-entries

:: Backtest specific tickers directly
python backtest.py --tickers SMCI NVDA MSTR --entry VWAP_REJECTION
```

**Output:** `output/backtest_trades_YYYYMMDD.csv` with full trade log

---

## Step 3 — OctagonAI Enrichment

Generates ready-to-run `/financial-analyst-master` commands for your top
candidates. Each command triggers a 6-phase institutional research report:
financial statements, earnings call analysis, SEC filings, peer comparison,
and a parabolic short verdict.

```cmd
:: Generate enrichment commands for top 10 candidates
python enricher.py --candidates output/parabolic_candidates_YYYYMMDD.csv --top 10

:: Specific tickers
python enricher.py --tickers SMCI NVDA --top 5
```

**Output:** `output/octagon_commands_YYYYMMDD.md`

Then in Claude Code, paste each command:
```
/financial-analyst-master SMCI small
/financial-analyst-master NVDA large
```

OctagonAI tools used per candidate:
- `stock-price-change` — confirms multi-period % move
- `company-market-cap` — confirms Small/Mid/Large classification
- `earnings-call-insights` — was the run hype or fundamentals driven?
- `earnings-conf-call-sentiment` — management tone at peak
- `sec-8k-analysis` — material events that could trigger reversal
- `analyst-estimates` — price vs. consensus (stretched = reversal likely)
- `financial-health-scores` — Altman Z-Score, Piotroski Score

---

## Universe builder (optional — pre-build for faster runs)

```cmd
python universe.py
```

Fetches all NASDAQ + NYSE tickers, classifies each by market cap, and caches
to `.universe_cache.json` (24-hour TTL). The screener uses this automatically.

---

## File reference

| File | Purpose |
|---|---|
| `universe.py` | Builds and caches the full stock universe |
| `screener.py` | Filters 1 + 2 — finds parabolic setups |
| `backtest.py` | Filter 3 + entry/exit simulation on 60-min data |
| `enricher.py` | Generates OctagonAI enrichment commands |
| `run_pipeline.bat` | Runs all 3 steps in sequence (Windows) |
| `run_pipeline.sh` | Runs all 3 steps in sequence (Mac/Linux) |
| `output/` | All generated CSVs and watchlists |
