"""
Daily portfolio summary — fetches Schwab positions and generates
an executive summary via Claude, delivered by email.
"""

import os
import smtplib
import json
from datetime import date, timedelta
from email.mime.text import MIMEText
from dotenv import load_dotenv
import schwab
import anthropic
import yfinance as yf

load_dotenv()

APP_KEY = os.environ["SCHWAB_APP_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
TOKEN_PATH = "token.json"
CALLBACK_URL = "https://127.0.0.1"


# ---------------------------------------------------------------------------
# Schwab data fetch
# ---------------------------------------------------------------------------

def get_schwab_client():
    return schwab.auth.client_from_token_file(
        token_path=TOKEN_PATH,
        api_key=APP_KEY,
        app_secret=APP_SECRET,
    )


def fetch_portfolio(client) -> dict:
    """Fetch all accounts with positions and return a structured summary."""
    response = client.get_accounts(fields=[client.Account.Fields.POSITIONS])
    response.raise_for_status()
    accounts_data = response.json()

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

def build_prompt(portfolio: dict, today: date, earnings: dict | None = None) -> str:
    total_value = sum(a["total_value"] for a in portfolio)
    total_day_pl = sum(a["day_pl"] for a in portfolio)

    lines = [
        f"Today is {today.strftime('%A, %B %d, %Y')}. US equity markets opened 30 minutes ago.",
        f"",
        f"Total portfolio value across all Schwab accounts: ${total_value:,.2f}",
        f"Today's gain/loss so far: ${total_day_pl:+,.2f}",
        f"",
        f"Account breakdown:",
    ]

    for acct in portfolio:
        lines.append(
            f"  {acct['account_type']} ({acct['account_id']}): "
            f"${acct['total_value']:,.2f} | Day P&L: ${acct['day_pl']:+,.2f}"
        )
        for pos in sorted(acct["positions"], key=lambda p: p["market_value"], reverse=True)[:10]:
            lines.append(
                f"    {pos['symbol']}: ${pos['market_value']:,.2f} "
                f"({pos['day_pl_pct']:+.2f}% today)"
            )

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


def generate_summary(portfolio: dict, today: date, earnings: dict | None = None) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(portfolio, today, earnings)

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
    portfolio = fetch_portfolio(schwab_client)

    total_value = sum(a["total_value"] for a in portfolio)
    total_day_pl = sum(a["day_pl"] for a in portfolio)

    print(f"Total value: ${total_value:,.2f} | Day P&L: ${total_day_pl:+,.2f}")

    print("Fetching earnings calendar...")
    earnings = fetch_earnings_calendar(portfolio)
    if earnings:
        print(f"Upcoming earnings ({EARNINGS_LOOKAHEAD_DAYS}d window): {', '.join(f'{s} {d}' for s, d in sorted(earnings.items()))}")
    else:
        print("No earnings within lookahead window.")

    print("Generating summary...")
    summary = generate_summary(portfolio, today, earnings)
    print("\n--- Summary ---")
    print(summary)

    subject = (
        f"Portfolio Summary | {today.strftime('%b %d')} | "
        f"${total_value:,.0f} | Day: ${total_day_pl:+,.0f}"
    )
    send_email(subject, summary)


if __name__ == "__main__":
    main()
