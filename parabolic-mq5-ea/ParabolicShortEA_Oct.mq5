//+------------------------------------------------------------------+
//|                                     ParabolicShortEA_Oct.mq5    |
//|         Parabolic Short Strategy — Marios Stamatoudis method     |
//|         v2.0  ·  OctagonAI-enhanced build (2026-04-26)           |
//|                                                                  |
//|  PLAYBOOK RULES (all three components required):                 |
//|                                                                  |
//|  Filter 1 – Size of Move                                         |
//|    Price ≥ threshold% above the last 20MA base touch on D1.      |
//|    Small-cap ≥200%  |  Mid-cap ≥100%  |  Large-cap ≥50%         |
//|    "Measured from the most recent base, not the absolute bottom."|
//|                                                                  |
//|  Filter 2 – Aggressive Structure                                 |
//|    The move must show urgency: large candles, steep slope.       |
//|    Moves with regular pullbacks release pressure and are weaker. |
//|    Checked via: candle-range acceleration + pullback count.      |
//|                                                                  |
//|  Filter 3 – Exhaustion Day                                       |
//|    Intraday: price loses VWAP (D1-reset) and cannot reclaim it.  |
//|    Lower highs forming. If price stays above VWAP → NOT active.  |
//|                                                                  |
//|  Entry (safest → earliest):                                      |
//|    VWAP_REJECTION  Price tags VWAP from below, closes back below |
//|    VWAP_LOSS       Structure break + first VWAP cross together   |
//|    BREAK_STRUCTURE First LH+LL while below VWAP                  |
//|                                                                  |
//|  Risk management:                                                |
//|    Stop = day high (or recent lower high) + buffer               |
//|    TP1 partial close → stop to break-even                        |
//|    Risk-free add on 2nd VWAP rejection after BE                  |
//|    Max 2 attempts per exhaustion window; EOD close always        |
//|    2-day window: setup expires if no entry by end of day+1       |
//+------------------------------------------------------------------+
#property copyright "Parabolic Short Strategy v2.0 (Oct)"
#property version   "2.00"
#property description "Parabolic Short — Marios Stamatoudis method v2 (OctagonAI build)"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//═══ Enums ═════════════════════════════════════════════════════════════════════
enum ENUM_CAP_CATEGORY { CAP_SMALL=0, CAP_MID=1, CAP_LARGE=2 };
enum ENUM_ENTRY_TYPE   { ENTRY_VWAP_REJECTION=0, ENTRY_VWAP_LOSS=1, ENTRY_BREAK_STRUCTURE=2 };
enum ENUM_STOP_TYPE    { STOP_DAY_HIGH=0, STOP_RECENT_LOWER_HIGH=1 };

enum ENUM_EA_STATE
{
   STATE_IDLE       = 0,  // Scanning D1 for parabolic setup
   STATE_PARABOLIC  = 1,  // Filters 1+2 confirmed; watching for exhaustion day
   STATE_EXHAUSTION = 2,  // Exhaustion confirmed; watching for entry (2-day window)
   STATE_IN_TRADE   = 3   // Position open; managing stops / partials
};

//═══ Inputs ════════════════════════════════════════════════════════════════════
input group "=== Filter 1: Size of Move ==="
input ENUM_CAP_CATEGORY InpCapCategory   = CAP_SMALL; // Market Cap Category
input double  InpSmallCapPct             = 200.0;     // Small-Cap move threshold %
input double  InpMidCapPct               = 100.0;     // Mid-Cap  move threshold %
input double  InpLargeCapPct             = 50.0;      // Large-Cap move threshold %
input int     InpMAPeriod                = 20;        // Base MA period (D1)
input int     InpMALookback              = 60;        // D1 bars to look back for last MA touch

input group "=== Filter 2: Aggressive Structure ==="
input int     InpAccelRecentBars         = 5;         // Recent bars for range acceleration check
input int     InpAccelPriorBars          = 15;        // Prior bars for range baseline
input double  InpAccelMultiplier         = 1.3;       // Recent range must be >= N x prior range
input int     InpMaxPullbackDays         = 3;         // Max significant down-days allowed in run
input double  InpPullbackPct             = 2.0;       // Down-day threshold % (close<open by >=X%)

input group "=== Filter 3: Exhaustion Day ==="
input int     InpVWAPBelowBars           = 1;         // Min H1 bars closed below VWAP to confirm
input double  InpVWAPBandPct             = 0.3;       // VWAP rejection band tolerance (%)
input double  InpVWAPReclaimPct          = 0.5;       // % above VWAP = strong reclaim -> invalid
input int     InpMaxExhaustDays          = 2;         // Days after parabolic peak to wait

input group "=== Entry / Stop ==="
input ENUM_ENTRY_TYPE InpEntryType       = ENTRY_VWAP_REJECTION; // Entry signal type
input ENUM_STOP_TYPE  InpStopType        = STOP_DAY_HIGH;        // Stop loss anchor
input double  InpStopBufferPct           = 0.1;       // Buffer % above stop anchor
input int     InpMaxAttempts             = 2;         // Max attempts per exhaustion window

input group "=== Trade Management ==="
input double  InpRiskPercent             = 1.0;       // Risk % of account per trade
input double  InpMaxLotSize              = 0.0;       // Max lot size (0 = symbol max)
input double  InpTP1Ratio                = 1.0;       // TP1 as R-multiple (1.0 = 1:1)
input double  InpTP1ClosePct             = 50.0;      // % of position to close at TP1
input bool    InpMoveToBE                = true;      // Move stop to break-even after TP1
input bool    InpRFAdd                   = true;      // Risk-free add on 2nd VWAP rejection
input double  InpRFAddRiskPct            = 0.5;       // Risk % for the risk-free add
input bool    InpCloseEOD                = true;      // Force close at EOD
input int     InpEODHour                 = 15;        // EOD close hour (server time)
input int     InpEODMin                  = 45;        // EOD close minute

input group "=== Settings ==="
input long    InpMagic                   = 20260419;  // Magic number
input int     InpSlippage                = 20;        // Slippage (points)
input bool    InpDashboard               = true;      // Show dashboard

//═══ Globals ═══════════════════════════════════════════════════════════════════
CTrade        g_trade;
CPositionInfo g_pos;
int           g_maHandle;

// State machine
ENUM_EA_STATE g_state          = STATE_IDLE;
datetime      g_lastBar        = 0;

// D1 filter results
double        g_movePct        = 0;
bool          g_filter1        = false;
bool          g_filter2        = false;
int           g_daysSincePeak  = -1;

// Session tracking
datetime      g_dayStart       = 0;
double        g_dayHigh        = 0;

// VWAP (D1-reset, H1-bar accumulated)
double        g_vwapPV         = 0;
double        g_vwapVol        = 0;
double        g_vwap           = 0;
bool          g_vwapCrossed    = false;
int           g_barsBelowVwap  = 0;

// Intraday structure
static const int STRUCT_SZ = 20;
double        g_sHigh[20];
double        g_sLow[20];
int           g_sCnt           = 0;

// Trade state
double        g_entryPx        = 0;
double        g_stopPx         = 0;
double        g_tp1Px          = 0;
bool          g_tp1Hit         = false;
bool          g_stopAtBE       = false;
bool          g_rfAdded        = false;
int           g_attempts       = 0;
bool          g_entryFired     = false;

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   g_trade.SetDeviationInPoints(InpSlippage);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   g_maHandle = iMA(_Symbol, PERIOD_D1, InpMAPeriod, 0, MODE_SMA, PRICE_CLOSE);
   if(g_maHandle == INVALID_HANDLE)
   {
      Alert("ParabolicShortEA_Oct v2: iMA handle failed on ", _Symbol);
      return INIT_FAILED;
   }

   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);

   Print("ParabolicShortEA_Oct v2.0  sym=", _Symbol,
         "  cap=",   EnumToString(InpCapCategory),
         "  entry=", EnumToString(InpEntryType),
         "  stop=",  EnumToString(InpStopType));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   IndicatorRelease(g_maHandle);
   Comment("");
}

//+------------------------------------------------------------------+
void OnTick()
{
   datetime barTime  = iTime(_Symbol, PERIOD_CURRENT, 0);
   bool     isNewBar = (barTime != g_lastBar);

   HandleDayReset(barTime, isNewBar);
   UpdateVWAP(isNewBar);
   TrackDayHigh();

   if(isNewBar)
   {
      g_lastBar = barTime;
      OnNewBar();
   }

   if(g_state == STATE_EXHAUSTION && !g_entryFired && g_attempts < InpMaxAttempts)
      ScanEntrySignal();

   if(g_state == STATE_IN_TRADE)
      ManagePosition();

   if(InpCloseEOD)    CheckEODClose();
   if(InpDashboard)   DrawDashboard();
}

//═══ Day / Session Reset ═══════════════════════════════════════════════════════
void HandleDayReset(datetime barTime, bool isNewBar)
{
   if(!isNewBar) return;
   MqlDateTime dt;
   TimeToStruct(barTime, dt);
   datetime ds = StringToTime(StringFormat("%04d.%02d.%02d 00:00", dt.year, dt.mon, dt.day));
   if(ds == g_dayStart) return;

   g_dayStart      = ds;
   g_dayHigh       = 0;
   g_vwapPV        = 0;
   g_vwapVol       = 0;
   g_vwap          = 0;
   g_vwapCrossed   = false;
   g_barsBelowVwap = 0;
   g_entryFired    = false;
   g_attempts      = 0;
   g_sCnt          = 0;
   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);

   if(g_daysSincePeak >= 0) g_daysSincePeak++;

   // Playbook: "If nothing happens within two days, skip the setup."
   if(g_daysSincePeak > InpMaxExhaustDays && g_state == STATE_PARABOLIC)
   {
      Print("ParabolicShortEA_Oct v2: 2-day exhaustion window expired -> IDLE");
      ResetAll();
   }
}

void TrackDayHigh()
{
   double hi = iHigh(_Symbol, PERIOD_CURRENT, 0);
   if(hi > g_dayHigh) g_dayHigh = hi;
}

//═══ VWAP ══════════════════════════════════════════════════════════════════════
void UpdateVWAP(bool isNewBar)
{
   if(!isNewBar) return;
   datetime t1 = iTime(_Symbol, PERIOD_CURRENT, 1);
   if(t1 < g_dayStart) return;

   double hi = iHigh  (_Symbol, PERIOD_CURRENT, 1);
   double lo = iLow   (_Symbol, PERIOD_CURRENT, 1);
   double cl = iClose (_Symbol, PERIOD_CURRENT, 1);
   long   vo = iVolume(_Symbol, PERIOD_CURRENT, 1);
   if(vo <= 0) return;

   double tp   = (hi + lo + cl) / 3.0;
   g_vwapPV   += tp * (double)vo;
   g_vwapVol  += (double)vo;
   g_vwap      = g_vwapPV / g_vwapVol;

   if(cl < g_vwap)  { g_barsBelowVwap++; g_vwapCrossed = true; }
   else               g_barsBelowVwap = 0;
}

//═══ D1 Filters ════════════════════════════════════════════════════════════════
void OnNewBar()
{
   UpdateD1Filters();

   switch(g_state)
   {
      case STATE_IDLE:
         if(g_filter1 && g_filter2)
         {
            g_daysSincePeak = 0;
            TransitionTo(STATE_PARABOLIC);
            Print("ParabolicShortEA_Oct v2: PARABOLIC  move=",
                  DoubleToString(g_movePct, 1), "%  need>=", GetThreshold(), "%");
         }
         break;

      case STATE_PARABOLIC:
         if(!g_filter1) { Print("ParabolicShortEA_Oct v2: Move fell below threshold -> IDLE"); ResetAll(); break; }
         AppendStructureBar();
         CheckExhaustionStart();
         break;

      case STATE_EXHAUSTION:
         AppendStructureBar();
         CheckExhaustionInvalidation();
         if(!HasOpenPosition() && g_entryFired && g_attempts < InpMaxAttempts)
            g_entryFired = false;
         break;

      case STATE_IN_TRADE:
         break;
   }
}

void UpdateD1Filters()
{
   int need = InpMALookback + InpAccelPriorBars + InpAccelRecentBars + 5;

   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(g_maHandle, 0, 0, need, ma) < need) return;

   double closeD1[], openD1[], highD1[], lowD1[];
   ArraySetAsSeries(closeD1, true);
   ArraySetAsSeries(openD1,  true);
   ArraySetAsSeries(highD1,  true);
   ArraySetAsSeries(lowD1,   true);
   if(CopyClose(_Symbol, PERIOD_D1, 0, need, closeD1) < need) return;
   if(CopyOpen (_Symbol, PERIOD_D1, 0, need, openD1)  < need) return;
   if(CopyHigh (_Symbol, PERIOD_D1, 0, need, highD1)  < need) return;
   if(CopyLow  (_Symbol, PERIOD_D1, 0, need, lowD1)   < need) return;

   // Filter 1 – Size of move from last 20MA base touch
   int touchBar = -1;
   for(int i = 1; i < InpMALookback - 1; i++)
      if(closeD1[i] <= ma[i]) { touchBar = i; break; }

   if(touchBar < 0 || closeD1[touchBar] <= 0) { g_filter1 = false; return; }

   g_movePct = ((closeD1[0] - closeD1[touchBar]) / closeD1[touchBar]) * 100.0;
   g_filter1  = (g_movePct >= GetThreshold());

   // Filter 2 – Aggressive structure: acceleration + few pullbacks
   double rRecent = 0, rPrior = 0;
   for(int i = 0; i < InpAccelRecentBars; i++)               rRecent += highD1[i] - lowD1[i];
   for(int i = InpAccelRecentBars; i < InpAccelRecentBars + InpAccelPriorBars; i++)
                                                               rPrior  += highD1[i] - lowD1[i];
   rRecent /= InpAccelRecentBars;
   rPrior  /= InpAccelPriorBars;
   bool accel = (rPrior > 0 && rRecent >= InpAccelMultiplier * rPrior);

   int pullbacks = 0;
   for(int i = 1; i < touchBar; i++)
      if(openD1[i] > 0 && (closeD1[i] - openD1[i]) / openD1[i] * 100.0 <= -InpPullbackPct)
         pullbacks++;
   bool fewPullbacks = (pullbacks <= InpMaxPullbackDays);

   g_filter2 = accel || fewPullbacks;
}

double GetThreshold()
{
   switch(InpCapCategory)
   {
      case CAP_MID:   return InpMidCapPct;
      case CAP_LARGE: return InpLargeCapPct;
      default:        return InpSmallCapPct;
   }
}

//═══ Exhaustion Detection ══════════════════════════════════════════════════════
void CheckExhaustionStart()
{
   if(g_state != STATE_PARABOLIC)   return;
   if(g_vwap <= 0 || !g_vwapCrossed) return;
   if(g_barsBelowVwap < InpVWAPBelowBars) return;
   if(!HasLHLL()) return;

   TransitionTo(STATE_EXHAUSTION);
   Print("ParabolicShortEA_Oct v2: EXHAUSTION confirmed",
         "  dayHigh=", DoubleToString(g_dayHigh, _Digits),
         "  vwap=",    DoubleToString(g_vwap, _Digits));
}

void CheckExhaustionInvalidation()
{
   if(g_state != STATE_EXHAUSTION || g_vwap <= 0) return;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > g_vwap * (1.0 + InpVWAPReclaimPct / 100.0) && g_barsBelowVwap == 0)
   {
      Print("ParabolicShortEA_Oct v2: VWAP strongly reclaimed -> setup INVALID -> IDLE");
      ResetAll();
   }
}

//═══ Intraday Structure ════════════════════════════════════════════════════════
void AppendStructureBar()
{
   if(g_sCnt >= STRUCT_SZ - 1) return;
   g_sHigh[g_sCnt] = iHigh(_Symbol, PERIOD_CURRENT, 1);
   g_sLow[g_sCnt]  = iLow (_Symbol, PERIOD_CURRENT, 1);
   g_sCnt++;
}

bool HasLHLL()
{
   if(g_sCnt < 2) return false;
   int n = g_sCnt - 1;
   return (g_sHigh[n] < g_sHigh[n-1] && g_sLow[n] < g_sLow[n-1]);
}

double GetRecentLowerHigh()
{
   for(int i = g_sCnt-1; i >= 0; i--)
      if(g_sHigh[i] > 0 && g_sHigh[i] < g_dayHigh)
         return g_sHigh[i];
   return g_dayHigh;
}

//═══ Entry Signals ═════════════════════════════════════════════════════════════
void ScanEntrySignal()
{
   if(g_vwap <= 0 || !g_vwapCrossed) return;
   switch(InpEntryType)
   {
      case ENTRY_VWAP_REJECTION:  CheckVWAPRejection();  break;
      case ENTRY_VWAP_LOSS:       CheckVWAPLoss();        break;
      case ENTRY_BREAK_STRUCTURE: CheckBreakStructure();  break;
   }
}

// Playbook best entry: bar opened below VWAP, tagged it, closed back below -> rejection
void CheckVWAPRejection()
{
   if(g_barsBelowVwap < 1) return;
   double band  = g_vwap * (InpVWAPBandPct / 100.0);
   double barHi = iHigh (_Symbol, PERIOD_CURRENT, 1);
   double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
   double barOp = iOpen (_Symbol, PERIOD_CURRENT, 1);
   if(barOp < g_vwap && barHi >= g_vwap - band && barCl < g_vwap)
      FireShort("VWAP_REJECTION");
}

// Structure break + first bar to close below VWAP simultaneously
void CheckVWAPLoss()
{
   double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
   double prevCl = iClose(_Symbol, PERIOD_CURRENT, 2);
   if(HasLHLL() && barCl < g_vwap && prevCl >= g_vwap)
      FireShort("VWAP_LOSS");
}

// Earliest: LH+LL while below VWAP (most risk)
void CheckBreakStructure()
{
   if(!HasLHLL()) return;
   if(SymbolInfoDouble(_Symbol, SYMBOL_BID) < g_vwap)
      FireShort("BREAK_STRUCTURE");
}

//═══ Order Execution ═══════════════════════════════════════════════════════════
void FireShort(const string reason)
{
   if(g_entryFired || g_attempts >= InpMaxAttempts || HasOpenPosition()) return;

   double anchor  = (InpStopType == STOP_RECENT_LOWER_HIGH) ? GetRecentLowerHigh() : g_dayHigh;
   if(anchor <= 0) return;

   double bid     = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double stopPx  = anchor * (1.0 + InpStopBufferPct / 100.0);
   double dist    = stopPx - bid;
   if(dist <= 0) { Print("ParabolicShortEA_Oct v2: Skip (", reason, ") stop<=bid"); return; }

   double tp1Px = bid - dist * InpTP1Ratio;
   double lots  = CalcLots(bid, stopPx, InpRiskPercent);
   if(lots <= 0) return;

   if(g_trade.Sell(lots, _Symbol, bid, stopPx, 0, "PS2:" + reason))
   {
      g_entryPx    = bid;
      g_stopPx     = stopPx;
      g_tp1Px      = tp1Px;
      g_tp1Hit     = false;
      g_stopAtBE   = false;
      g_rfAdded    = false;
      g_entryFired = true;
      g_attempts++;
      TransitionTo(STATE_IN_TRADE);
      Print("ParabolicShortEA_Oct v2: SHORT (", reason, ")",
            "  lots=",  DoubleToString(lots,   2),
            "  entry=", DoubleToString(bid,    _Digits),
            "  stop=",  DoubleToString(stopPx, _Digits),
            "  tp1=",   DoubleToString(tp1Px,  _Digits),
            "  att=",   g_attempts, "/", InpMaxAttempts);
   }
   else
      Print("ParabolicShortEA_Oct v2: Order FAILED (", reason, ") err=", GetLastError());
}

//═══ Position Management ═══════════════════════════════════════════════════════
void ManagePosition()
{
   if(!HasOpenPosition())
   {
      if(g_attempts < InpMaxAttempts)
      {
         Print("ParabolicShortEA_Oct v2: Position closed. Re-arming attempt ", g_attempts+1);
         g_entryFired = false;
         TransitionTo(STATE_EXHAUSTION);
      }
      else
      {
         Print("ParabolicShortEA_Oct v2: All ", InpMaxAttempts, " attempts used -> IDLE");
         ResetAll();
      }
      return;
   }

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // TP1 partial close
   if(!g_tp1Hit && g_tp1Px > 0 && bid <= g_tp1Px)
   {
      if(DoPartialClose(InpTP1ClosePct / 100.0))
      {
         g_tp1Hit = true;
         Print("ParabolicShortEA_Oct v2: TP1 hit  price=", bid, "  closed ", InpTP1ClosePct, "%");
         if(InpMoveToBE) MoveToBE();
      }
   }

   // Risk-free add: 2nd VWAP rejection after stop at break-even
   if(InpRFAdd && g_tp1Hit && g_stopAtBE && !g_rfAdded && g_vwap > 0)
   {
      double band  = g_vwap * (InpVWAPBandPct / 100.0);
      double barHi = iHigh (_Symbol, PERIOD_CURRENT, 1);
      double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
      double barOp = iOpen (_Symbol, PERIOD_CURRENT, 1);
      if(barOp < g_vwap && barHi >= g_vwap - band && barCl < g_vwap)
      {
         double al = CalcLots(bid, g_entryPx, InpRFAddRiskPct);
         if(al > 0 && g_trade.Sell(al, _Symbol, bid, g_entryPx, 0, "PS2:RF_ADD"))
         {
            g_rfAdded = true;
            Print("ParabolicShortEA_Oct v2: RF ADD  lots=", al,
                  "  stop=BE(", DoubleToString(g_entryPx, _Digits), ")");
         }
      }
   }

   // Keep stop current
   ulong ticket = GetOpenTicket();
   if(ticket > 0 && g_stopPx > 0)
      g_trade.PositionModify(ticket, g_stopPx, 0);
}

bool DoPartialClose(double fraction)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic() != (ulong)InpMagic || g_pos.Symbol() != _Symbol) continue;
      double cl = NormalizeLots(g_pos.Volume() * fraction);
      if(cl < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN)) return false;
      return g_trade.PositionClosePartial(g_pos.Ticket(), cl);
   }
   return false;
}

void MoveToBE()
{
   ulong t = GetOpenTicket();
   if(t > 0 && g_trade.PositionModify(t, g_entryPx, 0))
   {
      g_stopPx = g_entryPx;
      g_stopAtBE = true;
      Print("ParabolicShortEA_Oct v2: Stop -> BE=", DoubleToString(g_entryPx, _Digits));
   }
}

//═══ EOD Close ═════════════════════════════════════════════════════════════════
void CheckEODClose()
{
   if(!HasOpenPosition()) return;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.hour > InpEODHour || (dt.hour == InpEODHour && dt.min >= InpEODMin))
   {
      for(int i = PositionsTotal()-1; i >= 0; i--)
      {
         if(!g_pos.SelectByIndex(i)) continue;
         if(g_pos.Magic() != (ulong)InpMagic || g_pos.Symbol() != _Symbol) continue;
         g_trade.PositionClose(g_pos.Ticket());
         Print("ParabolicShortEA_Oct v2: EOD close  ticket=", g_pos.Ticket());
      }
      ResetAll();
   }
}

//═══ Helpers ═══════════════════════════════════════════════════════════════════
bool HasOpenPosition()
{
   for(int i = 0; i < PositionsTotal(); i++)
      if(g_pos.SelectByIndex(i) && g_pos.Magic() == (ulong)InpMagic && g_pos.Symbol() == _Symbol)
         return true;
   return false;
}

ulong GetOpenTicket()
{
   for(int i = 0; i < PositionsTotal(); i++)
      if(g_pos.SelectByIndex(i) && g_pos.Magic() == (ulong)InpMagic && g_pos.Symbol() == _Symbol)
         return g_pos.Ticket();
   return 0;
}

double CalcLots(double entry, double stop, double riskPct)
{
   double dist = MathAbs(stop - entry);
   if(dist <= 0) return 0;
   double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tv <= 0 || ts <= 0) return 0;
   double risk = AccountInfoDouble(ACCOUNT_BALANCE) * riskPct / 100.0;
   return NormalizeLots(risk / (dist * tv / ts));
}

double NormalizeLots(double lots)
{
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double mn   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   lots = MathFloor(lots / step) * step;
   lots = MathMax(lots, mn);
   if(InpMaxLotSize > 0) lots = MathMin(lots, InpMaxLotSize);
   return MathMin(lots, mx);
}

void TransitionTo(ENUM_EA_STATE s)
{
   if(g_state == s) return;
   Print("ParabolicShortEA_Oct v2: [", EnumToString(g_state), "] -> [", EnumToString(s), "]");
   g_state = s;
}

void ResetAll()
{
   TransitionTo(STATE_IDLE);
   g_daysSincePeak = -1;
   g_movePct       = 0;
   g_filter1       = false;
   g_filter2       = false;
   g_entryFired    = false;
   g_attempts      = 0;
   g_entryPx       = 0;
   g_stopPx        = 0;
   g_tp1Px         = 0;
   g_tp1Hit        = false;
   g_stopAtBE      = false;
   g_rfAdded       = false;
   g_sCnt          = 0;
   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);
}

//═══ Dashboard ═════════════════════════════════════════════════════════════════
void DrawDashboard()
{
   string s = "== ParabolicShort_Oct v2.0 ==\n";
   s += StringFormat("Symbol  : %s  TF:%s\n", _Symbol, EnumToString(Period()));
   s += StringFormat("State   : %s\n", EnumToString(g_state));
   s += StringFormat("F1 Size : %s  %.1f%% (need %.0f%%)\n",
                      g_filter1 ? "PASS" : "FAIL", g_movePct, GetThreshold());
   s += StringFormat("F2 Struc: %s\n", g_filter2 ? "PASS" : "FAIL");
   s += StringFormat("VWAP    : %s\n", g_vwap > 0 ? DoubleToString(g_vwap, _Digits) : "---");
   s += StringFormat("Day Hi  : %s\n", DoubleToString(g_dayHigh, _Digits));
   s += StringFormat("<VWAP   : %d bars  crossed:%s\n",
                      g_barsBelowVwap, g_vwapCrossed ? "yes" : "no");
   if(g_state >= STATE_EXHAUSTION)
      s += StringFormat("LH+LL   : %s\n", HasLHLL() ? "YES" : "no");
   if(g_state == STATE_EXHAUSTION)
      s += StringFormat("Attempt : %d/%d  Entry:%s\n",
                         g_attempts, InpMaxAttempts, EnumToString(InpEntryType));
   if(g_state == STATE_IN_TRADE)
   {
      s += StringFormat("Entry   : %s\n", DoubleToString(g_entryPx, _Digits));
      s += StringFormat("Stop    : %s%s\n",
                         DoubleToString(g_stopPx, _Digits), g_stopAtBE ? " [BE]" : "");
      s += StringFormat("TP1     : %s%s\n",
                         DoubleToString(g_tp1Px,  _Digits), g_tp1Hit  ? " [hit]" : "");
      s += StringFormat("RF Add  : %s\n",
                         g_rfAdded ? "done" :
                         (g_stopAtBE && g_tp1Hit ? "watching" : "---"));
      s += StringFormat("Attempt : %d/%d\n", g_attempts, InpMaxAttempts);
   }
   Comment(s);
}
