@../Dev-RZY/AGENT.md
@../Dev-RZY/SKILLS.md

<!-- DEV-ParabolicShort — strategy-specific overrides -->

## Strategy

**Parabolic Short — Marios Stamatoudis method** — stocks, SHORT only, day trading.  
Three required components must ALL be present: parabolic move from 20MA base, aggressive structure (steep/large candles), exhaustion day (price loses VWAP + lower highs).  
See `parabolic-mq5-ea/ParabolicShortEA.mq5` header comment for full rules.

## Critical Rules

- **SHORT only** — no long entries, ever
- **Day trading only — NO overnight holds** — EOD close is hardcoded in EA
- MANUAL_CONFIRM=true always — human must confirm entry type before execution
- Cap category determines threshold: Small-cap ≥200%, Mid-cap ≥100%, Large-cap ≥50% from last 20MA base
- Max 2 trade attempts per exhaustion window; if both fail, skip the setup
- Entry hierarchy (safest → earliest): `VWAP_REJECTION` → `VWAP_LOSS` → `BREAK_STRUCTURE`
- 2-day window only: setup must trigger on exhaustion day or the next day; after that, skip

## EA State Machine

```
STATE_IDLE → STATE_PARABOLIC → STATE_EXHAUSTION → STATE_IN_TRADE
```

Each state has distinct detection logic. Do not skip states in the implementation.

## Repo Structure

```
DEV-ParabolicShort/
├── parabolic-mq5-ea/
│   ├── ParabolicShortEA.mq5        main EA
│   └── ScanParabolicSymbols.mq5    multi-symbol scanner utility
├── parabolic-python-bot/
│   └── bot/
├── parabolic-signal-lab/
│   └── screener.py
└── parabolic-tradingview/
    ├── ParabolicShortScreener.pine  TradingView screener (reference only)
    └── ParabolicShortStrategy.pine  TradingView strategy (reference only)
```

## Key Differences from Standard Pattern

- Has a TradingView component — Pine scripts are reference/research only; execution is MT5
- Signal lab uses `screener.py` not `signal_lab.py` — scans for parabolic move conditions historically
- VWAP is intraday, D1 reset — must be calculated from intraday bars, not from a daily value
- Risk-free add: if price returns to VWAP a second time after BE, add to position (no extra risk)

## Verification

| Change | Verify with |
|---|---|
| EA state machine | Step through each state transition on a historical exhaustion chart |
| VWAP calc | Confirm D1 reset on bar 0 of each day |
| Screener | Run on ≥6M of data; check threshold detection matches manual chart review |

## Subagents

| Task | Command |
|---|---|
| Review code | `use the code-reviewer subagent` |
| Security scan | `use the security-reviewer subagent` |
| Deploy EA | `use the ea-deployer subagent` |
| Commit + push | `use the git-push subagent` |
