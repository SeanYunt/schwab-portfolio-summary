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
import time
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
NEWS_ARTICLES_PER_TICKER = 10  # raised from 5 — a bare "TICKER stock" query returns a mix of
                                # boilerplate and substantive articles; the decision-relevant
                                # ones (e.g. a lawsuit notice) can rank as low as #7-9 of 10
NEWS_FETCH_DELAY_S = 3.0    # pause between SearXNG queries — rapid-fire queries get engines rate-limit/CAPTCHA suspended

SEARXNG_TIMEOUT = 15
_REQUEST_HEADERS = {"User-Agent": "OpportunityScanner/1.0"}

NEWS_DEBUG = False  # set by --debug-news; prints per-request SearXNG diagnostics


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
        "categories": "news",  # default (general) category returns quote/overview pages,
                                # not news articles — confirmed empirically: general search
                                # returned 1 genuine news item in 10 results (undated), news
                                # category returned 9 genuine news items in 9 (see conversation)
    })
    url = f"{base_url}/search?{params}"
    req = Request(url, headers=_REQUEST_HEADERS)
    try:
        resp = urlopen(req, timeout=SEARXNG_TIMEOUT)
        body = resp.read()
    except HTTPError as e:
        if NEWS_DEBUG:
            print(f"    [news] GET {url}")
            print(f"    [news] HTTP {e.code} {e.reason} | body: {e.read()[:300]!r}")
        return None
    except (URLError, OSError) as e:
        if NEWS_DEBUG:
            print(f"    [news] GET {url}")
            print(f"    [news] {type(e).__name__}: {e}")
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        if NEWS_DEBUG:
            print(f"    [news] GET {url}")
            print(f"    [news] JSONDecodeError: {e} | body: {body[:300]!r}")
        return None
    results = data.get("results", [])
    unresponsive = data.get("unresponsive_engines", [])
    if NEWS_DEBUG:
        print(f"    [news] GET {url}")
        print(
            f"    [news] HTTP {resp.status} | {len(results)} results | "
            f"unresponsive_engines={unresponsive}"
        )
        if not results:
            print(f"    [news] empty result set; raw body (first 600 bytes): {body[:600]!r}")
    if not results and unresponsive:
        # Zero results with failed engines means the engines were rate-limited or
        # CAPTCHA-suspended, not that no news exists — treat as a failed fetch so
        # the caller falls back / warns instead of silently reporting no news.
        return None
    return results


def fetch_news(ticker: str) -> tuple[list[dict], bool]:
    """Fetch recent news articles for a ticker via SearXNG with URL fallback.

    Returns (articles, searxng_ok). searxng_ok is False when every configured URL
    fails — either a connection error, or an empty result set where all of the
    instance's engines were rate-limited/CAPTCHA-suspended. A genuine empty
    result set (engines responded, nothing found) returns ([], True).
    """
    # Deliberately excludes company_name: combined with time_range=month, an
    # exact-or-near company name match narrows the candidate page set enough
    # that the recency filter's intersection with it goes to zero on most
    # engines. "TICKER stock" alone stays broad enough to survive the filter
    # (confirmed empirically against the SearXNG instance — see conversation).
    query = f"{ticker} stock"
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
            ], True
    return [], False


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

        dip = c["pct_from_high"]
        if dip <= -35:
            tier = "TIER 3 (>35% dip — high skepticism; weigh the evidence and commit to a conviction-weighted verdict)"
        elif dip <= -20:
            tier = "TIER 2 (20–35% dip — elevated scrutiny; macro alone is insufficient)"
        else:
            tier = "TIER 1 (10–20% dip — standard screening)"

        sections.append(
            f"### {c['ticker']} — {c['name']} (${c['market_cap_b']:.0f}B mkt cap) [{tier}]\n"
            f"Current: ${c['current_price']:.2f} | 52w high: ${c['high_52w']:.2f} | "
            f"Dip: {c['pct_from_high']:.1f}%\n\n"
            f"Recent news:\n{news_lines}"
        )

    return f"""You are a skeptical value-oriented equity analyst screening large-cap stocks for genuine buying opportunities. Your job is to protect capital first, find opportunities second — but a refusal to judge is not protection. Every stock gets a directional verdict with a conviction level; uncertainty is expressed through conviction, not by defaulting to UNCLEAR.

Each stock is labeled with a dip magnitude tier. Apply the corresponding standard of evidence:

TIER 1 (10–20% dip): Standard screening. A credible temporary catalyst qualifies.
TIER 2 (20–35% dip): Elevated scrutiny. You must identify a specific, named catalyst — not general macro sentiment. A broad "risk-off selloff" does not qualify. The 52-week high may have been inflated; cheap-vs-the-high is not the same as cheap.
TIER 3 (>35% dip): High skepticism. A drawdown this deep often means the market knows something that hasn't fully surfaced in the news, and absence of bad news is NOT evidence of recoverability. But do not default to UNCLEAR: weigh the available evidence and commit to a conviction-weighted verdict. If the evidence leans toward a temporary cause, RECOVERABLE — LOW CONVICTION is a legitimate call; if it leans toward lasting damage, use LEANS STRUCTURAL.

VERDICT DEFINITIONS (five valid verdicts: RECOVERABLE, RECOVERABLE — LOW CONVICTION, UNCLEAR, LEANS STRUCTURAL, STRUCTURAL):

RECOVERABLE — a specific, identifiable, temporary disruption with a plausible resolution mechanism. Qualifying catalysts include:
  - Named labor strike or work stoppage with resolution path
  - One bad quarter from a specific, non-recurring cost item (name it)
  - Named weather, logistics, or supply-chain event with known end date
  - Specific regulatory uncertainty with a known decision timeline
  - Input cost pressure tied to a commodity with clear mean-reversion history
  - Guidance reset or one quarter of soft bookings with a named driver
  - Capped, quantifiable litigation exposure (not open-ended)
  - Temporary margin compression from a defined investment cycle (e.g., elevated capex with a stated timeline)
  - Analyst or market overreaction to a single data point, contradicted by other fundamentals
  - Forced or technical selling (index rebalance, large holder exit) distinct from fundamental deterioration
  DO NOT use this verdict for: vague "sentiment," sector rotation, or valuation compression with no named cause. Macro-driven dips are governed by the MACRO RULE below.
  State conviction as HIGH, MEDIUM, or LOW. RECOVERABLE — LOW CONVICTION means the evidence favors recovery but is incomplete — a directional lean, not a confident call.

UNCLEAR — reserve this for genuinely 50/50 cases: the evidence for recovery and for lasting damage is roughly balanced, or there is essentially no evidence either way. "Not 100% sure" is NOT grounds for UNCLEAR — every verdict carries uncertainty, and that belongs in the CONVICTION field. This remains a warning verdict: do not act until more information is available.

LEANS STRUCTURAL — the evidence points more toward lasting damage than recovery, but the case is not yet conclusive. Use this instead of UNCLEAR when you can articulate a specific structural concern that outweighs the recovery case.

STRUCTURAL — lasting damage that changes the fundamental thesis:
  - Demand erosion or secular volume decline
  - Competitive disruption or market share loss
  - Management or governance problems
  - Balance sheet deterioration
  - Product obsolescence or technology displacement
  - Regulatory action directly threatening the core business model

MACRO RULE: For a dip attributed to a broad market selloff or macro risk-off, evaluate three conditions: (1) you can name the specific macro event, (2) you can explain why it should reverse within your stated timeframe, (3) you can explain why this stock specifically recovers when it does. State how many of the three you can support, and set the verdict and conviction accordingly: all three supports RECOVERABLE up to HIGH conviction; two supports RECOVERABLE at MEDIUM or LOW; one supports at most RECOVERABLE — LOW CONVICTION or UNCLEAR; none means UNCLEAR or LEANS STRUCTURAL.

NO-FABRICATION RULE: Never introduce a specific number — price, market cap, share count, revenue, margin, or any other financial metric — that is not present in the input data below. If a missing number would materially affect your verdict, name it under MISSING_INFO. Do not estimate it, and do not recall it from training data.

Output format. Every verdict includes a CONVICTION line (HIGH / MEDIUM / LOW) so results can be sorted by confidence — for UNCLEAR and structural verdicts, conviction means confidence in that verdict itself.

For RECOVERABLE stocks:
---
TICKER: {{ticker}}
VERDICT: RECOVERABLE [or RECOVERABLE — LOW CONVICTION]
TIER: [1 / 2 / 3]
CONVICTION: [HIGH / MEDIUM / LOW — one sentence on what sets it at this level]
CATALYST: [one sentence — the specific named event driving the dip]
RECOVERY_MECHANISM: [what specifically changes to restore price, and what triggers that change]
RATIONALE: [one paragraph — why the disruption is temporary and bounded]
TIMEFRAME: [realistic window with justification — do not default to "1–2 quarters" without reasoning]
---

For UNCLEAR stocks:
---
TICKER: {{ticker}}
VERDICT: UNCLEAR — DO NOT ACT
TIER: [1 / 2 / 3]
CONVICTION: [HIGH / MEDIUM / LOW — how confident you are that this is genuinely undecidable rather than a lean you haven't committed to]
RISK: [what specifically is unknown, contradictory, or unexplained by the news]
MISSING_INFO: [what data or event would resolve the uncertainty — including any number excluded under the NO-FABRICATION RULE]
REVISIT: [what to watch — earnings date, regulatory decision, macro indicator]
---

For LEANS STRUCTURAL and STRUCTURAL stocks:
---
TICKER: {{ticker}}
VERDICT: [LEANS STRUCTURAL / STRUCTURAL]
TIER: [1 / 2 / 3]
CONVICTION: [HIGH / MEDIUM / LOW]
REASON: [one or two sentences — the specific lasting damage; for LEANS STRUCTURAL, also what evidence would flip the call]
---

If none qualify as recoverable, say so explicitly. Make a call — do not hedge every sentence, and do not let UNCLEAR absorb every hard case.

--- STOCKS TO ANALYZE ---

{chr(10).join(sections)}"""


def analyze_candidates(candidates: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    print("Requesting Claude analysis...")
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": _build_analysis_prompt(candidates)}],
    )
    return next(b.text for b in message.content if b.type == "text")


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
        f"{'—' * 52}\n"
        f"VERDICT LEGEND\n"
        f"{'—' * 52}\n"
        f"RECOVERABLE — specific, temporary disruption with a plausible resolution mechanism.\n"
        f"RECOVERABLE — LOW CONVICTION — evidence favors recovery but is incomplete; a directional lean, not a confident call.\n"
        f"UNCLEAR — genuinely 50/50: evidence for recovery and lasting damage is balanced, or there is no evidence either way. Do not act.\n"
        f"LEANS STRUCTURAL — evidence points more toward lasting damage than recovery, but the case is not conclusive.\n"
        f"STRUCTURAL — lasting damage to the fundamental thesis (demand erosion, share loss, governance, balance sheet, obsolescence).\n"
        f"CONVICTION — HIGH/MEDIUM/LOW confidence in the verdict itself; for UNCLEAR it means confidence the case is truly undecidable.\n"
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
    parser.add_argument(
        "--debug-news",
        action="store_true",
        help="Print per-request SearXNG diagnostics (URL, HTTP status, raw response on empty/error)",
    )
    args = parser.parse_args()

    if args.debug_news:
        global NEWS_DEBUG
        NEWS_DEBUG = True

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
    searxng_failures = 0
    for i, c in enumerate(candidates):
        try:
            info = yf.Ticker(c["ticker"]).info
            c["name"] = info.get("shortName") or c["ticker"]
            if NEWS_DEBUG:
                print(
                    f"    [name] {c['ticker']}: shortName={info.get('shortName')!r} "
                    f"longName={info.get('longName')!r} displayName={info.get('displayName')!r}"
                )
        except Exception as e:
            c["name"] = c["ticker"]
            if NEWS_DEBUG:
                print(f"    [name] {c['ticker']}: lookup failed — {type(e).__name__}: {e}")
        c["news"], searxng_ok = fetch_news(c["ticker"])
        if not searxng_ok:
            searxng_failures += 1
        print(f"  {c['ticker']}: {c['name']} — {len(c['news'])} articles")
        if i < len(candidates) - 1:
            time.sleep(NEWS_FETCH_DELAY_S)
    if searxng_failures:
        print(f"  WARNING: SearXNG fetch failed for {searxng_failures}/{len(candidates)} ticker(s) (unreachable, or all engines rate-limited/suspended) — Claude will analyze without news context")

    total_articles = sum(len(c["news"]) for c in candidates)
    print(f"News fetched: {total_articles} articles across {len(candidates)} candidates")
    if total_articles == 0:
        print("  WARNING: zero news articles for ALL candidates — SearXNG is returning empty results; re-run with --debug-news to see raw responses")

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
