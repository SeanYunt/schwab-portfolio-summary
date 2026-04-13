"""
First-run OAuth authentication for Schwab API.

Run this once manually to generate token.json:
    python auth.py

After that, portfolio_summary.py handles token refresh automatically.
"""

import os
from dotenv import load_dotenv
import schwab

load_dotenv()

APP_KEY = os.environ["SCHWAB_APP_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
TOKEN_PATH = "token.json"
CALLBACK_URL = "https://127.0.0.1"


def authenticate():
    """Open browser for OAuth flow and save token to token.json."""
    print("Opening browser for Schwab OAuth login...")
    print("After authorizing, you will be redirected to a localhost URL.")
    print("Copy the full redirect URL and paste it here when prompted.\n")

    schwab.auth.client_from_manual_flow(
        api_key=APP_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        token_path=TOKEN_PATH,
    )
    print(f"\nSuccess. Token saved to {TOKEN_PATH}")
    print("You can now run: python portfolio_summary.py")


if __name__ == "__main__":
    authenticate()
