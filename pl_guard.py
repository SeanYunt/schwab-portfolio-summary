"""
Sanity checks for the broker-reported day P&L.

On Oct 14 and 15 2026 Schwab returned currentDayProfitLoss values of roughly
-50% for nearly every position (-$90K on a ~$194K portfolio) while the total
value was normal. The raw figure went straight into the email subject and the
LLM prompt, and the model wrote news-driven explanations for a crash that never
happened.

This module is deliberately dependency-free (no Schwab/Anthropic imports) so it
can be tested without credentials.
"""

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# A real one-day move beyond this fraction of a diversified portfolio is
# vanishingly rare; a broker data fault is far more likely.
MAX_DAY_PL_FRACTION = 0.15

# If at least this share of positions (min MIN_POSITIONS) shows a day move of
# this size, the position-level data is suspect even if the sum looks small.
SUSPECT_POSITION_PCT = 25.0
SUSPECT_POSITION_SHARE = 1 / 3
MIN_POSITIONS = 3


@dataclass
class DayPLAssessment:
    reported: float                 # what the broker said (summed over accounts)
    total_value: float
    verified: bool
    value: float | None             # safe-to-display figure; None if unavailable
    estimated: bool = False         # True when value is derived, not reported
    reasons: list[str] = field(default_factory=list)


def _positions(portfolio: list) -> list:
    return [p for acct in portfolio for p in acct["positions"]]


def assess_day_pl(portfolio: list, prev_total_value: float | None = None) -> DayPLAssessment:
    total_value = sum(a["total_value"] for a in portfolio)
    reported = sum(a["day_pl"] for a in portfolio)
    reasons = []

    if total_value > 0 and abs(reported) > MAX_DAY_PL_FRACTION * total_value:
        reasons.append(
            f"reported day P&L is {reported / total_value:+.1%} of total value "
            f"(limit ±{MAX_DAY_PL_FRACTION:.0%})"
        )

    positions = _positions(portfolio)
    if len(positions) >= MIN_POSITIONS:
        extreme = [p for p in positions if abs(p["day_pl_pct"]) >= SUSPECT_POSITION_PCT]
        if len(extreme) / len(positions) >= SUSPECT_POSITION_SHARE:
            reasons.append(
                f"{len(extreme)} of {len(positions)} positions show a day move of "
                f"{SUSPECT_POSITION_PCT:.0f}% or more"
            )

    if not reasons:
        return DayPLAssessment(reported, total_value, True, reported)

    if prev_total_value is not None:
        return DayPLAssessment(
            reported, total_value, False, total_value - prev_total_value, True, reasons
        )
    return DayPLAssessment(reported, total_value, False, None, False, reasons)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def subject_day_label(a: DayPLAssessment) -> str:
    if a.verified:
        return f"Day: ${a.value:+,.0f}"
    if a.value is not None:
        return f"Day: UNVERIFIED (est. ${a.value:+,.0f})"
    return "Day: UNVERIFIED"


def email_banner(a: DayPLAssessment) -> str:
    """Human-facing warning placed above the briefing. Empty when data is fine."""
    if a.verified:
        return ""
    lines = [
        "!! DATA WARNING: DAY P&L NOT TRUSTED !!",
        f"Schwab reported a day P&L of ${a.reported:+,.2f} on a total value of "
        f"${a.total_value:,.2f}. That failed a sanity check ({'; '.join(a.reasons)}).",
    ]
    if a.value is not None:
        lines.append(
            f"Change in total value since the previous run: ${a.value:+,.2f} "
            "(includes any deposits/withdrawals)."
        )
    lines.append(
        "Per-position day moves were withheld from the analysis below. "
        "Check Schwab directly before acting on any figure in this email."
    )
    return "\n".join(lines) + "\n\n" + "=" * 60 + "\n\n"


# ---------------------------------------------------------------------------
# LLM prompt fragments
# ---------------------------------------------------------------------------

def snapshot_lines(positions: list, total_value: float, a: DayPLAssessment) -> list[str]:
    if a.verified:
        lines = [f"Total value: ${total_value:,.2f} | Day P&L: ${a.value:+,.2f}", "", "Holdings:"]
        lines += [
            f"  {p['symbol']}: ${p['market_value']:,.2f} ({p['day_pl_pct']:+.2f}% today)"
            for p in positions
        ]
        return lines

    lines = [
        f"Total value: ${total_value:,.2f} | Day P&L: UNAVAILABLE "
        "(broker figure failed a sanity check)",
    ]
    if a.value is not None:
        lines.append(
            f"Approximate change in total value since the previous run: ${a.value:+,.0f} "
            "(includes deposits/withdrawals; not attributable to any holding)"
        )
    lines += ["", "Holdings (market value only — day moves unavailable):"]
    lines += [f"  {p['symbol']}: ${p['market_value']:,.2f}" for p in positions]
    return lines


def integrity_notice(a: DayPLAssessment) -> str:
    """Instructions that override the task when day-move data is unusable."""
    if a.verified:
        return ""
    return "\n".join([
        "=== DATA INTEGRITY NOTICE (overrides the task below) ===",
        "The broker's day gain/loss figures for today failed validation and were",
        "withheld. No reliable information exists about how any holding moved today.",
        "- Do NOT state, estimate, or imply any price move, gain, loss, or percentage",
        "  for the portfolio or any holding.",
        "- Do NOT explain, attribute, or correlate any price move with news; there is",
        "  no verified move to explain.",
        "- Begin the PORTFOLIO SUMMARY with one sentence saying that today's day P&L",
        "  data was unavailable and should be checked directly with the broker.",
        "- You may still report total portfolio value, summarize WARNING/CRITICAL news,",
        "  note upcoming earnings dates, and give a watch-list item.",
        "",
    ])


# ---------------------------------------------------------------------------
# Persistence: total-value history and raw broker payloads
# ---------------------------------------------------------------------------

def _read_history(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_prev_total(path: Path, today: date) -> float | None:
    """Most recent recorded total value from a day strictly before `today`."""
    earlier = [k for k in _read_history(path) if k < today.isoformat()]
    if not earlier:
        return None
    return _read_history(path)[max(earlier)]


def record_total(path: Path, today: date, total_value: float, keep: int = 60) -> None:
    history = _read_history(path)
    history[today.isoformat()] = total_value
    history = dict(sorted(history.items())[-keep:])
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2))
    except OSError as e:
        print(f"  WARNING: could not record total value history: {e}")


def _redact(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "accountNumber" and isinstance(v, str):
                out[k] = "****" + v[-4:]
            elif k == "hashValue":
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(node, list):
        return [_redact(i) for i in node]
    return node


def save_raw_snapshot(directory: Path, stamp: str, payload, keep: int = 30) -> Path | None:
    """Write the broker response (account identifiers redacted) for forensics."""
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"schwab_raw_{stamp}.json"
        path.write_text(json.dumps(_redact(copy.deepcopy(payload)), indent=2))
        # World-readable on purpose: the container writes as root, and the
        # host user must be able to read it. Account identifiers are redacted.
        try:
            path.chmod(0o644)
        except OSError:
            pass
        for old in sorted(directory.glob("schwab_raw_*.json"))[:-keep]:
            old.unlink()
        return path
    except OSError as e:
        print(f"  WARNING: could not save raw Schwab snapshot: {e}")
        return None
