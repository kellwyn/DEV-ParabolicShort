//+------------------------------------------------------------------+
//|                                        ParabolicShortEA.mq5     |
//|        Parabolic Short Strategy — Marios Stamatoudis method      |
//|        v1.0  ·  Iterative MT5 build                             |
//|                                                                  |
//|  Recommended chart: H1 on a stock CFD symbol                    |
//|                                                                  |
//|  Three required components (all must be present):               |
//|    1. Parabolic move  — price ≥ threshold% from last 20MA base  |
//|    2. Aggressive structure — steep slope, large candles          |
//|    3. Exhaustion day — price loses VWAP, lower highs forming     |
//|                                                                  |
//|  Entry hierarchy (safest → earliest):                           |
//|    VWAP_REJECTION: failed attempt to reclaim VWAP (best)        |
//|    VWAP_LOSS:      structure break + VWAP loss simultaneously    |
//|    BREAK_STRUCTURE: first lower high + lower low (most risk)    |
//|                                                                  |
//|  Trade management:                                               |
//|    • Stop = day high (or recent lower high) + buffer             |
//|    • TP1 partial close → move stop to break-even                |
//|    • Risk-free add on 2nd VWAP rejection after BE               |
//|    • Max 2 attempts per exhaustion window; close EOD             |
//+------------------------------------------------------------------+
#property copyright "Parabolic Short Strategy"
#property version   "1.00"
#property description "Parabolic Short — Marios Stamatoudis method"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//─── Enums ─────────────────────────────────────────────────────────────────────
enum ENUM_CAP_CATEGORY
{
   CAP_SMALL = 0,  // Small-Cap  (≥ 200% threshold)
   CAP_MID   = 1,  // Mid-Cap    (≥ 100% threshold)
   CAP_LARGE = 2   // Large-Cap  (≥  50% threshold)
};

enum ENUM_ENTRY_TYPE
{
   ENTRY_VWAP_REJECTION  = 0,  // VWAP Rejection — price fails to reclaim VWAP (safest)
   ENTRY_VWAP_LOSS       = 1,  // VWAP Loss — structure break + VWAP loss together
   ENTRY_BREAK_STRUCTURE = 2   // Break of Structure — first LH+LL (earliest / most risk)
};

enum ENUM_STOP_TYPE
{
   STOP_DAY_HIGH          = 0,  // High of the exhaustion day
   STOP_RECENT_LOWER_HIGH = 1   // Most recent lower high before entry
};

enum ENUM_EA_STATE
{
   STATE_IDLE       = 0,  // Scanning for parabolic setup on D1
   STATE_PARABOLIC  = 1,  // Parabolic move confirmed — watching for exhaustion day
   STATE_EXHAUSTION = 2,  // Exhaustion day active — watching for entry signal
   STATE_IN_TRADE   = 3   // Position open — managing stops / partials
};

//─── Inputs ────────────────────────────────────────────────────────────────────
input group "=== Parabolic Setup ==="
input ENUM_CAP_CATEGORY InpCapCategory   = CAP_SMALL;  // Market Cap Category
input double  InpSmallCapPct             = 200.0;      // Small-Cap move threshold %
input double  InpMidCapPct               = 100.0;      // Mid-Cap  move threshold %
input double  InpLargeCapPct             = 50.0;       // Large-Cap move threshold %
input int     InpMAPeriod                = 20;         // MA period for base detection (D1)
input int     InpMALookback              = 60;         // D1 bars to look back for last MA touch

input group "=== Exhaustion Detection ==="
input int     InpMaxExhaustWaitDays      = 2;          // Days to watch for exhaustion after setup
input int     InpVWAPBelowBars           = 1;          // Completed bars below VWAP to confirm exhaustion
input double  InpVWAPBandPct             = 0.3;        // VWAP band for rejection check (%)
input double  InpVWAPReclaimPct          = 0.5;        // % above VWAP = strong reclaim → setup invalid

input group "=== Entry / Stop ==="
input ENUM_ENTRY_TYPE InpEntryType       = ENTRY_VWAP_REJECTION; // Entry Signal
input ENUM_STOP_TYPE  InpStopType        = STOP_DAY_HIGH;        // Stop Loss Anchor
input double  InpStopBufferPct           = 0.1;        // Buffer above stop anchor (%)
input int     InpMaxAttempts             = 2;          // Max trade attempts per exhaustion window

input group "=== Trade Management ==="
input double  InpRiskPercent             = 1.0;        // Risk % of account per trade
input double  InpMaxLotSize              = 0.0;        // Max lot size (0 = symbol limit)
input double  InpTP1Ratio                = 1.0;        // TP1 as R-multiple (1.0 = 1:1)
input double  InpTP1ClosePct             = 50.0;       // Position % to close at TP1
input bool    InpMoveStopToBE            = true;       // Move stop to break-even after TP1
input bool    InpRiskFreeAdd             = true;       // Add on 2nd VWAP rejection after BE
input double  InpRFAddRiskPct            = 0.5;        // Risk % for risk-free add
input bool    InpCloseEOD                = true;       // Force-close positions at EOD
input int     InpEODCloseHour            = 15;         // EOD close hour (server time)
input int     InpEODCloseMinute          = 45;         // EOD close minute

input group "=== Trade Settings ==="
input long    InpMagicNumber             = 20260419;   // Magic Number
input int     InpSlippage                = 20;         // Slippage (points)
input bool    InpShowDashboard           = true;       // Show info panel

//─── Globals ───────────────────────────────────────────────────────────────────
CTrade        g_trade;
CPositionInfo g_pos;

int           g_maHandle;
int           g_atrHandle;

// EA state machine
ENUM_EA_STATE g_state          = STATE_IDLE;
datetime      g_lastBarTime    = 0;
datetime      g_setupDate      = 0;     // D1 date parabolic was confirmed
datetime      g_exhaustDate    = 0;     // day exhaustion started

// Current-day tracking
datetime      g_dayStart       = 0;
double        g_dayOpen        = 0;
double        g_dayHigh        = 0;

// VWAP (accumulated bar-by-bar from session open, reset daily)
double        g_vwapPV         = 0;
double        g_vwapVol        = 0;
double        g_vwap           = 0;
bool          g_vwapCrossed    = false; // price has gone below VWAP today
int           g_barsBelowVWAP  = 0;

// Intraday structure (rolling window of bar H/L for lower-high/lower-low detection)
static const int STRUCT_SIZE   = 20;
double        g_sHigh[];
double        g_sLow[];
int           g_sCnt           = 0;

// Position state
double        g_entryPrice     = 0;
double        g_stopPrice      = 0;
double        g_tp1Price       = 0;
bool          g_tp1Hit         = false;
bool          g_stopAtBE       = false;
bool          g_riskFreeAdded  = false;
int           g_attemptsToday  = 0;
bool          g_entryFired     = false;

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber((ulong)InpMagicNumber);
   g_trade.SetDeviationInPoints(InpSlippage);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   g_maHandle  = iMA(_Symbol, PERIOD_D1, InpMAPeriod, 0, MODE_SMA, PRICE_CLOSE);
   g_atrHandle = iATR(_Symbol, PERIOD_D1, 14);

   if(g_maHandle == INVALID_HANDLE || g_atrHandle == INVALID_HANDLE)
   {
      Alert("ParabolicShortEA: Indicator init failed on ", _Symbol);
      return INIT_FAILED;
   }

   ArrayResize(g_sHigh, STRUCT_SIZE);
   ArrayResize(g_sLow,  STRUCT_SIZE);
   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);

   Print("ParabolicShortEA v1.0  sym=", _Symbol,
         "  tf=",    EnumToString(Period()),
         "  cap=",   EnumToString(InpCapCategory),
         "  entry=", EnumToString(InpEntryType),
         "  stop=",  EnumToString(InpStopType));
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   IndicatorRelease(g_maHandle);
   IndicatorRelease(g_atrHandle);
   Comment("");
}

//+------------------------------------------------------------------+
void OnTick()
{
   datetime barTime  = iTime(_Symbol, PERIOD_CURRENT, 0);
   bool     isNewBar = (barTime != g_lastBarTime);

   HandleDayBoundary(barTime, isNewBar);
   UpdateVWAP(isNewBar);
   TrackDayHigh();

   switch(g_state)
   {
      case STATE_IDLE:
         if(isNewBar) ScanParabolicSetup();
         break;

      case STATE_PARABOLIC:
         if(isNewBar)
         {
            ScanParabolicSetup();     // Re-validate each bar (setup can disappear)
            ScanExhaustionStart();
            CheckSetupExpiry();
         }
         break;

      case STATE_EXHAUSTION:
         if(isNewBar)
         {
            AppendStructureBar();
            CheckExhaustionExpiry();
         }
         if(!g_entryFired && g_attemptsToday < InpMaxAttempts)
            ScanEntrySignal();
         break;

      case STATE_IN_TRADE:
         ManageOpenPosition();
         break;
   }

   if(InpCloseEOD)        CheckEODClose();
   if(InpShowDashboard)   DrawDashboard();

   if(isNewBar) g_lastBarTime = barTime;
}

//═══ Day Boundary ══════════════════════════════════════════════════════════════
void HandleDayBoundary(datetime barTime, bool isNewBar)
{
   if(!isNewBar) return;

   MqlDateTime dt;
   TimeToStruct(barTime, dt);
   datetime ds = StringToTime(StringFormat("%04d.%02d.%02d 00:00",
                                            dt.year, dt.mon, dt.day));
   if(ds == g_dayStart) return;

   g_dayStart        = ds;
   g_dayOpen         = iOpen(_Symbol, PERIOD_CURRENT, 0);
   g_dayHigh         = 0;
   g_vwapPV          = 0;
   g_vwapVol         = 0;
   g_vwap            = 0;
   g_vwapCrossed     = false;
   g_barsBelowVWAP   = 0;
   g_entryFired      = false;
   g_attemptsToday   = 0;
   g_sCnt            = 0;
   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);
}

//═══ VWAP ══════════════════════════════════════════════════════════════════════
void UpdateVWAP(bool isNewBar)
{
   if(!isNewBar) return;

   // Accumulate the just-completed bar if it belongs to today
   datetime t1 = iTime(_Symbol, PERIOD_CURRENT, 1);
   if(t1 < g_dayStart) return;

   double hi = iHigh(_Symbol,   PERIOD_CURRENT, 1);
   double lo = iLow(_Symbol,    PERIOD_CURRENT, 1);
   double cl = iClose(_Symbol,  PERIOD_CURRENT, 1);
   long   vo = iVolume(_Symbol, PERIOD_CURRENT, 1);

   if(vo > 0)
   {
      double tp  = (hi + lo + cl) / 3.0;
      g_vwapPV  += tp * (double)vo;
      g_vwapVol += (double)vo;
      g_vwap     = g_vwapPV / g_vwapVol;
   }

   // Track how many completed bars have closed below VWAP
   if(g_vwap > 0)
   {
      if(cl < g_vwap)
      {
         g_barsBelowVWAP++;
         g_vwapCrossed = true;
      }
      else
      {
         g_barsBelowVWAP = 0;
      }
   }
}

void TrackDayHigh()
{
   double hi = iHigh(_Symbol, PERIOD_CURRENT, 0);
   if(hi > g_dayHigh) g_dayHigh = hi;
}

//═══ Parabolic Setup (D1) ══════════════════════════════════════════════════════
void ScanParabolicSetup()
{
   bool valid = IsParabolicSetup();

   if(!valid)
   {
      if(g_state == STATE_PARABOLIC)
      {
         Print("ParabolicShort: Setup no longer valid → IDLE");
         TransitionTo(STATE_IDLE);
      }
      return;
   }

   if(g_state == STATE_IDLE)
   {
      g_setupDate = iTime(_Symbol, PERIOD_D1, 0);
      TransitionTo(STATE_PARABOLIC);
      Print("ParabolicShort: Parabolic CONFIRMED  sym=", _Symbol,
            "  movePct=", DoubleToString(CalcMovePct(), 1), "%",
            "  threshold=", GetMoveThreshold(), "%");
   }
}

bool IsParabolicSetup()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(g_maHandle, 0, 0, InpMALookback, ma) < InpMALookback)
      return false;

   double closeD1[];
   ArraySetAsSeries(closeD1, true);
   if(CopyClose(_Symbol, PERIOD_D1, 0, InpMALookback, closeD1) < InpMALookback)
      return false;

   // Find most recent D1 bar where price was at/below the 20MA (last base)
   int touchBar = -1;
   for(int i = 1; i < InpMALookback - 1; i++)
   {
      if(closeD1[i] <= ma[i])
      {
         touchBar = i;
         break;
      }
   }
   if(touchBar < 0) return false;

   double baseClose = closeD1[touchBar];
   if(baseClose <= 0) return false;

   double movePct = ((closeD1[0] - baseClose) / baseClose) * 100.0;
   return (movePct >= GetMoveThreshold());
}

double CalcMovePct()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(g_maHandle, 0, 0, InpMALookback, ma) < InpMALookback) return 0;

   double closeD1[];
   ArraySetAsSeries(closeD1, true);
   if(CopyClose(_Symbol, PERIOD_D1, 0, InpMALookback, closeD1) < InpMALookback) return 0;

   for(int i = 1; i < InpMALookback - 1; i++)
   {
      if(closeD1[i] <= ma[i] && closeD1[i] > 0)
         return ((closeD1[0] - closeD1[i]) / closeD1[i]) * 100.0;
   }
   return 0;
}

double GetMoveThreshold()
{
   switch(InpCapCategory)
   {
      case CAP_MID:   return InpMidCapPct;
      case CAP_LARGE: return InpLargeCapPct;
      default:        return InpSmallCapPct;
   }
}

//═══ Exhaustion Day ════════════════════════════════════════════════════════════
// Exhaustion is confirmed when price has gone below VWAP and stayed there.
// The playbook: "price moves below VWAP and is unable to reclaim it."
void ScanExhaustionStart()
{
   if(g_state != STATE_PARABOLIC) return;
   if(g_vwap <= 0 || !g_vwapCrossed) return;
   if(g_barsBelowVWAP < InpVWAPBelowBars) return;

   g_exhaustDate = g_dayStart;
   TransitionTo(STATE_EXHAUSTION);
   Print("ParabolicShort: EXHAUSTION DAY  date=", TimeToString(g_exhaustDate, TIME_DATE),
         "  dayHigh=", DoubleToString(g_dayHigh, _Digits),
         "  vwap=",    DoubleToString(g_vwap,    _Digits));
}

void CheckSetupExpiry()
{
   if(g_state != STATE_PARABOLIC || g_setupDate == 0) return;
   datetime curD1 = iTime(_Symbol, PERIOD_D1, 0);
   if((curD1 - g_setupDate) > (datetime)(InpMaxExhaustWaitDays * 86400))
   {
      Print("ParabolicShort: Exhaustion window expired → IDLE");
      TransitionTo(STATE_IDLE);
   }
}

void CheckExhaustionExpiry()
{
   if(g_state != STATE_EXHAUSTION) return;
   if(g_vwap <= 0) return;

   // Playbook: "If price reclaims VWAP strongly and continues higher, setup is no longer valid."
   double bid      = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double reclaimThreshold = g_vwap * (1.0 + InpVWAPReclaimPct / 100.0);
   if(bid > reclaimThreshold && g_barsBelowVWAP == 0)
   {
      Print("ParabolicShort: VWAP strongly reclaimed (bid=", bid,
            " > threshold=", reclaimThreshold, ") → setup INVALID → IDLE");
      ResetAll();
   }
}

//═══ Structure Tracking ════════════════════════════════════════════════════════
// Append the just-completed bar's H/L to the rolling structure array.
void AppendStructureBar()
{
   if(g_sCnt >= STRUCT_SIZE - 1) return;

   g_sHigh[g_sCnt] = iHigh(_Symbol, PERIOD_CURRENT, 1);
   g_sLow[g_sCnt]  = iLow(_Symbol,  PERIOD_CURRENT, 1);
   g_sCnt++;
}

// Lower high AND lower low vs. previous bar → structure is breaking down
bool HasLowerHighAndLow()
{
   if(g_sCnt < 2) return false;
   int n = g_sCnt - 1;
   return (g_sHigh[n] < g_sHigh[n-1] && g_sLow[n] < g_sLow[n-1]);
}

// Walk back to find the most recent local high that is below the day high
double GetRecentLowerHigh()
{
   for(int i = g_sCnt - 1; i >= 0; i--)
   {
      if(g_sHigh[i] > 0 && g_sHigh[i] < g_dayHigh)
         return g_sHigh[i];
   }
   return g_dayHigh;  // fallback: use day high
}

//═══ Entry Signals ═════════════════════════════════════════════════════════════
void ScanEntrySignal()
{
   if(g_state  != STATE_EXHAUSTION) return;
   if(g_vwap   <= 0)                return;
   if(!g_vwapCrossed)               return;  // price must have gone below VWAP first

   switch(InpEntryType)
   {
      case ENTRY_VWAP_REJECTION:  CheckVWAPRejection();  break;
      case ENTRY_VWAP_LOSS:       CheckVWAPLoss();        break;
      case ENTRY_BREAK_STRUCTURE: CheckBreakStructure();  break;
   }
}

//  Best entry: completed bar tagged VWAP from below, closed back below → rejection confirmed
void CheckVWAPRejection()
{
   if(g_barsBelowVWAP < 1) return;  // Must already be in below-VWAP territory

   double band  = g_vwap * (InpVWAPBandPct / 100.0);
   double barHi = iHigh(_Symbol,  PERIOD_CURRENT, 1);
   double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
   double barOp = iOpen(_Symbol,  PERIOD_CURRENT, 1);

   // Bar opened below VWAP, pushed up to tag it (within band), closed back below
   bool openedBelow  = (barOp  <  g_vwap);
   bool taggedVWAP   = (barHi  >= g_vwap - band);
   bool closedBelow  = (barCl  <  g_vwap);

   if(openedBelow && taggedVWAP && closedBelow)
      ExecuteShort("VWAP_REJECTION");
}

// Stronger than BOS alone: structure breaking down AND price closes below VWAP
void CheckVWAPLoss()
{
   double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
   double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(barCl < g_vwap && bid < g_vwap && HasLowerHighAndLow())
      ExecuteShort("VWAP_LOSS");
}

// Earliest entry: first lower high + lower low, price already below VWAP
void CheckBreakStructure()
{
   if(!HasLowerHighAndLow()) return;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid >= g_vwap) return;  // Require price to already be below VWAP
   ExecuteShort("BREAK_STRUCTURE");
}

//═══ Order Execution ═══════════════════════════════════════════════════════════
void ExecuteShort(const string reason)
{
   if(g_entryFired || g_attemptsToday >= InpMaxAttempts || HasOpenPosition()) return;

   double anchor = (InpStopType == STOP_RECENT_LOWER_HIGH)
                   ? GetRecentLowerHigh()
                   : g_dayHigh;
   if(anchor <= 0) return;

   double bid      = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double stopPx   = anchor * (1.0 + InpStopBufferPct / 100.0);

   if(stopPx <= bid)
   {
      Print("ParabolicShort: Entry skipped (", reason, ") stop=", stopPx,
            " must be > bid=", bid);
      return;
   }

   double riskDist = stopPx - bid;
   double tp1Px    = bid - (riskDist * InpTP1Ratio);
   double lots     = CalcLots(bid, stopPx, InpRiskPercent);
   if(lots <= 0) return;

   if(g_trade.Sell(lots, _Symbol, bid, stopPx, 0, "PS:" + reason))
   {
      g_entryPrice    = bid;
      g_stopPrice     = stopPx;
      g_tp1Price      = tp1Px;
      g_tp1Hit        = false;
      g_stopAtBE      = false;
      g_riskFreeAdded = false;
      g_entryFired    = true;
      g_attemptsToday++;

      TransitionTo(STATE_IN_TRADE);

      Print("ParabolicShort: SHORT opened (", reason, ")",
            "  lots=",  DoubleToString(lots, 2),
            "  entry=", DoubleToString(bid,    _Digits),
            "  stop=",  DoubleToString(stopPx, _Digits),
            "  tp1=",   DoubleToString(tp1Px,  _Digits),
            "  attempt=", g_attemptsToday, "/", InpMaxAttempts);
   }
   else
   {
      Print("ParabolicShort: Order FAILED (", reason, ")  err=", GetLastError());
   }
}

//═══ Position Management ═══════════════════════════════════════════════════════
void ManageOpenPosition()
{
   if(!HasOpenPosition())
   {
      // Position closed externally (stop hit or manual) — allow 2nd attempt
      if(g_attemptsToday < InpMaxAttempts)
      {
         Print("ParabolicShort: Position closed. Attempt ", g_attemptsToday,
               "/", InpMaxAttempts, " — back to EXHAUSTION state.");
         g_entryFired = false;
         TransitionTo(STATE_EXHAUSTION);
      }
      else
      {
         Print("ParabolicShort: All ", InpMaxAttempts, " attempts used → IDLE");
         ResetAll();
      }
      return;
   }

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // ─── TP1 partial close ────────────────────────────────────────
   if(!g_tp1Hit && g_tp1Price > 0 && bid <= g_tp1Price)
   {
      double pct = InpTP1ClosePct / 100.0;
      if(DoPartialClose(pct))
      {
         g_tp1Hit = true;
         Print("ParabolicShort: TP1 hit  price=", bid, "  closed ", InpTP1ClosePct, "%");

         if(InpMoveStopToBE)
            MoveToBE();
      }
   }

   // ─── Risk-free add ────────────────────────────────────────────
   // Playbook: after stop at BE + TP1 taken, add on 2nd VWAP rejection
   if(InpRiskFreeAdd && g_tp1Hit && g_stopAtBE && !g_riskFreeAdded && g_vwap > 0)
   {
      double band  = g_vwap * (InpVWAPBandPct / 100.0);
      double barHi = iHigh(_Symbol,  PERIOD_CURRENT, 1);
      double barCl = iClose(_Symbol, PERIOD_CURRENT, 1);
      double barOp = iOpen(_Symbol,  PERIOD_CURRENT, 1);

      bool openedBelow = (barOp <  g_vwap);
      bool taggedVWAP  = (barHi >= g_vwap - band);
      bool closedBelow = (barCl <  g_vwap);

      if(openedBelow && taggedVWAP && closedBelow)
      {
         // Stop for the add = break-even price (entry); no new risk if stop at BE
         double addLots = CalcLots(bid, g_entryPrice, InpRFAddRiskPct);
         if(addLots > 0 && g_trade.Sell(addLots, _Symbol, bid, g_entryPrice, 0, "PS:RF_ADD"))
         {
            g_riskFreeAdded = true;
            Print("ParabolicShort: Risk-free ADD  lots=", addLots,
                  "  stop=BE(", g_entryPrice, ")");
         }
      }
   }
}

bool DoPartialClose(double fraction)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic()  != InpMagicNumber || g_pos.Symbol() != _Symbol) continue;
      if(g_pos.PositionType() != POSITION_TYPE_SELL) continue;

      double closeLots = NormalizeLots(g_pos.Volume() * fraction);
      double minLot    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      if(closeLots < minLot) return false;  // Remaining would be below minimum

      return g_trade.PositionClosePartial(g_pos.Ticket(), closeLots);
   }
   return false;
}

void MoveToBE()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic()  != InpMagicNumber || g_pos.Symbol() != _Symbol) continue;
      if(g_pos.PositionType() != POSITION_TYPE_SELL) continue;

      if(g_trade.PositionModify(g_pos.Ticket(), g_entryPrice, 0))
      {
         g_stopPrice = g_entryPrice;
         g_stopAtBE  = true;
         Print("ParabolicShort: Stop moved to BE=", DoubleToString(g_entryPrice, _Digits));
      }
      return;
   }
}

//═══ EOD Close ═════════════════════════════════════════════════════════════════
void CheckEODClose()
{
   if(!HasOpenPosition()) return;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.hour > InpEODCloseHour ||
      (dt.hour == InpEODCloseHour && dt.min >= InpEODCloseMinute))
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         if(!g_pos.SelectByIndex(i)) continue;
         if(g_pos.Magic() != InpMagicNumber || g_pos.Symbol() != _Symbol) continue;
         g_trade.PositionClose(g_pos.Ticket());
         Print("ParabolicShort: EOD close  ticket=", g_pos.Ticket());
      }
      ResetAll();
   }
}

//═══ Lot Sizing ════════════════════════════════════════════════════════════════
double CalcLots(double entry, double stop, double riskPct)
{
   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmt  = balance * riskPct / 100.0;
   double stopDist = MathAbs(stop - entry);
   if(stopDist <= 0) return 0;

   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0 || tickVal <= 0) return 0;

   double lots = riskAmt / (stopDist * (tickVal / tickSize));
   return NormalizeLots(lots);
}

double NormalizeLots(double lots)
{
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

   lots = MathFloor(lots / step) * step;
   lots = MathMax(lots, minLot);
   if(InpMaxLotSize > 0) lots = MathMin(lots, InpMaxLotSize);
   return MathMin(lots, maxLot);
}

//═══ Helpers ═══════════════════════════════════════════════════════════════════
bool HasOpenPosition()
{
   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(g_pos.SelectByIndex(i) &&
         g_pos.Magic()  == InpMagicNumber &&
         g_pos.Symbol() == _Symbol)
         return true;
   }
   return false;
}

void TransitionTo(ENUM_EA_STATE newState)
{
   if(g_state == newState) return;
   Print("ParabolicShort: [", EnumToString(g_state), "] → [", EnumToString(newState), "]");
   g_state = newState;
}

void ResetAll()
{
   TransitionTo(STATE_IDLE);
   g_setupDate      = 0;
   g_exhaustDate    = 0;
   g_entryFired     = false;
   g_attemptsToday  = 0;
   g_entryPrice     = 0;
   g_stopPrice      = 0;
   g_tp1Price       = 0;
   g_tp1Hit         = false;
   g_stopAtBE       = false;
   g_riskFreeAdded  = false;
   g_sCnt           = 0;
   ArrayInitialize(g_sHigh, 0);
   ArrayInitialize(g_sLow,  0);
}

//═══ Dashboard ═════════════════════════════════════════════════════════════════
void DrawDashboard()
{
   string s = "══ ParabolicShort v1.0 ══════════════\n";
   s += StringFormat("Symbol : %s   TF: %s\n", _Symbol, EnumToString(Period()));
   s += StringFormat("State  : %s\n", EnumToString(g_state));
   s += StringFormat("Cap    : %s  (≥%.0f%%)\n", EnumToString(InpCapCategory), GetMoveThreshold());

   if(g_state >= STATE_PARABOLIC)
      s += StringFormat("Move   : %.1f%%  (setup %s)\n",
                         CalcMovePct(), TimeToString(g_setupDate, TIME_DATE));

   s += StringFormat("VWAP   : %s\n", g_vwap > 0 ? DoubleToString(g_vwap, _Digits) : "—");
   s += StringFormat("Day Hi : %s\n", DoubleToString(g_dayHigh, _Digits));
   s += StringFormat("< VWAP : %d bars   crossed: %s\n",
                      g_barsBelowVWAP, g_vwapCrossed ? "yes" : "no");

   if(g_state == STATE_EXHAUSTION)
   {
      s += StringFormat("Struct : %d bars  LH+LL: %s\n",
                         g_sCnt, HasLowerHighAndLow() ? "YES" : "no");
      s += StringFormat("Entry  : %s\n", EnumToString(InpEntryType));
      s += StringFormat("Attempt: %d / %d\n", g_attemptsToday, InpMaxAttempts);
   }

   if(g_state == STATE_IN_TRADE)
   {
      s += StringFormat("Entry  : %s\n", DoubleToString(g_entryPrice, _Digits));
      s += StringFormat("Stop   : %s%s\n", DoubleToString(g_stopPrice, _Digits),
                         g_stopAtBE ? "  [BE]" : "");
      s += StringFormat("TP1    : %s%s\n", DoubleToString(g_tp1Price, _Digits),
                         g_tp1Hit ? "  [hit]" : "");
      s += StringFormat("RF Add : %s\n", g_riskFreeAdded ? "done" :
                         (g_tp1Hit && g_stopAtBE ? "watching" : "—"));
      s += StringFormat("Attempt: %d / %d\n", g_attemptsToday, InpMaxAttempts);
   }

   Comment(s);
}
