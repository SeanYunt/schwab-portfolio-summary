"""
Smoke tests for Anthropic API, Gmail SMTP, and Schwab API.
Run: python test_connections.py
"""

import os
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ---------------------------------------------------------------------------
# 1. Anthropic API
# ---------------------------------------------------------------------------
print("Testing Anthropic API key...")
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=32,
    messages=[{"role": "user", "content": "Reply with exactly: API key works."}],
)
print(f"  {response.content[0].text}")

# ---------------------------------------------------------------------------
# 2. Gmail SMTP
# ---------------------------------------------------------------------------
print("Testing Gmail SMTP...")
sender = os.environ["GMAIL_SENDER"]
recipient = os.environ["GMAIL_RECIPIENT"]
password = os.environ["GMAIL_APP_PASSWORD"]

msg = MIMEText("If you're reading this, Gmail SMTP is working correctly.")
msg["Subject"] = "Schwab Portfolio Summary — Email Test"
msg["From"] = sender
msg["To"] = recipient

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(sender, password)
    server.send_message(msg)

print(f"  Email sent to {recipient}. Check your inbox.")

# ---------------------------------------------------------------------------
# 3. Schwab API
# ---------------------------------------------------------------------------
print("Testing Schwab API...")

app_key = os.environ.get("SCHWAB_APP_KEY", "")
app_secret = os.environ.get("SCHWAB_APP_SECRET", "")
token_path = "token.json"

if not app_key or app_key == "your_app_key_here":
    print("  SKIPPED — SCHWAB_APP_KEY not set in .env.")
elif not app_secret or app_secret == "your_app_secret_here":
    print("  SKIPPED — SCHWAB_APP_SECRET not set in .env.")
elif not os.path.exists(token_path):
    print(f"  SKIPPED — token.json not found. Run: python auth.py")
else:
    import schwab
    try:
        schwab_client = schwab.auth.client_from_token_file(
            token_path=token_path,
            api_key=app_key,
            app_secret=app_secret,
        )
        response = schwab_client.get_account_numbers()
        response.raise_for_status()
        accounts = response.json()
        print(f"  Connected. {len(accounts)} account(s) found.")
    except Exception as e:
        print(f"  FAILED — {e}")
        raise

print("\nAll checks passed.")
