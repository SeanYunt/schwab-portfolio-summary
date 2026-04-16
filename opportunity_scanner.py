"""
opportunity_scanner.py
======================
Scans large-cap blue-chip stocks for buying opportunities where a significant
dip from the 52-week high appears to stem from a temporary, non-structural
disruption — labor disputes, transient cost spikes, sentiment bleed, etc.

This is a companion to morning_briefing.py but serves a different purpose:
finding stocks NOT currently owned that have dipped for recoverable reasons.

Run on-demand or conditionally when the market is down:
    python opportunity_scanner.py
    python opportunity_scanner.py --if-market-down 2.0
    python opportunity_scanner.py --dip-threshold 8.0 --no-email

News is sourced from SearXNG (same pipeline as messages-to-midnight). Set
SEARXNG_URL and/or SEARXNG_INTERNAL_URL in .env to point at your instance.
"""

import json
import os
import sys
import smtplib
import argparse
from datetime import datetime
from email.mime.text import MIMEText
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

import anthropic
import yfinance as yf
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Large-cap universe — S&P 100 plus select additions
# Market-cap floor enforced at runtime; this is the candidate pool.
# ---------------------------------------------------------------------------

LARGE_CAP_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "CSCO", "IBM", "INTC", "AMD",
    "TXN", "QCOM", "ADBE", "ACN", "NOW", "INTU",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "LOW", "MCD", "SBUX", "NKE", "TGT", "COST", "WMT",
    "BKNG", "MAR", "HLT", "YUM", "CMG", "TJX", "GM", "F",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "AXP", "V", "MA",
    "USB", "PNC", "TFC", "COF", "SCHW", "CB", "PGR", "MET", "AFL", "BK",
    # Healthcare
    "JNJ", "LLY", "UNH", "ABBV", "PFE", "MRK", "TMO", "ABT", "BMY", "AMGN",
    "GILD", "CVS", "CI", "HUM", "DHR", "SYK", "BSX", "MDT",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY",
    # Industrials
    "CAT", "DE", "HON", "GE", "MMM", "RTX", "LMT", "NOC", "BA", "UPS",
    "FDX", "CSX", "NSC", "UNP", "EMR", "ETN", "ITW", "GD",
    # Communication Services
    "GOOGL", "META", "T", "VZ", "CMCSA", "DIS", "NFLX",
    # Consumer Staples
    "PG", "KO", "PEP", "MDLZ", "GIS", "MO", "PM", "CL", "KMB", "EL",
    # Materials
    "LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DOW",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE",
    # Real Estate
    "PLD", "AMT", "EQIX", "CCI", "SPG", "O",
]

MIN_MARKET_CAP = 10e9       # $10B floor — filters micro/small-cap outliers
DIP_THRESHOLD_PCT = 10.0    # % below 52-week high to qualify
MAX_CANDIDATES = 15         # caps candidates sent to Claude to control cost/latency
NEWS_ARTICLES_PER_TICKER = 5

SEARXNG_TIMEOUT = 15
_REQUEST_HEADERS = {"User-Agent": "OpportunityScanner/1.0"}


# ---------------------------------------------------------------------------
# SearXNG helpers (mirrors scan.py pattern)
# ---------------------------------------------------------------------------

def _searxng_urls() -> list[str]:
    public = os.environ.get("SEARXNG_URL", "")
    internal = os.environ.get("SEARXNG_INTERNAL_URL", "http://localhost:8080")
    return [u for u in [public, internal] if u]


def _search_one(query: str, base_url: str) -> list[dict] | None:
    """Return results list, or None on connection failure (to trigger fallback)."""
    params = urlencode({
        "q": query,
        "format": "json",
        "time_range": "month",
        "language": "en",
    })
    req = Request(f"{base_url}/search?{params}", headers=_REQUEST_HEADERS)
    try:
        resp = urlopen(req, timeout=SEARXNG_TIMEOUT)
        return json.loads(resp.read()).get("results", [])
    except (URLError, HTTPError, json.JSONDecodeError, OSError):
        return None


def fetch_news(ticker: str, company_name: str) -> list[dict]:
    """Fetch recent news articles for a ticker via SearXNG with URL fallback."""
    query = f'"{company_name}" {ticker} stock'
    for base_url in _searxng_urls():
        results = _search_one(query, base_url)
        if results is not None:
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("content", "")[:300],
                    "date": (r.get("publishedDate") or r.get("pubdate") or "")[:10],
                    "url": r.get("url", ""),
                }
                for r in results[:NEWS_ARTICLES_PER_TICKER]
            ]
    return []


# ---------------------------------------------------------------------------
# Price screening
# ---------------------------------------------------------------------------

def screen_dipped_stocks(threshold_pct: float) -> list[dict]:
    """
    Screen LARGE_CAP_UNIVERSE for stocks >= threshold_pct below their 52-week high.
    Returns candidates sorted by depth of dip (deepest first), capped at MAX_CANDIDATES.
    """
    print(f"Screening {len(LARGE_CAP_UNIVERSE)} tickers (dip >= {threshold_pct:.0f}% from 52w high)...")
    candidates = []

    for ticker in LARGE_CAP_UNIVERSE:
        try:
            info = yf.Ticker(ticker).fast_info
            high_52w = info.year_high
            current = info.last_price
            market_cap = info.market_cap

            if not all([high_52w, current, market_cap]):
                continue
            if market_cap < MIN_MARKET_CAP:
                continue

            pct_from_high = ((current - high_52w) / high_52w) * 100
            if pct_from_high <= -threshold_pct:
                candidates.append({
                    "ticker": ticker,
                    "current_price": current,
                    "high_52w": high_52w,
                    "pct_from_high": pct_from_high,
                    "market_cap_b": market_cap / 1e9,
                })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["pct_from_high"])  # deepest dip first
    print(f"  Found {len(candidates)} candidates, analyzing top {min(len(candidates), MAX_CANDIDATES)}")
    return candidates[:MAX_CANDIDATES]


def market_change_today() -> float:
    """Return SPY's % change vs previous close. Negative = market is down."""
    try:
        info = yf.Ticker("SPY").fast_info
        return ((info.last_price - info.previous_close) / info.previous_close) * 100
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def _build_analysis_prompt(candidates: list[dict]) -> str:
    sections = []
    for c in candidates:
        news_lines = "\n".join(
            f"  [{a['date'] or 'n/d'}] {a['title']}"
            + (f"\n    {a['snippet']}" if a["snippet"] else "")
            for a in c["news"]
        ) or "  (no recent news found)"

        sections.append(
            f"### {c['ticker']} — {c['name']} (${c['market_cap_b']:.0f}B mkt cap)\n"
            f"Current: ${c['current_price']:.2f} | 52w high: ${c['high_52w']:.2f} | "
            f"Dip: {c['pct_from_high']:.1f}%\n\n"
            f"Recent news:\n{news_lines}"
        )

    return f"""You are a value-oriented equity analyst screening large-cap blue-chip stocks that have pulled back significantly from their 52-week highs.

For each stock below, reason about whether the dip is:

RECOVERABLE — temporary, non-structural disruption:
  - Labor strike or work stoppage
  - One bad quarter from an identifiable, non-recurring cost spike
  - Weather, logistics, or supply-chain disruption
  - Regulatory uncertainty pending a known resolution
  - Sentiment bleed from unrelated sector news or macro fear
  - Short-term input cost pressure without underlying demand destruction

STRUCTURAL — lasting damage that changes the fundamental thesis:
  - Demand erosion or secular volume decline
  - Competitive disruption or market share loss to a new entrant
  - Management integrity or governance problems
  - Balance sheet deterioration (excessive leverage, liquidity concerns)
  - Product obsolescence or technology displacement
  - Regulatory action directly threatening the core business model

For each RECOVERABLE stock — or where evidence is insufficient to rule it out — output exactly this block:

---
TICKER: {{ticker}}
VERDICT: RECOVERABLE | UNCLEAR
CATALYST: [one sentence — the specific event or news driving the dip]
RATIONALE: [one paragraph — why the disruption is likely temporary and how it resolves]
TIMEFRAME: [expected resolution window, e.g., "1–2 quarters", "next earnings cycle"]
---

For STRUCTURAL cases, output a single line:
TICKER: {{ticker}} — STRUCTURAL: [one-sentence reason]

If none qualify as recoverable or unclear, say so explicitly. Make a call — do not hedge every sentence.

--- STOCKS TO ANALYZE ---

{chr(10).join(sections)}"""


def analyze_candidates(candidates: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    print("Requesting Claude analysis...")
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=6000,
        messages=[{"role": "user", "content": _build_analysis_prompt(candidates)}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def _format_body(candidates: list[dict], analysis: str, spy_pct: float, threshold_pct: float) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    screened = "\n".join(
        f"  {c['ticker']:<6}  {c['pct_from_high']:+.1f}%  "
        f"${c['current_price']:.2f}  (52w high ${c['high_52w']:.2f})  {c['name']}"
        for c in candidates
    )
    return (
        f"Opportunity Scanner — {today}\n"
        f"SPY: {spy_pct:+.2f}%  |  Dip threshold: {threshold_pct:.0f}%  |  Candidates: {len(candidates)}\n"
        f"\n"
        f"{'—' * 52}\n"
        f"SCREENED DIPS (≥{threshold_pct:.0f}% below 52-week high)\n"
        f"{'—' * 52}\n"
        f"{screened}\n"
        f"\n"
        f"{'—' * 52}\n"
        f"CLAUDE'S ASSESSMENT\n"
        f"{'—' * 52}\n"
        f"{analysis}\n"
        f"\n"
        f"Generated {datetime.now().strftime('%H:%M')} | opportunity_scanner.py\n"
    )


def send_email(subject: str, body: str) -> None:
    sender = os.environ["GMAIL_SENDER"]
    recipient = os.environ["GMAIL_RECIPIENT"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)

    print(f"Results emailed to {recipient}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Scan large-cap stocks for recoverable buying opportunities"
    )
    parser.add_argument(
        "--if-market-down",
        type=float,
        metavar="PCT",
        help="Only run if SPY is down at least PCT%% today (e.g., 2.0)",
    )
    parser.add_argument(
        "--dip-threshold",
        type=float,
        default=DIP_THRESHOLD_PCT,
        metavar="PCT",
        help=f"Min %% below 52-week high to flag a stock (default: {DIP_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Print output to stdout instead of sending email",
    )
    args = parser.parse_args()

    # Conditional market trigger
    spy_pct = market_change_today()
    if args.if_market_down and spy_pct > -args.if_market_down:
        print(f"SPY {spy_pct:+.2f}% — not down enough (threshold -{args.if_market_down:.1f}%). Exiting.")
        sys.exit(0)

    print(f"SPY today: {spy_pct:+.2f}%")

    # Price screening
    candidates = screen_dipped_stocks(args.dip_threshold)
    if not candidates:
        print(f"No candidates with dip >= {args.dip_threshold:.0f}%. Nothing to report.")
        sys.exit(0)

    # Enrich with company name and news
    print("Fetching company names and news...")
    for c in candidates:
        try:
            c["name"] = yf.Ticker(c["ticker"]).info.get("shortName") or c["ticker"]
        except Exception:
            c["name"] = c["ticker"]
        c["news"] = fetch_news(c["ticker"], c["name"])
        print(f"  {c['ticker']}: {c['name']} — {len(c['news'])} articles")

    # Claude assessment
    analysis = analyze_candidates(candidates)

    # Deliver results
    today = datetime.now().strftime("%b %d")
    subject = (
        f"Opportunity Scanner | {today} | {len(candidates)} candidates | SPY {spy_pct:+.1f}%"
    )
    body = _format_body(candidates, analysis, spy_pct, args.dip_threshold)

    if args.no_email:
        print(f"\n{'=' * 60}\nSubject: {subject}\n{'=' * 60}\n{body}")
    else:
        send_email(subject, body)


if __name__ == "__main__":
    main()
