# Schwab Portfolio Summary

Fetches your Charles Schwab brokerage positions, optionally ingests news signals from [messages-to-midnight](https://base.societus.club/societus/messages-to-midnight), and emails you a Claude-generated briefing each morning.

Two modes:

- **`portfolio_summary.py`** — plain portfolio snapshot, no news
- **`morning_briefing.py`** — portfolio + news cross-reference, consolidated into a single briefing

---

## Example output

```
Subject: Morning Briefing | Apr 13 | $247,831 | Day: +$1,204

The portfolio gained $1,204 (+0.49%) through the first hour of trading, led by a
strong open in NVDA (+2.3%) and steady performance in the core index positions.
The IRA account slightly lagged the taxable account, dragged by a small pullback
in AMZN (-0.7%).

On the news front, two WARNING-level signals surfaced this morning: a Fed
official's hawkish comments on rate cuts, and an earnings preannouncement miss
from a mid-cap semiconductor supplier. The semiconductor news is worth noting
given NVDA's supply-chain exposure — treat today's early NVDA strength with some
caution until broader sector reaction is clearer.

Watch the 10-year yield for the rest of the session; if it moves above 4.55%,
expect further pressure on growth names in the portfolio.
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/SeanYunt/schwab-portfolio-summary
cd schwab-portfolio-summary
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

### 2. Register a Schwab app

1. Go to [developer.schwab.com](https://developer.schwab.com) and create an app.
2. Set the callback URL to `https://127.0.0.1`.
3. Note the **App Key** and **App Secret**.

### 3. Configure `.env`

Copy `.env.example` to `.env` and fill in each value:

```ini
# Schwab API credentials
SCHWAB_APP_KEY=your_app_key_here
SCHWAB_APP_SECRET=your_app_secret_here

# Anthropic API key — get one at console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Gmail delivery — use an App Password (not your login password)
# https://myaccount.google.com/apppasswords
GMAIL_SENDER=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
GMAIL_RECIPIENT=you@gmail.com

# (Morning briefing only) Path to messages-to-midnight scan.py
SCAN_SCRIPT_PATH=C:\path\to\messages-to-midnight\scripts\scan.py
```

### 4. Authenticate with Schwab (first run only)

```bash
python auth.py
```

This opens a browser, walks you through the OAuth flow, and saves `token.json`. Subsequent runs refresh the token automatically.

### 5. Run

```bash
# Plain portfolio summary
python portfolio_summary.py

# Full morning briefing (portfolio + news)
python morning_briefing.py
```

---

## messages-to-midnight (news scan)

The morning briefing integrates with [messages-to-midnight](https://base.societus.club/societus/messages-to-midnight), a self-hosted news scanner that queries a local SearXNG instance and classifies articles as INFO / WARNING / CRITICAL.

### Set it up

1. Clone the repo and follow its setup instructions.
2. Start the SearXNG container it ships with:
   ```bash
   docker compose up -d searxng
   ```
3. Point `SCAN_SCRIPT_PATH` in your `.env` at `scripts/scan.py` inside that clone.

`morning_briefing.py` runs `scan.py` as a subprocess and passes its output directly into the Claude prompt for cross-referencing against your holdings.

If `SCAN_SCRIPT_PATH` is unset or the scanner errors, the briefing still runs — the news section is omitted gracefully.

---

## Scheduling (Windows Task Scheduler)

A helper batch file is included:

```
run_morning_briefing.bat
```

To run it automatically each weekday at 9:35 AM ET:

1. Open **Task Scheduler** → Create Basic Task.
2. Trigger: Daily, 9:35 AM; repeat Mon–Fri via the advanced settings.
3. Action: Start a program → `run_morning_briefing.bat`.
4. Ensure "Run only when user is logged on" matches your setup, or configure accordingly for headless runs.

---

## Project structure

```
schwab-portfolio-summary/
├── auth.py                  # One-time Schwab OAuth flow
├── portfolio_summary.py     # Standalone portfolio snapshot + email
├── morning_briefing.py      # Portfolio + news briefing + email
├── run_morning_briefing.bat # Task Scheduler launcher
├── requirements.txt
├── .env.example
└── token.json               # Created by auth.py (gitignored)
```

---

## Requirements

- Python 3.11+
- A Charles Schwab brokerage account with API access
- An [Anthropic API key](https://console.anthropic.com)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) configured
- Docker (optional, for messages-to-midnight / SearXNG)
