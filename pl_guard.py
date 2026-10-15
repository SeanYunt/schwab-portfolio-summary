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
    estimated: bool = False         # True when value is the change in total value
    reasons: list[str] = field(default_factory=list)
    reconstructed: bool = False     # True when value was rebuilt from netChange
    excluded: list[str] = field(default_factory=list)   # symbols not reconstructed


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

    # Positions without a day move (None) are skipped, not counted.
    pcts = [p["day_pl_pct"] for p in _positions(portfolio) if p.get("day_pl_pct") is not None]
    if len(pcts) >= MIN_POSITIONS:
        extreme = [x for x in pcts if abs(x) >= SUSPECT_POSITION_PCT]
        if len(extreme) / len(pcts) >= SUSPECT_POSITION_SHARE:
            reasons.append(
                f"{len(extreme)} of {len(pcts)} positions show a day move of "
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
# Reconstruction
#
# Oct 2026 forensics: for every corrupted equity,
#     currentDayProfitLoss == qty * instrument.netChange - currentDayCost
# and currentDayCost was filled with the position's full cost basis
# (qty * averagePrice). netChange (per-share day change) was intact and
# reproduces Schwab's own correct figure for mutual funds exactly. ETFs carry
# no netChange, but currentDayProfitLoss + currentDayCost recovers the same
# quantity, so that fallback is only applied when the cost-basis signature
# is present.
# ---------------------------------------------------------------------------

# currentDayCost must match qty * averagePrice within this fraction to count
# as the Schwab fault signature.
COST_BASIS_TOLERANCE = 0.02
# Give up if positions we could not reconstruct exceed this share of position value.
MAX_UNCOVERED_FRACTION = 0.05


def _reconstruct_position(p: dict) -> float | None:
    """Best available day P&L for one position, or None if not recoverable."""
    quantity = p.get("quantity", 0) or 0
    net_qty = quantity - (p.get("short_quantity", 0) or 0)

    net_change = p.get("net_change")
    if net_change is not None:
        return net_qty * net_change

    day_cost = p.get("day_cost") or 0
    average_price = p.get("average_price")
    if (
        average_price is not None
        and day_cost > 0
        and abs(quantity * average_price - day_cost) <= COST_BASIS_TOLERANCE * day_cost
    ):
        return p["day_pl"] + day_cost
    return None


def _reconstruct_portfolio(portfolio: list) -> tuple[list, list[str]] | None:
    """Copy of `portfolio` with reconstructed day figures, or None if coverage is too thin."""
    rebuilt = copy.deepcopy(portfolio)
    total_abs = uncovered_abs = 0.0
    excluded = []

    for acct in rebuilt:
        for p in acct["positions"]:
            market_value = p.get("market_value", 0) or 0
            total_abs += abs(market_value)
            derived = _reconstruct_position(p)
            if derived is None:
                uncovered_abs += abs(market_value)
                excluded.append(p["symbol"])
                p["day_pl"] = None
                p["day_pl_pct"] = None
                continue
            base = market_value - derived          # implied value before today's move
            p["day_pl"] = derived
            p["day_pl_pct"] = derived / base * 100 if base else 0.0
        acct["day_pl"] = sum(p["day_pl"] for p in acct["positions"] if p["day_pl"] is not None)

    if total_abs <= 0 or uncovered_abs / total_abs > MAX_UNCOVERED_FRACTION:
        return None
    return rebuilt, excluded


def assess_with_reconstruction(
    portfolio: list, prev_total_value: float | None = None
) -> tuple[DayPLAssessment, list]:
    """
    Assess the broker's day P&L; if it fails, try to rebuild it from per-share
    price changes. Returns (assessment, portfolio_to_use). The input is never
    mutated; the returned portfolio is the input when nothing was rebuilt.
    """
    assessment = assess_day_pl(portfolio, prev_total_value)
    if assessment.verified:
        return assessment, portfolio

    rebuilt = _reconstruct_portfolio(portfolio)
    if rebuilt is not None:
        new_portfolio, excluded = rebuilt
        # A rebuild that is itself implausible is not laundered into a figure.
        if assess_day_pl(new_portfolio).verified:
            value = sum(a["day_pl"] for a in new_portfolio)
            return (
                DayPLAssessment(
                    assessment.reported, assessment.total_value, False, value,
                    False, assessment.reasons, reconstructed=True, excluded=excluded,
                ),
                new_portfolio,
            )
    return assessment, portfolio


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def subject_day_label(a: DayPLAssessment) -> str:
    if a.verified:
        return f"Day: ${a.value:+,.0f}"
    if a.reconstructed:
        return f"Day: UNVERIFIED (reconstructed ${a.value:+,.0f})"
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
    if a.reconstructed:
        lines.append(
            f"Day P&L reconstructed from Schwab's per-share price changes: ${a.value:+,.2f}"
            + (f" (not available for: {', '.join(a.excluded)})." if a.excluded else ".")
        )
        lines.append(
            "Per-position moves in the analysis below use the reconstructed figures. "
            "Check Schwab directly before acting on any figure in this email."
        )
    else:
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

    if a.reconstructed:
        lines = [
            f"Total value: ${total_value:,.2f} | Day P&L: ${a.value:+,.2f} "
            "(RECONSTRUCTED from per-share price changes; the broker's own figure was corrupt)",
            "",
            "Holdings (day moves RECONSTRUCTED):",
        ]
        for p in positions:
            if p.get("day_pl_pct") is None:
                lines.append(f"  {p['symbol']}: ${p['market_value']:,.2f} (day move unavailable)")
            else:
                lines.append(
                    f"  {p['symbol']}: ${p['market_value']:,.2f} ({p['day_pl_pct']:+.2f}% today)"
                )
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
    if a.reconstructed:
        return "\n".join([
            "=== DATA INTEGRITY NOTICE (overrides the task below) ===",
            "The broker's own day gain/loss fields were corrupt today and were discarded.",
            "The day moves listed above were RECONSTRUCTED from per-share price-change data.",
            "- Say once, briefly, that today's day figures were reconstructed because the",
            "  broker's data was faulty.",
            "- Use only the figures listed above. Do NOT cite, estimate or mention any other",
            "  day P&L figure or percentage.",
            "- Positions marked 'day move unavailable' have no move; do not describe one.",
            "- The sourcing rule below still applies: attribute a cause only when an article",
            "  in the news section directly supports it.",
            "",
        ])
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
