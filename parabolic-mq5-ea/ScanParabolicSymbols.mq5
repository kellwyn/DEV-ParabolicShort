//+------------------------------------------------------------------+
//|                                      ScanParabolicSymbols.mq5   |
//|           Parabolic Short Strategy — Symbol Scanner Script       |
//|           v1.0                                                   |
//|                                                                  |
//|  What it does:                                                   |
//|   Iterates every symbol your broker exposes, checks the         |
//|   parabolic condition (price ≥ threshold% from last 20MA base   |
//|   touch on D1), then adds qualifying symbols to Market Watch.   |
//|                                                                  |
//|  Limitation:                                                     |
//|   Only discovers symbols your broker already provides.          |
//|   For full US-market screening use the Python screener          |
//|   (parabolic-signal-lab/screener.py) which queries yfinance.    |
//|                                                                  |
//|  Usage:                                                          |
//|   Attach as a Script (not EA) to any chart. Results appear in   |
//|   the Experts tab and qualifying symbols land in Market Watch.  |
//+------------------------------------------------------------------+
#property copyright "Parabolic Short Strategy"
#property version   "1.00"
#property script_show_inputs
#property description "Scans broker symbols for parabolic move setups and adds them to Market Watch"

//─── Inputs ────────────────────────────────────────────────────────
input group "=== Parabolic Filter ==="
input double InpSmallCapPct     = 200.0;  // Small-Cap move threshold %
input double InpMidCapPct       = 100.0;  // Mid-Cap  move threshold %
input double InpLargeCapPct     = 50.0;   // Large-Cap move threshold %
input int    InpMAPeriod        = 20;     // MA period (D1)
input int    InpMALookback      = 60;     // D1 bars to look back for last MA touch

input group "=== Symbol Filter ==="
input string InpSymbolSuffix    = "";     // Only scan symbols containing this text (e.g. ".US", "#", leave blank = all)
input bool   InpStocksOnly      = true;   // Skip Forex/Futures — only CFD/Exchange stocks
input int    InpMinPrice        = 2;      // Skip penny stocks below this price (USD)

input group "=== Output ==="
input bool   InpAddToMarketWatch = true;  // Add qualifying symbols to Market Watch
input bool   InpLogAll           = false; // Log every symbol checked (verbose)

//─── Calc mode constants that represent stock/CFD types ────────────
// SYMBOL_CALC_MODE_CFD           = 2
// SYMBOL_CALC_MODE_CFDINDEX      = 3
// SYMBOL_CALC_MODE_CFDLEVERAGE   = 4
// SYMBOL_CALC_MODE_EXCH_STOCKS   = 32
// SYMBOL_CALC_MODE_EXCH_FUTURES  = 33

bool IsStockType(const string sym)
{
   long mode = SymbolInfoInteger(sym, SYMBOL_TRADE_CALC_MODE);
   return (mode == 2 || mode == 3 || mode == 4 || mode == 32);
}

//+------------------------------------------------------------------+
void OnStart()
{
   int    total      = SymbolsTotal(false);  // false = include non-Market-Watch symbols
   int    scanned    = 0;
   int    qualified  = 0;
   int    skipped    = 0;
   int    noData     = 0;

   string qualified200[];  // Small-cap hits (≥200%)
   string qualified100[];  // Mid-cap hits   (≥100%)
   string qualified50[];   // Large-cap hits (≥ 50%)
   int    q200 = 0, q100 = 0, q50 = 0;

   ArrayResize(qualified200, 0);
   ArrayResize(qualified100, 0);
   ArrayResize(qualified50,  0);

   Print("════════════════════════════════════════════════════");
   Print("ParabolicShort Scanner — checking ", total, " broker symbols");
   Print("Suffix filter: '", InpSymbolSuffix, "'  StocksOnly: ", InpStocksOnly);
   Print("Thresholds: Small≥", InpSmallCapPct, "%  Mid≥", InpMidCapPct, "%  Large≥", InpLargeCapPct, "%");
   Print("════════════════════════════════════════════════════");

   for(int i = 0; i < total; i++)
   {
      string sym = SymbolName(i, false);
      if(sym == "") continue;

      // ── Suffix filter ──────────────────────────────────────
      if(InpSymbolSuffix != "" && StringFind(sym, InpSymbolSuffix) < 0)
      {
         skipped++;
         continue;
      }

      // ── Stock-type filter ──────────────────────────────────
      if(InpStocksOnly && !IsStockType(sym))
      {
         skipped++;
         if(InpLogAll) Print("  SKIP (non-stock): ", sym);
         continue;
      }

      // ── Minimum price filter ───────────────────────────────
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      if(bid < InpMinPrice && InpMinPrice > 0)
      {
         skipped++;
         if(InpLogAll) Print("  SKIP (penny): ", sym, " bid=", bid);
         continue;
      }

      scanned++;

      // ── Ensure symbol is temporarily selected so we can pull D1 data
      bool wasSelected = (bool)SymbolInfoInteger(sym, SYMBOL_SELECT);
      if(!wasSelected) SymbolSelect(sym, true);

      // ── Load D1 history ────────────────────────────────────
      double movePct = CalcMovePct(sym);

      if(movePct < 0)
      {
         noData++;
         if(InpLogAll) Print("  NO DATA: ", sym);
         if(!wasSelected) SymbolSelect(sym, false);
         continue;
      }

      // ── Categorise and record ──────────────────────────────
      string category = "";
      bool   qualifies = false;

      if(movePct >= InpSmallCapPct)
      {
         category = StringFormat("SMALL-CAP  +%.1f%%", movePct);
         qualifies = true;
         ArrayResize(qualified200, q200 + 1);
         qualified200[q200++] = sym;
      }
      else if(movePct >= InpMidCapPct)
      {
         category = StringFormat("MID-CAP    +%.1f%%", movePct);
         qualifies = true;
         ArrayResize(qualified100, q100 + 1);
         qualified100[q100++] = sym;
      }
      else if(movePct >= InpLargeCapPct)
      {
         category = StringFormat("LARGE-CAP  +%.1f%%", movePct);
         qualifies = true;
         ArrayResize(qualified50, q50 + 1);
         qualified50[q50++] = sym;
      }

      if(qualifies)
      {
         qualified++;
         Print("  ✓ QUALIFIES  ", sym, "  [", category, "]");
         if(InpAddToMarketWatch) SymbolSelect(sym, true);
      }
      else
      {
         if(!wasSelected) SymbolSelect(sym, false);  // Clean up if we added it just to check
         if(InpLogAll) Print("  – ", sym, "  +", DoubleToString(movePct, 1), "% (below threshold)");
      }
   }

   // ── Summary ────────────────────────────────────────────────────
   Print("════════════════════════════════════════════════════");
   Print("SCAN COMPLETE");
   Print("  Total broker symbols : ", total);
   Print("  Symbols scanned      : ", scanned);
   Print("  Skipped (filtered)   : ", skipped);
   Print("  No D1 data           : ", noData);
   Print("  ─────────────────────────────────────────────");
   Print("  ≥", InpSmallCapPct, "% (Small-Cap)  : ", q200, " symbols");
   for(int i = 0; i < q200; i++) Print("      ", qualified200[i]);
   Print("  ≥", InpMidCapPct,   "% (Mid-Cap)    : ", q100, " symbols");
   for(int i = 0; i < q100; i++) Print("      ", qualified100[i]);
   Print("  ≥", InpLargeCapPct, "% (Large-Cap)  : ", q50,  " symbols");
   for(int i = 0; i < q50;  i++) Print("      ", qualified50[i]);
   Print("  ─────────────────────────────────────────────");
   Print("  TOTAL QUALIFYING     : ", qualified);
   if(InpAddToMarketWatch && qualified > 0)
      Print("  → Added to Market Watch. Attach ParabolicShortEA to each H1 chart.");
   Print("════════════════════════════════════════════════════");

   // ── Alert summary ──────────────────────────────────────────────
   if(qualified > 0)
      Alert("ParabolicShort Scanner: ", qualified, " qualifying symbols found. Check Experts log.");
   else
      Print("No qualifying symbols found with current thresholds.");
}

//─── Core: calculate % move from last 20MA base touch on D1 ────────
double CalcMovePct(const string sym)
{
   int lookback = InpMALookback + 5;

   // Request D1 bars
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(sym, PERIOD_D1, 0, lookback, rates);
   if(copied < InpMAPeriod + 5) return -1.0;

   // Calculate SMA on D1 closes
   double ma[];
   ArrayResize(ma, copied);
   for(int i = 0; i < copied; i++)
   {
      if(i < InpMAPeriod - 1) { ma[i] = 0; continue; }
      double sum = 0;
      for(int j = i; j < i + InpMAPeriod && j < copied; j++) sum += rates[j].close;
      ma[i] = sum / InpMAPeriod;
   }

   // Current close = rates[0].close (most recent)
   double curClose = rates[0].close;

   // Walk back to find the most recent bar where close ≤ 20MA (last base touch)
   int touchBar = -1;
   for(int i = 1; i < copied - InpMAPeriod; i++)
   {
      if(ma[i] <= 0) continue;
      if(rates[i].close <= ma[i])
      {
         touchBar = i;
         break;
      }
   }
   if(touchBar < 0) return -1.0;

   double baseClose = rates[touchBar].close;
   if(baseClose <= 0) return -1.0;

   return ((curClose - baseClose) / baseClose) * 100.0;
}
