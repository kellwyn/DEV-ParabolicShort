#!/usr/bin/env bash
# Parabolic Short Strategy - Full Pipeline Runner
# Usage: ./run_pipeline.sh [universe] [entry_type]
#   universe   : sp500 | nasdaq | nyse | all  (default: sp500)
#   entry_type : VWAP_REJECTION | VWAP_LOSS | BREAK_STRUCTURE  (default: VWAP_REJECTION)

set -euo pipefail

UNIVERSE=${1:-sp500}
ENTRY=${2:-VWAP_REJECTION}
TODAY=$(date +%Y%m%d)
OUTDIR="output"
CSV="${OUTDIR}/parabolic_candidates_${TODAY}.csv"

echo ""
echo "============================================================"
echo " PARABOLIC SHORT STRATEGY PIPELINE"
echo " Universe  : ${UNIVERSE}"
echo " Entry type: ${ENTRY}"
echo " Output dir: ${OUTDIR}"
echo "============================================================"
echo ""

# Step 1 - Screen
echo "[STEP 1/3] Screening ${UNIVERSE} for parabolic setups..."
python screener.py --universe "${UNIVERSE}" --out-dir "${OUTDIR}"

# Step 2 - Backtest
echo ""
echo "[STEP 2/3] Backtesting qualifying setups (60-min intraday)..."
python backtest.py --candidates "${CSV}" --entry "${ENTRY}" --out-dir "${OUTDIR}" || \
    echo "[WARN] Backtest failed or no candidates found."

# Step 3 - Enrichment commands
echo ""
echo "[STEP 3/3] Generating OctagonAI enrichment commands..."
python enricher.py --candidates "${CSV}" --top 10 --out-dir "${OUTDIR}"

echo ""
echo "============================================================"
echo " PIPELINE COMPLETE"
echo " Results in: ${OUTDIR}/"
echo " Run OctagonAI enrichment: paste commands from"
echo " octagon_commands_${TODAY}.md into Claude Code"
echo "============================================================"
