"""
Daily portfolio summary — fetches Schwab positions and generates
an executive summary via Claude, delivered by email.
"""

import os
import smtplib
import json
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv
import pl_guard
import schwab
import anthropic
import yfinance as yf

load_dotenv()

APP_KEY = os.environ["SCHWAB_APP_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
TOKEN_PATH = "token.json"
CALLBACK_URL = "https://127.0.0.1"

# Persisted across runs (mounted volume in docker-compose.yml): raw broker
# payloads and the daily total-value history used to sanity-check day P&L.
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "snapshots"))
HISTORY_PATH = SNAPSHOT_DIR / "total_value_history.json"


# ---------------------------------------------------------------------------
# Schwab data fetch
# ---------------------------------------------------------------------------

def get_schwab_client():
    return schwab.auth.client_from_token_file(
        token_path=TOKEN_PATH,
        api_key=APP_KEY,
        app_secret=APP_SECRET,
    )


def fetch_accounts_raw(client) -> list:
    """Return Schwab's unmodified accounts+positions response."""
    response = client.get_accounts(fields=[client.Account.Fields.POSITIONS])
    response.raise_for_status()
    return response.json()


def fetch_portfolio(client) -> list:
    """Fetch all accounts with positions and return a structured summary."""
    return parse_accounts(fetch_accounts_raw(client))


def parse_accounts(accounts_data: list) -> list:
    portfolio = []
    for account in accounts_data:
        acct = account.get("securitiesAccount", {})
        account_type = acct.get("type", "Unknown")
        account_id = acct.get("accountNumber", "")[-4:]  # last 4 digits only

        positions = []
        for pos in acct.get("positions", []):
            instrument = pos.get("instrument", {})
            positions.append({
                "symbol": instrument.get("symbol", instrument.get("description", "Unknown")),
                "asset_type": instrument.get("assetType", ""),
                "quantity": pos.get("longQuantity", 0),
                "market_value": pos.get("marketValue", 0),
                "day_pl": pos.get("currentDayProfitLoss", 0),
                "day_pl_pct": pos.get("currentDayProfitLossPercentage", 0),
            })

        balances = acct.get("currentBalances", {})
        portfolio.append({
            "account_type": account_type,
            "account_id": f"...{account_id}",
            "total_value": balances.get("liquidationValue", 0),
            "cash": balances.get("cashBalance", 0),
            "day_pl": sum(p["day_pl"] for p in positions),
            "positions": positions,
        })

    return portfolio


# ---------------------------------------------------------------------------
# Earnings calendar
# ---------------------------------------------------------------------------

EARNINGS_LOOKAHEAD_DAYS = 16  # options premium + analyst revisions start ~2 weeks out; 16d gives buffer for date shifts


def fetch_earnings_calendar(portfolio: list) -> dict:
    """
    Return {symbol: earnings_date_str} for equity holdings with earnings
    within EARNINGS_LOOKAHEAD_DAYS days. Silently skips symbols that fail
    or have no upcoming date (ETFs, cash, etc.).
    """
    today = date.today()
    cutoff = today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)

    symbols = {
        pos["symbol"]
        for acct in portfolio
        for pos in acct["positions"]
        if pos["asset_type"] in ("EQUITY", "ETF", "")
        and pos["symbol"].isalpha()  # skip bond tickers, options, etc.
    }

    upcoming = {}
    for symbol in sorted(symbols):
        try:
            cal = yf.Ticker(symbol).calendar
            if not cal:
                continue
            # yfinance returns a dict; 'Earnings Date' is a list of Timestamps
            dates = cal.get("Earnings Date", [])
            if not dates:
                continue
            earliest = min(dates).date() if hasattr(dates[0], "date") else dates[0]
            if today <= earliest <= cutoff:
                upcoming[symbol] = earliest.strftime("%b %d")
        except Exception:
            continue  # non-critical — don't let a bad ticker break the brief

    return upcoming


# ---------------------------------------------------------------------------
# Claude summary
# ---------------------------------------------------------------------------

def build_prompt(
    portfolio: list,
    today: date,
    earnings: dict | None = None,
    assessment: "pl_guard.DayPLAssessment | None" = None,
) -> str:
    total_value = sum(a["total_value"] for a in portfolio)
    if assessment is None:
        assessment = pl_guard.assess_day_pl(portfolio)
    trusted = assessment.verified

    lines = [
        f"Today is {today.strftime('%A, %B %d, %Y')}. US equity markets opened 30 minutes ago.",
        f"",
        f"Total portfolio value across all Schwab accounts: ${total_value:,.2f}",
        (
            f"Today's gain/loss so far: ${assessment.value:+,.2f}"
            if trusted
            else "Today's gain/loss so far: UNAVAILABLE (broker figure failed validation)"
        ),
        f"",
        f"Account breakdown:",
    ]

    for acct in portfolio:
        acct_pl = f" | Day P&L: ${acct['day_pl']:+,.2f}" if trusted else ""
        lines.append(
            f"  {acct['account_type']} ({acct['account_id']}): "
            f"${acct['total_value']:,.2f}{acct_pl}"
        )
        for pos in sorted(acct["positions"], key=lambda p: p["market_value"], reverse=True)[:10]:
            pos_pct = f" ({pos['day_pl_pct']:+.2f}% today)" if trusted else ""
            lines.append(f"    {pos['symbol']}: ${pos['market_value']:,.2f}{pos_pct}")

    notice = pl_guard.integrity_notice(assessment)
    if notice:
        lines += ["", notice]

    if earnings:
        lines += ["", "Upcoming earnings (within 14 days):"]
        for symbol, dt in sorted(earnings.items()):
            lines.append(f"  {symbol}: reports {dt}")

    lines += [
        "",
        "Write a concise 5–7 sentence executive summary of this portfolio snapshot. Include:",
        "- Overall portfolio performance today",
        "- Which accounts or positions are driving gains or losses — anchor explanations to",
        "  macro drivers first (trade policy, Fed, fiscal) before sector or company narratives",
        "- For any holding with upcoming earnings flagged above, note the date and what",
        "  investors are likely repricing ahead of that report",
        "- One sentence on what to monitor for the rest of the session",
        "Use plain language. No bullet points — flowing prose only.",
    ]

    return "\n".join(lines)


def generate_summary(portfolio: list, today: date, earnings: dict | None = None, assessment=None) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(portfolio, today, earnings, assessment)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Email delivery
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

    print(f"Summary emailed to {recipient}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    today = date.today()
    print(f"Fetching portfolio data for {today}...")

    schwab_client = get_schwab_client()
    raw_accounts = fetch_accounts_raw(schwab_client)
    pl_guard.save_raw_snapshot(SNAPSHOT_DIR / "raw", datetime.now().strftime("%Y%m%dT%H%M%S"), raw_accounts)
    portfolio = parse_accounts(raw_accounts)

    total_value = sum(a["total_value"] for a in portfolio)
    assessment = pl_guard.assess_day_pl(portfolio, pl_guard.load_prev_total(HISTORY_PATH, today))
    pl_guard.record_total(HISTORY_PATH, today, total_value)

    print(f"Total value: ${total_value:,.2f} | Day P&L (reported): ${assessment.reported:+,.2f}")
    if not assessment.verified:
        print(f"WARNING: day P&L failed sanity check — {'; '.join(assessment.reasons)}")

    print("Fetching earnings calendar...")
    earnings = fetch_earnings_calendar(portfolio)
    if earnings:
        print(f"Upcoming earnings ({EARNINGS_LOOKAHEAD_DAYS}d window): {', '.join(f'{s} {d}' for s, d in sorted(earnings.items()))}")
    else:
        print("No earnings within lookahead window.")

    print("Generating summary...")
    summary = pl_guard.email_banner(assessment) + generate_summary(portfolio, today, earnings, assessment)
    print("\n--- Summary ---")
    print(summary)

    subject = (
        f"Portfolio Summary | {today.strftime('%b %d')} | "
        f"${total_value:,.0f} | {pl_guard.subject_day_label(assessment)}"
    )
    send_email(subject, summary)


if __name__ == "__main__":
    main()
