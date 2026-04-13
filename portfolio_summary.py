"""
Daily portfolio summary — fetches Schwab positions and generates
an executive summary via Claude, delivered by email.
"""

import os
import smtplib
import json
from datetime import date
from email.mime.text import MIMEText
from dotenv import load_dotenv
import schwab
import anthropic

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
# Claude summary
# ---------------------------------------------------------------------------

def build_prompt(portfolio: dict, today: date) -> str:
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

    lines += [
        "",
        "Write a concise 5–7 sentence executive summary of this portfolio snapshot. Include:",
        "- Overall portfolio performance today",
        "- Which accounts or positions are driving gains or losses",
        "- Any notable movers worth watching",
        "- One sentence on what to monitor for the rest of the session",
        "Use plain language. No bullet points — flowing prose only.",
    ]

    return "\n".join(lines)


def generate_summary(portfolio: dict, today: date) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(portfolio, today)

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
    print("Generating summary...")

    summary = generate_summary(portfolio, today)
    print("\n--- Summary ---")
    print(summary)

    subject = (
        f"Portfolio Summary | {today.strftime('%b %d')} | "
        f"${total_value:,.0f} | Day: ${total_day_pl:+,.0f}"
    )
    send_email(subject, summary)


if __name__ == "__main__":
    main()
