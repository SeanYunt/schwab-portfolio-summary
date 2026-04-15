"""
Morning Briefing — combines Schwab portfolio data with the messages-to-midnight
news scan and generates a single cross-referenced email via Claude.

Run: python morning_briefing.py
"""

import os
import subprocess
import sys
import smtplib
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import yaml
import yfinance as yf

from portfolio_summary import get_schwab_client, fetch_portfolio, fetch_earnings_calendar, EARNINGS_LOOKAHEAD_DAYS

load_dotenv()

SCAN_SCRIPT = os.environ.get("SCAN_SCRIPT_PATH", "")
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# subjects.yaml auto-generation
# ---------------------------------------------------------------------------

PORTFOLIO_SENTINEL = "# ==> PORTFOLIO_SUBJECTS_START (auto-generated — do not edit below this line)"

# Symbols to skip — index funds, broad ETFs, cash equivalents
SKIP_SYMBOLS = {"SWPPX", "SWTSX", "SWLSX", "VTI", "SPY", "QQQ"}

# Symbols that belong to a single grouped topic entry (first symbol listed is the "primary")
SECTOR_GROUPS = [
    {
        "symbols": {"VST", "CEG", "NRG"},
        "topic": "Vistra VST Constellation Energy CEG NRG power demand earnings",
        "why": (
            "VST, CEG, and NRG are held positions in the power/utility sector. "
            "AI data center electricity demand, nuclear policy (IRA credits, permitting), "
            "and grid investment are the primary tailwinds to monitor."
        ),
    },
]

# Hand-crafted why fields for known holdings — used instead of generic yfinance descriptions
SUBJECT_OVERRIDES = {
    "CAT": {
        "topic": "Caterpillar CAT earnings guidance tariffs 2026",
        "why": (
            "CAT is a top holding. Tariff metal cost headwinds ($1.8B flagged) and upcoming "
            "earnings are the primary risk variables. Any guidance revision or margin commentary "
            "is directly actionable."
        ),
    },
    "RTX": {
        "topic": "RTX Raytheon Technologies earnings defense spending 2026",
        "why": (
            "RTX is a top holding. Defense budget trajectory, aerospace aftermarket demand, "
            "and any pre-earnings analyst commentary affect near-term positioning."
        ),
    },
    "FAST": {
        "topic": "Fastenal FAST industrial demand tariffs distribution",
        "why": (
            "FAST is a top holding and a bellwether for industrial activity. As a distributor "
            "of imported fasteners and hardware, it has direct tariff cost exposure and is "
            "sensitive to construction/manufacturing demand signals."
        ),
    },
    "RYCEY": {
        "topic": "Rolls-Royce RYCEY aerospace engines defense contracts revenue",
        "why": (
            "RYCEY is a held position. Key drivers are widebody engine flying hours "
            "(civil aerospace recovery), defense contract wins, and FX exposure. "
            "Any earnings or contract news is relevant."
        ),
    },
    "XOM": {
        "topic": "ExxonMobil XOM oil price energy earnings 2026",
        "why": (
            "XOM is a held position. Oil price direction, Strait of Hormuz/Iran supply "
            "disruption, and energy policy changes directly affect valuation."
        ),
    },
    "GLD": {
        "topic": "gold silver precious metals inflation Fed rate 2026",
        "why": (
            "GLD is a held position. Fed rate path, real yield direction, and dollar "
            "strength are the primary drivers of gold ETF performance."
        ),
    },
    "SLV": {
        "topic": "silver precious metals industrial demand Fed rate 2026",
        "why": (
            "SLV is a held position. Silver is driven by both monetary demand (real yields, "
            "dollar strength) and industrial demand (solar panels, EVs)."
        ),
    },
    "CPER": {
        "topic": "copper market supply demand outlook mining 2026",
        "why": (
            "CPER (copper futures ETF) is a held position. Supply constraints (water scarcity, "
            "ore depletion), China demand, and green energy buildout affect copper price direction."
        ),
    },
}


def _generate_generic_subject(symbol: str) -> dict:
    """Build a generic topic/why entry for a symbol using yfinance info."""
    try:
        info = yf.Ticker(symbol).info
        name = info.get("longName") or info.get("shortName") or symbol
        sector = info.get("sector", "")
        industry = info.get("industry", "")
        desc_parts = [p for p in [sector, industry] if p]
        context = f" ({', '.join(desc_parts)})" if desc_parts else ""
        return {
            "topic": f"{name} {symbol} earnings stock price 2026",
            "why": (
                f"{symbol} is a held position{context}. "
                "Monitor for earnings releases, analyst rating changes, and sector news "
                "that could affect share price."
            ),
        }
    except Exception:
        return {
            "topic": f"{symbol} stock price earnings news 2026",
            "why": f"{symbol} is a held position. Monitor for any material news.",
        }


def update_portfolio_subjects(portfolio: list) -> None:
    """
    Rewrite the auto-generated section of subjects.yaml to match current holdings.

    Reads everything above PORTFOLIO_SENTINEL from the file, then appends freshly
    generated entries for each equity position (grouped entries for sector clusters,
    overrides for known tickers, generic fallback for everything else).
    """
    if not SCAN_SCRIPT:
        return  # no scan configured — nothing to update

    subjects_path = Path(SCAN_SCRIPT).parent.parent / "references" / "subjects.yaml"
    if not subjects_path.exists():
        print(f"  subjects.yaml not found at {subjects_path} — skipping auto-update")
        return

    # Collect equity symbols from portfolio (skip index funds etc.)
    symbols = sorted(
        {
            pos["symbol"]
            for acct in portfolio
            for pos in acct["positions"]
            if pos["asset_type"] in ("EQUITY", "ETF", "")
            and pos["symbol"].isalpha()
            and pos["symbol"] not in SKIP_SYMBOLS
        }
    )

    # Read static portion (everything up to and including the sentinel)
    raw = subjects_path.read_text(encoding="utf-8")
    if PORTFOLIO_SENTINEL in raw:
        static_part = raw[: raw.index(PORTFOLIO_SENTINEL)]
    else:
        static_part = raw.rstrip() + "\n"

    # Build new portfolio entries
    new_entries = []
    grouped_symbols: set[str] = set()

    # Grouped sector entries (e.g. VST/CEG/NRG together)
    for group in SECTOR_GROUPS:
        held = group["symbols"] & set(symbols)
        if held:
            new_entries.append({"topic": group["topic"], "why": group["why"]})
            grouped_symbols |= held

    # Individual entries
    already_added: set[str] = set()
    for symbol in symbols:
        if symbol in grouped_symbols:
            continue
        if symbol in already_added:
            continue
        entry = SUBJECT_OVERRIDES.get(symbol) or _generate_generic_subject(symbol)
        new_entries.append(entry)
        already_added.add(symbol)

    # Serialize entries as a YAML sequence, then indent 2 spaces to nest under `subjects:`
    raw_yaml = yaml.dump(
        new_entries,
        allow_unicode=True,
        default_flow_style=False,
        width=10000,  # prevent line-wrapping that breaks indentation
    )
    portfolio_block = "\n".join(
        "  " + line if line.strip() else ""
        for line in raw_yaml.splitlines()
    )

    new_content = (
        static_part.rstrip("\n")
        + "\n"
        + PORTFOLIO_SENTINEL
        + "\n\n"
        + portfolio_block
        + "\n"
    )

    subjects_path.write_text(new_content, encoding="utf-8")
    print(f"  Updated subjects.yaml with {len(new_entries)} portfolio topic(s).")


# ---------------------------------------------------------------------------
# News scan
# ---------------------------------------------------------------------------

def run_news_scan() -> str:
    """Run scan.py and return its stdout output."""
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [PYTHON, SCAN_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            print(f"  Scan stderr: {result.stderr.strip()}")
            return f"News scan error:\n{result.stderr}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "News scan error: timed out after 120 seconds."
    except FileNotFoundError as e:
        return f"News scan error: could not launch scanner — {e}"


# ---------------------------------------------------------------------------
# Combined prompt
# ---------------------------------------------------------------------------

def build_briefing_prompt(portfolio: list, news: str, today: date, earnings: dict | None = None) -> str:
    total_value = sum(a["total_value"] for a in portfolio)
    total_day_pl = sum(a["day_pl"] for a in portfolio)

    # Build holdings summary for cross-reference
    holdings = []
    for acct in portfolio:
        for pos in acct["positions"]:
            holdings.append(
                f"  {pos['symbol']}: ${pos['market_value']:,.2f} "
                f"({pos['day_pl_pct']:+.2f}% today)"
            )

    # Normalize news section
    if "NO_NEW_RESULTS" in news:
        news_section = "No new news signals today. All recent articles were already reported in a previous scan."
    elif news.startswith("News scan error"):
        news_section = f"News scan unavailable: {news}"
    else:
        news_section = news

    lines = [
        f"Today is {today.strftime('%A, %B %d, %Y')}.",
        "",
        "=== PORTFOLIO SNAPSHOT ===",
        f"Total value: ${total_value:,.2f} | Day P&L: ${total_day_pl:+,.2f}",
        "",
        "Holdings:",
        *holdings,
        "",
        "=== NEWS SIGNALS ===",
        news_section,
        "",
        f"=== UPCOMING EARNINGS (next {EARNINGS_LOOKAHEAD_DAYS} days) ===",
        *(
            [f"  {s}: reports {d}" for s, d in sorted(earnings.items())]
            if earnings
            else ["  None within the lookahead window."]
        ),
        "",
        "=== YOUR TASK ===",
        "Write a unified morning briefing in flowing prose (no bullet points). Include:",
        "",
        "STRICT SOURCING RULE: You may only attribute a cause to a price move if a specific",
        "article in the NEWS SIGNALS section above directly supports it. If no article explains",
        "a holding's move, say so explicitly (e.g. 'no news signal available for X's decline').",
        "Do not invent macro narratives, infer causes, or fill gaps with plausible-sounding",
        "explanations. When citing a cause, name the source publication or outlet if it appears",
        "in the article metadata (e.g. 'per Reuters...' or 'according to a Wall Street Journal",
        "report in today's scan...'). Speculation is worse than silence.",
        "",
        "1. PORTFOLIO SUMMARY (2-3 sentences)",
        "   - Overall performance today",
        "   - Notable movers — state the move, but only explain the cause if a news article",
        "     above supports it; otherwise leave the cause unattributed",
        "",
        "2. NEWS & MARKET SIGNALS (2-3 sentences)",
        "   - Summarize the most important WARNING or CRITICAL news items from the scan",
        "   - Skip INFO-level items unless directly relevant to a holding",
        "   - Quote or closely paraphrase the article; do not editorialize beyond what it says",
        "",
        "3. CROSS-REFERENCE (1-3 sentences, only if a specific article supports the link)",
        "   - Connect a held position to a specific article from the scan",
        "   - Name the position, the article source, and whether it is a tailwind or headwind",
        "   - For holdings with upcoming earnings, note the date and what the market may be",
        "     watching — but only if a news article above gives concrete context",
        "   - If no articles connect meaningfully to holdings, omit this section entirely",
        "",
        "4. WATCH LIST (1 sentence)",
        "   - One specific, observable thing to monitor — price level, headline, or event",
        "",
        "Be direct and factual. No hype. Total length: 8-12 sentences.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def generate_briefing(portfolio: list, news: str, today: date, earnings: dict | None = None) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_briefing_prompt(portfolio, news, today, earnings)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str):
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

    print(f"Briefing emailed to {recipient}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    today = date.today()
    print(f"=== Morning Briefing — {today} ===")

    print("Fetching portfolio...")
    schwab_client = get_schwab_client()
    portfolio = fetch_portfolio(schwab_client)
    total_value = sum(a["total_value"] for a in portfolio)
    total_day_pl = sum(a["day_pl"] for a in portfolio)
    print(f"  ${total_value:,.2f} | Day P&L: ${total_day_pl:+,.2f}")

    print("Fetching earnings calendar...")
    earnings = fetch_earnings_calendar(portfolio)
    if earnings:
        print(f"  Upcoming ({EARNINGS_LOOKAHEAD_DAYS}d): {', '.join(f'{s} {d}' for s, d in sorted(earnings.items()))}")
    else:
        print("  None within lookahead window.")

    print("Updating portfolio subjects...")
    update_portfolio_subjects(portfolio)

    print("Running news scan...")
    news = run_news_scan()
    if "NO_NEW_RESULTS" in news:
        print("  No new news signals.")
    else:
        count = news.count("--- Result ")
        print(f"  {count} new article(s) found.")

    print("Generating briefing with Claude...")
    briefing = generate_briefing(portfolio, news, today, earnings)
    subject = (
        f"Morning Briefing | {today.strftime('%b %d')} | "
        f"${total_value:,.0f} | Day: ${total_day_pl:+,.0f}"
    )
    send_email(subject, briefing)

    print("\n--- Briefing ---")
    print(briefing.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))


if __name__ == "__main__":
    main()
