---
name: new-strategy
description: Scaffolds a new strategy repo with the standard three-component structure (mq5-ea, python-bot, signal-lab). Run when starting a new trading strategy project.
disable-model-invocation: true
---

Scaffold a new MetaTrader strategy project named $ARGUMENTS.

Follow these steps exactly:

1. **Confirm the strategy name** — derive a slug (e.g. "SwingBB" → `swingbb`, "80/20 Scalps" → `8020scalps`)

2. **Create repo directory structure**:
```
DEV-<StrategyName>/
├── <slug>-mq5-ea/
├── <slug>-python-bot/
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── connection.py    (copy from SKILLS.md SKILL: PYTHON_BOT_SHELL)
│   │   ├── signals.py       (copy from SKILLS.md SKILL: PYTHON_BOT_SHELL)
│   │   ├── strategy.py      (copy from SKILLS.md SKILL: PYTHON_BOT_SHELL)
│   │   └── trader.py        (copy from marci-python-bot/bot/trader.py — logic is identical)
│   ├── main.py              (copy from SKILLS.md SKILL: PYTHON_BOT_SHELL)
│   └── requirements.txt
├── <slug>-signal-lab/
│   ├── config.py
│   ├── fetcher.py           (copy from marci-signal-lab/fetcher.py — logic is identical)
│   ├── main.py
│   ├── report.py
│   ├── strategy_scan.py
│   └── tp_analysis.py
├── .env.example             (copy from SKILLS.md SKILL: ENV_SETUP)
├── .gitignore               (copy from Dev-RZY/.gitignore)
├── AGENT.md                 (copy from Dev-RZY/AGENT.md)
├── CLAUDE.md                (copy from Dev-RZY/CLAUDE.md — update strategy name)
├── CREDENTIALS.md           (copy from Dev-RZY/CREDENTIALS.md)
└── SKILLS.md                (copy from Dev-RZY/SKILLS.md)
```

3. **Create the EA shell** in `<slug>-mq5-ea/<StrategyName>_v1.00.mq5`:
   - Use the MQL5_EA_SHELL from SKILLS.md
   - Set `#property version "1.00"`
   - Add strategy name and date to file header comment

4. **Update MAGIC_NUMBER** in `.env.example` — use today's date as YYYYMMDD (e.g. `20260423`)

5. **Initialise git**:
```bash
git init
git add .
git commit -m "$(cat <<'EOF'
Initial commit — <StrategyName> v1.00 scaffold

Co-Authored-By: KSE
EOF
)"
```

6. **Report** — list all created files and confirm next step: "Implement strategy rules in `<slug>-mq5-ea/<StrategyName>_v1.00.mq5` starting with Rule 1."
