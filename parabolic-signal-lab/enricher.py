"""
OctagonAI Enrichment Layer  -  Parabolic Short Strategy
=======================================================
Takes the top candidates from screener.py and enriches each one with
OctagonAI MCP data to answer the key question:

  "Is this parabolic move narrative/hype-driven or fundamentals-driven?"

Hype-driven moves (AI theme, quantum computing, meme, IPO speculation) tend
to reverse faster and more aggressively than fundamentals-driven runs per the
playbook ("Stocks driven by hype or strong themes tend to drop 20-40% or more
in a single day").

HOW OCTAGON ENRICHMENT WORKS
-----------------------------
OctagonAI runs through Claude Code's MCP protocol  -  it is NOT called directly
from Python. Instead, this module generates structured prompts that you paste
into Claude Code (or run via /financial-analyst-master) to get enrichment.

For automated enrichment, the module also outputs a ready-to-run Claude Code
command file that invokes /financial-analyst-master for each candidate.

OCTAGON TOOLS USED PER CANDIDATE
----------------------------------
  stock-price-change      -> Confirm the % move over 1M/3M/6M
  company-market-cap      -> Confirm cap classification + Small/Mid/Large label
  earnings-call-insights  -> Was the run driven by guidance, hype, or theme?
  earnings-conf-call-sentiment -> Management tone (overconfident = bearish signal)
  sec-8k-analysis         -> Recent material events (merger, secondary offering, etc.)
  sec-risk-factors        -> Key risks that could accelerate reversal
  analyst-estimates       -> Consensus vs. current price (stretched = reversal likely)

Usage:
  python enricher.py --candidates output/parabolic_candidates_YYYYMMDD.csv
  python enricher.py --tickers SMCI NVDA MSTR --top 5
"""

import argparse
import csv
import datetime
import json
from pathlib import Path


# --- Octagon skill mapping -----------------------------------------------------
# These are the OctagonAI MCP tools most relevant to parabolic short analysis.
# Ordered by usefulness for this strategy.
OCTAGON_SKILLS = {
    "stock-price-change": "Confirms multi-period % move (1M/3M/6M/1Y). "
                          "Validates the parabolic run size.",
    "company-market-cap": "Confirms market cap + Small/Mid/Large-cap classification. "
                          "Sets the correct move threshold.",
    "earnings-call-insights": "Extracts guidance, strategic priorities, and management "
                               "commentary from the latest earnings call. "
                               "Key: is the run driven by durable guidance or hype?",
    "earnings-conf-call-sentiment": "Analyses management tone and confidence level. "
                                     "Overconfident tone near a parabolic peak = bearish.",
    "sec-8k-analysis": "Flags recent material events: secondary offerings, "
                        "insider selling, restatements, guidance cuts.",
    "sec-risk-factors": "Extracts and categorises key risks from the latest 10-K/10-Q. "
                         "High leverage + high short interest = faster reversal.",
    "analyst-estimates": "Consensus revenue/EPS estimates vs. current valuation. "
                          "Price far above consensus -> stretched, reversal likely.",
    "financial-health-scores": "Altman Z-Score (bankruptcy risk) and Piotroski Score. "
                                 "Low scores on a parabolic stock = fragile move.",
}

CATEGORY_CONTEXT = {
    "MARKET_LEADER": (
        "MARKET LEADER (Large-cap >=$10B, >=50% threshold). "
        "These stocks usually reverse 10-20%. "
        "Focus on whether the move is narrative-driven vs. justified by earnings revision."
    ),
    "THEMATIC": (
        "THEMATIC MID-CAP ($2-10B, >=100% threshold). "
        "Sector/theme runners (AI, quantum, biotech, EV). "
        "These reverse 20-40%. Check if the theme catalyst is durable or sentiment-driven."
    ),
    "IPO_SMALL_CAP": (
        "IPO / SMALL-CAP (<$2B, >=200% threshold). "
        "Highest volatility, most aggressive reversals (40%+ in a day possible). "
        "Check float, short interest, and whether the move is purely speculative."
    ),
}


# --- Enrichment prompt builder ------------------------------------------------
def build_enrichment_prompt(candidate: dict) -> str:
    ticker   = candidate["ticker"]
    category = candidate.get("category", "THEMATIC")
    move_pct = candidate.get("move_pct", "?")
    cap_b    = candidate.get("mkt_cap_b", "?")
    sector   = candidate.get("sector", "?")
    ctx      = CATEGORY_CONTEXT.get(category, "")

    tools_str = "\n".join(
        f"  {i+1}. {tool}: {desc}"
        for i, (tool, desc) in enumerate(OCTAGON_SKILLS.items())
    )

    return f"""
/financial-analyst-master {ticker} {category.lower().replace('_', '-')}

CONTEXT FOR THIS ANALYSIS
--------------------------
Ticker    : {ticker}
Category  : {category}  ({ctx})
Market Cap: ${cap_b}B
Sector    : {sector}
Move      : +{move_pct}% from last 20MA base touch
Strategy  : Parabolic Short  -  looking for exhaustion and reversal

OCTAGON TOOLS TO PRIORITISE (in order)
----------------------------------------
{tools_str}

SPECIFIC QUESTIONS TO ANSWER
------------------------------
1. Is the parabolic move driven by durable fundamentals or hype/narrative?
2. What was the catalyst? Is it still active or has it been priced in?
3. What is the most recent earnings call sentiment? Any overconfidence?
4. Are there any 8-K material events that could trigger or accelerate a reversal?
5. Is the stock stretched relative to analyst consensus? By how much?
6. What is the Altman Z-Score? Does the balance sheet support this valuation?
7. Expected reversal magnitude based on stock type: __%

OUTPUT THE STANDARD PARABOLIC SHORT ASSESSMENT REPORT FORMAT.
""".strip()


def build_claude_command_file(candidates: list[dict], out_path: Path) -> None:
    """
    Generates a markdown file with ready-to-run Claude Code /financial-analyst-master
    commands for each candidate, sorted by move % descending.
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    lines = [
        f"# OctagonAI Enrichment Commands  -  {today}",
        "",
        "Paste each command into Claude Code to run the OctagonAI enrichment.",
        "Commands are ordered by move % (highest conviction first).",
        "",
        "---",
        "",
    ]

    sorted_cands = sorted(candidates, key=lambda c: -float(c.get("move_pct", 0)))

    for i, c in enumerate(sorted_cands, 1):
        ticker   = c["ticker"]
        category = c.get("category", "?")
        move_pct = c.get("move_pct", "?")
        cap_b    = c.get("mkt_cap_b", "?")
        sector   = c.get("sector", "?")

        lines += [
            f"## {i}. {ticker}  |  {category}  |  +{move_pct}%  |  ${cap_b}B  |  {sector}",
            "",
            "```",
            build_enrichment_prompt(c),
            "```",
            "",
        ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"Claude commands: {out_path}")


# --- Summary table -------------------------------------------------------------
def print_enrichment_plan(candidates: list[dict]) -> None:
    print(f"\n{'='*70}")
    print("  OCTAGON ENRICHMENT PLAN")
    print(f"{'='*70}")
    print(f"  {'#':<4} {'Ticker':<8} {'Category':<18} {'Move%':>7} {'Cap $B':>8} {'Sector'}")
    print(f"  {'-'*64}")

    sorted_cands = sorted(candidates, key=lambda c: -float(c.get("move_pct", 0)))
    for i, c in enumerate(sorted_cands, 1):
        cap_str = f"${float(c['mkt_cap_b']):.1f}" if c.get("mkt_cap_b") else "n/a"
        print(
            f"  {i:<4} {c['ticker']:<8} {c.get('category','?'):<18} "
            f"{float(c.get('move_pct',0)):>6.1f}% {cap_str:>8}  {c.get('sector','')}"
        )

    print(f"  {'='*64}")
    print(f"\n  {len(candidates)} candidates queued for OctagonAI enrichment")
    print(f"\n  Octagon tools to run per candidate:")
    for tool, desc in OCTAGON_SKILLS.items():
        print(f"    - {tool}")
    print(f"\n  Run each command with:  /financial-analyst-master <TICKER>")
    print(f"  Or use the generated commands file (see output)")


# --- CLI ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate OctagonAI enrichment commands")
    p.add_argument("--candidates", help="CSV from screener.py")
    p.add_argument("--tickers",    nargs="+", help="Specific tickers")
    p.add_argument("--top",        type=int, default=20,
                   help="Enrich top N candidates by move pct (default: 20)")
    p.add_argument("--out-dir",    default="output")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.tickers:
        candidates = [
            {"ticker": t, "category": "THEMATIC", "move_pct": "100",
             "mkt_cap_b": None, "sector": ""}
            for t in args.tickers
        ]
    elif args.candidates:
        with open(args.candidates) as f:
            candidates = list(csv.DictReader(f))
    else:
        print("Provide --candidates CSV or --tickers list.")
        return

    # Take top N by move %
    candidates = sorted(candidates, key=lambda c: -float(c.get("move_pct", 0)))
    candidates = candidates[:args.top]

    print_enrichment_plan(candidates)

    today     = datetime.date.today().strftime("%Y%m%d")
    out_dir   = Path(args.out_dir)
    cmd_file  = out_dir / f"octagon_commands_{today}.md"
    build_claude_command_file(candidates, cmd_file)

    print(f"\nNext steps:")
    print(f"  1. Open Claude Code")
    print(f"  2. Make sure OCTAGON_API_KEY is set in .env")
    print(f"  3. Paste commands from: {cmd_file}")
    print(f"  4. Run /financial-analyst-master for each top candidate")


if __name__ == "__main__":
    main()
