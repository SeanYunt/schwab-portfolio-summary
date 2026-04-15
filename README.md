# Schwab Portfolio Summary

Fetches your Charles Schwab brokerage positions, optionally ingests news signals from [messages-to-midnight](https://base.societus.club/societus/messages-to-midnight), and emails you a Claude-generated briefing each morning.

Two modes:

- **`portfolio_summary.py`** — plain portfolio snapshot, no news
- **`morning_briefing.py`** — portfolio + news cross-reference, consolidated into a single briefing

---

## Example output

```
Subject: Morning Briefing | Apr 15 | $187,611 | Day: -$1,412

The portfolio shed $1,412 today, closing at $187,611, with losses broad-based
across industrial and commodity names. The steepest decliner was Caterpillar,
down 4.26%, followed by NRG Energy at -2.59% and RTX at -2.08%; no single news
article in today's scan directly explains the magnitude of today's CAT or RTX
moves, though broader tariff and valuation anxiety is well-documented in recent
coverage.

The most actionable warning comes from Fastenal's Q1 earnings: per Reuters and
Bloomberg, FAST flagged that tariffs are driving up costs faster than the company
can raise prices, with gross margins trailing estimates on cost headwinds.
TradingKey reports that Caterpillar now anticipates $2.6 billion in tariff
headwinds for 2026, up from the $1.8 billion figure previously flagged, adding
fresh pressure ahead of its April 30 earnings.

Fastenal's tariff-margin squeeze is a direct headwind for the FAST position; the
pricing-lag dynamic could persist into coming quarters. RTX reports April 21 and
a new $3.7 billion Patriot interceptor contract bolsters the backlog narrative,
though valuation concerns at record highs may cap upside.

Monitor CAT's pre-earnings price action into April 30, specifically whether the
$2.6 billion tariff headwind figure triggers further analyst estimate revisions.
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
# Plain portfolio snapshot
python portfolio_summary.py

# Full morning briefing (portfolio + news)
python morning_briefing.py
```

---

## messages-to-midnight (news scan)

The morning briefing integrates with [messages-to-midnight](https://base.societus.club/societus/messages-to-midnight), a self-hosted news scanner that queries a local SearXNG instance and outputs structured article results.

### Set it up

1. Clone the repo and follow its setup instructions.
2. Start the SearXNG container it ships with:
   ```bash
   docker compose up -d searxng
   ```
3. Point `SCAN_SCRIPT_PATH` in your `.env` at `scripts/scan.py` inside that clone.

`morning_briefing.py` runs `scan.py` as a subprocess and passes its output directly into the Claude prompt for cross-referencing against your holdings.

If `SCAN_SCRIPT_PATH` is unset or the scanner errors (e.g. Docker is not running), the briefing still runs — the news section is omitted gracefully.

### Automatic subjects.yaml management

`morning_briefing.py` rewrites the portfolio section of `subjects.yaml` on every run, based on your live Schwab positions. You do not need to edit it manually when your holdings change.

- Index funds and broad ETFs (SWPPX, SWTSX, VTI, etc.) are skipped
- Known holdings (CAT, RTX, FAST, RYCEY, XOM, GLD, SLV, CPER) use hand-crafted search topics and context descriptions defined in `SUBJECT_OVERRIDES`
- Related names are grouped into a single topic entry (e.g. VST, CEG, and NRG share one power-sector entry)
- Any other equity position falls back to a generic topic generated from yfinance metadata

The static early-warning subjects at the top of `subjects.yaml` (systemic risk topics like AGI, CBDC, cyberattacks, etc.) are never touched.

### Sourcing rules

The Claude prompt enforces a strict sourcing rule: price moves may only be attributed to a cause if a specific article from the scan directly supports it. If no article explains a move, the briefing says so explicitly rather than inventing a narrative.

---

## Earnings calendar

`morning_briefing.py` checks for upcoming earnings across all equity holdings using yfinance, with a 16-day lookahead window. Earnings dates are injected into the Claude prompt so the briefing can flag pre-earnings repricing dynamics and note what the market is likely watching ahead of each report.

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
├── morning_briefing.py      # Portfolio + news + earnings briefing + email
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
