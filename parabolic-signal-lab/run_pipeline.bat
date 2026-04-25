@echo off
:: Parabolic Short Strategy - Full Pipeline Runner
:: Usage: run_pipeline.bat [universe] [entry_type]
::   universe   : sp500 | nasdaq | nyse | all  (default: sp500)
::   entry_type : VWAP_REJECTION | VWAP_LOSS | BREAK_STRUCTURE  (default: VWAP_REJECTION)

SET UNIVERSE=%1
IF "%UNIVERSE%"=="" SET UNIVERSE=sp500

SET ENTRY=%2
IF "%ENTRY%"=="" SET ENTRY=VWAP_REJECTION

SET TODAY=%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%
SET OUTDIR=output
SET CSV=%OUTDIR%\parabolic_candidates_%TODAY%.csv

echo.
echo ============================================================
echo  PARABOLIC SHORT STRATEGY PIPELINE
echo  Universe  : %UNIVERSE%
echo  Entry type: %ENTRY%
echo  Output dir: %OUTDIR%
echo ============================================================
echo.

:: Step 1 - Screen
echo [STEP 1/3] Screening %UNIVERSE% for parabolic setups...
python screener.py --universe %UNIVERSE% --out-dir %OUTDIR%
IF ERRORLEVEL 1 (
    echo [ERROR] Screener failed. Exiting.
    exit /b 1
)

:: Step 2 - Backtest
echo.
echo [STEP 2/3] Backtesting qualifying setups (60-min intraday)...
python backtest.py --candidates %CSV% --entry %ENTRY% --out-dir %OUTDIR%
IF ERRORLEVEL 1 (
    echo [WARN] Backtest failed or no candidates found.
)

:: Step 3 - Enrichment commands
echo.
echo [STEP 3/3] Generating OctagonAI enrichment commands...
python enricher.py --candidates %CSV% --top 10 --out-dir %OUTDIR%

echo.
echo ============================================================
echo  PIPELINE COMPLETE
echo  Results in: %OUTDIR%\
echo  Run OctagonAI enrichment: paste commands from octagon_commands_%TODAY%.md
echo  into Claude Code and run /financial-analyst-master for each ticker
echo ============================================================
