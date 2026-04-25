---
name: build-universe
description: Runs the full stock universe builder across NYSE, S&P 500, NASDAQ, and Russell 2000. Exports all stocks with market cap > $100M to CSV + XLSX, categorised as Market Leaders (>$100B), Mid-Large Cap ($10B-$100B), and Small Cap ($100M-$10B). Use when you need a fresh universe for screening or analysis.
---

Run the stock universe builder from `parabolic-signal-lab/build_universe_csv.py`.

Parse $ARGUMENTS for optional overrides:
- `--min-cap N`   : minimum market cap in $M (default: 100)
- `--workers N`   : parallel threads (default: 25)
- `--no-russell`  : skip Russell 2000 for a faster run
- `--out-dir DIR` : output directory (default: output)

If no arguments given, use the defaults.

**Steps:**

1. Confirm the script exists:
```bash
ls /c/Users/kellw/OneDrive/DEV-ParabolicShort/parabolic-signal-lab/build_universe_csv.py
```

2. Run the builder (background — takes 15-25 min for full universe):
```bash
cd /c/Users/kellw/OneDrive/DEV-ParabolicShort/parabolic-signal-lab && python build_universe_csv.py --workers 25 --min-cap 100 --out-dir output
```

If `--no-russell` was in $ARGUMENTS, append `--no-russell` to the command.
If `--min-cap` was specified, use that value instead of 100.
If `--workers` was specified, use that value.

3. When the command finishes, check what was produced:
```bash
ls -lh /c/Users/kellw/OneDrive/DEV-ParabolicShort/parabolic-signal-lab/output/universe_*.csv /c/Users/kellw/OneDrive/DEV-ParabolicShort/parabolic-signal-lab/output/universe_*.xlsx 2>/dev/null
```

4. Report back:
- How many stocks qualified (from the summary output)
- Breakdown by category (Market Leaders / Mid-Large / Small Cap)
- File paths for the CSV and XLSX
- Remind the user they can now run `/screener` or `/financial-analyst-master` on any ticker from the list
