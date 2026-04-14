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
from dotenv import load_dotenv
import anthropic

from portfolio_summary import get_schwab_client, fetch_portfolio

load_dotenv()

SCAN_SCRIPT = os.environ.get("SCAN_SCRIPT_PATH", "")
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# News scan
# ---------------------------------------------------------------------------

def run_news_scan() -> str:
    """Run scan.py and return its stdout output."""
    try:
        result = subprocess.run(
            [PYTHON, SCAN_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
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

def build_briefing_prompt(portfolio: list, news: str, today: date) -> str:
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
        "=== YOUR TASK ===",
        "Write a unified morning briefing in flowing prose (no bullet points). Include:",
        "",
        "1. PORTFOLIO SUMMARY (2-3 sentences)",
        "   - Overall performance today",
        "   - Notable movers",
        "",
        "2. NEWS & MARKET SIGNALS (2-3 sentences)",
        "   - Summarize the most important WARNING or CRITICAL news items",
        "   - Skip INFO-level items unless directly relevant to a holding",
        "",
        "3. CROSS-REFERENCE (1-3 sentences, only if relevant connections exist)",
        "   - Flag any news that plausibly affects a held position",
        "   - Name the position, the news event, and whether it is a tailwind or headwind",
        "   - If no meaningful connections exist, omit this section entirely",
        "",
        "4. WATCH LIST (1 sentence)",
        "   - One thing to monitor for the rest of the session",
        "",
        "Be direct and factual. No hype. Total length: 8-12 sentences.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def generate_briefing(portfolio: list, news: str, today: date) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_briefing_prompt(portfolio, news, today)

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

    print("Running news scan...")
    news = run_news_scan()
    if "NO_NEW_RESULTS" in news:
        print("  No new news signals.")
    else:
        # Count results
        count = news.count("--- Result ")
        print(f"  {count} new article(s) found.")

    print("Generating briefing with Claude...")
    briefing = generate_briefing(portfolio, news, today)
    print("\n--- Briefing ---")
    print(briefing)

    subject = (
        f"Morning Briefing | {today.strftime('%b %d')} | "
        f"${total_value:,.0f} | Day: ${total_day_pl:+,.0f}"
    )
    send_email(subject, briefing)


if __name__ == "__main__":
    main()
