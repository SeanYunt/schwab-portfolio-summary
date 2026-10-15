"""
Tests for pl_guard — the sanity check that stops implausible broker day-P&L
figures (Oct 14/15 2026: -$90K on a $194K portfolio) from reaching the email
subject and the LLM prompt.

Run: python -m unittest test_pl_guard -v
"""

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pl_guard


def position(symbol, market_value, day_pl, pct):
    return {
        "symbol": symbol,
        "market_value": market_value,
        "day_pl": day_pl,
        "day_pl_pct": pct,
    }


def portfolio_of(total_value, positions, day_pl=None):
    """One-account portfolio; day_pl defaults to the sum of position day_pl."""
    if day_pl is None:
        day_pl = sum(p["day_pl"] for p in positions)
    return [{
        "account_type": "MARGIN",
        "account_id": "...1234",
        "total_value": total_value,
        "cash": 0,
        "day_pl": day_pl,
        "positions": positions,
    }]


def healthy_positions():
    return [
        position("CAT", 30000, -250.0, -0.83),
        position("XOM", 20000, 120.0, 0.60),
        position("IBM", 25000, 300.0, 1.20),
        position("GLD", 15000, -90.0, -0.60),
    ]


def incident_positions():
    """Roughly the Oct 14 shape: every position ~-50% on the day."""
    return [
        position("NRG", 20000, -26400.0, -56.9),
        position("VST", 20000, -22900.0, -53.4),
        position("CEG", 20000, -22200.0, -52.6),
        position("NOC", 15000, -16900.0, -53.0),
        position("GLD", 10000, -8100.0, -44.8),
        position("SWPPX", 40000, 0.0, 0.0),
    ]


class AssessDayPLTests(unittest.TestCase):
    def test_oct_14_incident_is_rejected(self):
        portfolio = portfolio_of(194114.76, incident_positions(), day_pl=-90587.79)
        result = pl_guard.assess_day_pl(portfolio)
        self.assertFalse(result.verified)
        self.assertEqual(result.reported, -90587.79)
        self.assertTrue(result.reasons)

    def test_normal_day_is_verified_and_uses_reported_value(self):
        portfolio = portfolio_of(195651.41, healthy_positions(), day_pl=-458.65)
        result = pl_guard.assess_day_pl(portfolio)
        self.assertTrue(result.verified)
        self.assertEqual(result.value, -458.65)
        self.assertFalse(result.estimated)

    def test_aggregate_threshold_is_fifteen_percent_of_total(self):
        ok = pl_guard.assess_day_pl(portfolio_of(100000, healthy_positions(), day_pl=-14000))
        bad = pl_guard.assess_day_pl(portfolio_of(100000, healthy_positions(), day_pl=-16000))
        self.assertTrue(ok.verified)
        self.assertFalse(bad.verified)

    def test_large_gain_is_also_rejected(self):
        result = pl_guard.assess_day_pl(portfolio_of(100000, healthy_positions(), day_pl=40000))
        self.assertFalse(result.verified)

    def test_widespread_position_moves_rejected_even_when_aggregate_is_small(self):
        # Tiny positions, so the summed P&L is small, but 4 of 6 show -40%.
        positions = [position(f"S{i}", 500, -330.0, -40.0) for i in range(4)]
        positions += [position("IDX1", 90000, 10.0, 0.01), position("IDX2", 90000, 5.0, 0.01)]
        result = pl_guard.assess_day_pl(portfolio_of(181000, positions))
        self.assertFalse(result.verified)

    def test_single_stock_crash_is_not_rejected(self):
        positions = healthy_positions() + [position("XYZ", 5000, -4000.0, -44.4)]
        result = pl_guard.assess_day_pl(portfolio_of(100000, positions))
        self.assertTrue(result.verified)

    def test_rejected_value_is_estimated_from_previous_total(self):
        portfolio = portfolio_of(194114.76, incident_positions(), day_pl=-90587.79)
        result = pl_guard.assess_day_pl(portfolio, prev_total_value=195651.41)
        self.assertFalse(result.verified)
        self.assertTrue(result.estimated)
        self.assertAlmostEqual(result.value, -1536.65, places=2)

    def test_rejected_value_is_none_without_previous_total(self):
        portfolio = portfolio_of(194114.76, incident_positions(), day_pl=-90587.79)
        result = pl_guard.assess_day_pl(portfolio)
        self.assertIsNone(result.value)
        self.assertFalse(result.estimated)

    def test_sums_across_multiple_accounts(self):
        a = portfolio_of(50000, healthy_positions(), day_pl=-9000)[0]
        b = portfolio_of(50000, healthy_positions(), day_pl=-9000)[0]
        # -18,000 of 100,000 total = 18% > 15% even though each account is 18%...
        result = pl_guard.assess_day_pl([a, b])
        self.assertFalse(result.verified)
        self.assertEqual(result.reported, -18000)


class FormattingTests(unittest.TestCase):
    def _assess(self, day_pl, prev=None):
        positions = incident_positions() if abs(day_pl) > 20000 else healthy_positions()
        return pl_guard.assess_day_pl(portfolio_of(194114.76, positions, day_pl=day_pl), prev)

    def test_subject_for_verified_value_is_unchanged_format(self):
        self.assertEqual(pl_guard.subject_day_label(self._assess(-458.65)), "Day: $-459")

    def test_subject_for_rejected_value_with_estimate(self):
        label = pl_guard.subject_day_label(self._assess(-90587.79, prev=195651.41))
        self.assertIn("UNVERIFIED", label)
        self.assertIn("est.", label)
        self.assertIn("-1,537", label)
        self.assertNotIn("90,588", label)

    def test_subject_for_rejected_value_without_estimate(self):
        label = pl_guard.subject_day_label(self._assess(-90587.79))
        self.assertIn("UNVERIFIED", label)
        self.assertNotIn("90,588", label)

    def test_email_banner_shows_rejected_figure_for_the_human(self):
        banner = pl_guard.email_banner(self._assess(-90587.79, prev=195651.41))
        self.assertIn("90,587.79", banner)
        self.assertIn("-1,536.65", banner)

    def test_email_banner_is_empty_when_verified(self):
        self.assertEqual(pl_guard.email_banner(self._assess(-458.65)), "")


class PromptTests(unittest.TestCase):
    def test_verified_prompt_shows_day_pl_and_position_percentages(self):
        positions = healthy_positions()
        assessment = pl_guard.assess_day_pl(portfolio_of(195651.41, positions, day_pl=-458.65))
        snapshot = "\n".join(pl_guard.snapshot_lines(positions, 195651.41, assessment))
        self.assertIn("Day P&L: $-458.65", snapshot)
        self.assertIn("CAT", snapshot)
        self.assertIn("-0.83% today", snapshot)
        self.assertEqual(pl_guard.integrity_notice(assessment), "")

    def test_rejected_prompt_contains_no_bogus_figures_or_per_position_moves(self):
        positions = incident_positions()
        assessment = pl_guard.assess_day_pl(
            portfolio_of(194114.76, positions, day_pl=-90587.79), prev_total_value=195651.41
        )
        snapshot = "\n".join(pl_guard.snapshot_lines(positions, 194114.76, assessment))
        notice = pl_guard.integrity_notice(assessment)
        for text in (snapshot, notice):
            self.assertNotIn("90,587", text)
            self.assertNotIn("-56.9", text)
            self.assertNotIn("% today", text)
        self.assertIn("NRG", snapshot)            # holdings still listed
        self.assertIn("$20,000.00", snapshot)     # with market value
        self.assertIn("UNAVAILABLE", snapshot)

    def test_rejected_notice_forbids_attributing_price_moves(self):
        assessment = pl_guard.assess_day_pl(
            portfolio_of(194114.76, incident_positions(), day_pl=-90587.79)
        )
        notice = pl_guard.integrity_notice(assessment).lower()
        self.assertIn("do not", notice)
        self.assertIn("price move", notice)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.json"

    def test_missing_file_gives_no_previous_total(self):
        self.assertIsNone(pl_guard.load_prev_total(self.path, date(2026, 10, 15)))

    def test_returns_latest_entry_strictly_before_today(self):
        pl_guard.record_total(self.path, date(2026, 10, 13), 195651.41)
        pl_guard.record_total(self.path, date(2026, 10, 14), 194114.76)
        pl_guard.record_total(self.path, date(2026, 10, 15), 192928.80)
        self.assertEqual(pl_guard.load_prev_total(self.path, date(2026, 10, 15)), 194114.76)

    def test_same_day_rerun_overwrites_instead_of_appending(self):
        pl_guard.record_total(self.path, date(2026, 10, 14), 100.0)
        pl_guard.record_total(self.path, date(2026, 10, 14), 200.0)
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"2026-10-14": 200.0})

    def test_corrupt_file_is_treated_as_empty(self):
        self.path.write_text("{not json")
        self.assertIsNone(pl_guard.load_prev_total(self.path, date(2026, 10, 15)))
        pl_guard.record_total(self.path, date(2026, 10, 15), 1.0)  # must not raise
        self.assertEqual(json.loads(self.path.read_text()), {"2026-10-15": 1.0})

    def test_history_is_trimmed(self):
        for day in range(1, 29):
            pl_guard.record_total(self.path, date(2026, 9, day), float(day), keep=10)
        self.assertEqual(len(json.loads(self.path.read_text())), 10)


class RawSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"

    def test_writes_payload_and_redacts_account_identifiers(self):
        payload = [{"securitiesAccount": {
            "accountNumber": "12345678", "hashValue": "ABCDEF",
            "positions": [{"currentDayProfitLoss": -26400.0}],
        }}]
        path = pl_guard.save_raw_snapshot(self.dir, "20261015T070300", payload)
        text = path.read_text()
        self.assertNotIn("12345678", text)
        self.assertNotIn("ABCDEF", text)
        self.assertIn("5678", text)  # last four kept, like the rest of the app
        self.assertIn("-26400.0", text)

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes only")
    def test_snapshot_is_readable_by_the_host_user(self):
        # The container writes as root; a 0600 file is unreadable to the
        # host user who needs it for forensics (seen on T3600, Oct 15 2026).
        path = pl_guard.save_raw_snapshot(self.dir, "20261015T070300", [{"n": 1}])
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertTrue(mode & stat.S_IROTH, f"mode is {oct(mode)}")

    def test_prunes_to_newest_n_files(self):
        for i in range(6):
            pl_guard.save_raw_snapshot(self.dir, f"20261010T07000{i}", [{"n": i}], keep=3)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len(names), 3)
        self.assertIn("20261010T070005", names[-1])

    def test_unwritable_directory_does_not_raise(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        # directory path is under a regular file -> mkdir fails
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = pl_guard.save_raw_snapshot(blocker / "raw", "20261015T070300", [])
        self.assertIsNone(result)
        self.assertIn("WARNING", out.getvalue())


if __name__ == "__main__":
    unittest.main()
