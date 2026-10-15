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


# ---------------------------------------------------------------------------
# Reconstruction from per-share netChange (added after the Oct 15 forensics)
# ---------------------------------------------------------------------------

def rpos(symbol, asset_type, qty, price, pl, net_change=None, day_cost=0.0, avg=None, short=0.0):
    """Position carrying the fields the reconstruction needs (real Oct 15 values)."""
    mv = round(qty * price, 2)
    return {
        "symbol": symbol, "asset_type": asset_type, "quantity": qty,
        "short_quantity": short, "market_value": mv,
        "day_pl": pl, "day_pl_pct": pl / (mv - pl) * 100 if mv - pl else 0.0,
        "net_change": net_change, "day_cost": day_cost, "average_price": avg,
    }


def corrupt_oct15_positions():
    """Subset of the real broker payload: day_pl == qty*netChange - currentDayCost."""
    return [
        rpos("SWPPX", "MUTUAL_FUND", 660.15, 19.95, -145.23, -0.22, 10127.05, 15.34),
        rpos("F", "EQUITY", 200.0, 12.1677, -2409.66, 23.54 / 200.0, 2433.20, 12.166),
        rpos("XOM", "EQUITY", 66.27, 175.23, -5303.20, -117.96 / 66.27, 5185.24, 78.24),
        rpos("GLD", "COLLECTIVE_INVESTMENT", 25.0, 381.07, -8592.88, None, 8719.88, 348.795),
        rpos("CPER", "COLLECTIVE_INVESTMENT", 300.0, 39.321, -9842.05, None, 9740.35, 32.4678),
    ]


# qty*netChange for the equities/fund, and (reported + currentDayCost) for the ETFs
EXPECTED_RECONSTRUCTED_TOTAL = -145.23 + 23.54 - 117.96 + 127.00 - 101.70   # -214.35


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = portfolio_of(40000.0, corrupt_oct15_positions())

    def _by_symbol(self, portfolio):
        return {p["symbol"]: p for p in portfolio[0]["positions"]}

    def test_equities_use_quantity_times_net_change(self):
        _, portfolio = pl_guard.assess_with_reconstruction(self.portfolio)
        by = self._by_symbol(portfolio)
        self.assertAlmostEqual(by["F"]["day_pl"], 23.54, delta=0.01)
        self.assertAlmostEqual(by["XOM"]["day_pl"], -117.96, delta=0.01)

    def test_mutual_fund_reconstruction_matches_broker_figure(self):
        _, portfolio = pl_guard.assess_with_reconstruction(self.portfolio)
        self.assertAlmostEqual(self._by_symbol(portfolio)["SWPPX"]["day_pl"], -145.23, delta=0.05)

    def test_etf_without_net_change_uses_cost_basis_signature(self):
        _, portfolio = pl_guard.assess_with_reconstruction(self.portfolio)
        by = self._by_symbol(portfolio)
        self.assertAlmostEqual(by["GLD"]["day_pl"], 127.00, delta=0.01)
        self.assertAlmostEqual(by["CPER"]["day_pl"], -101.70, delta=0.01)

    def test_reconstructed_percentages_are_plausible(self):
        _, portfolio = pl_guard.assess_with_reconstruction(self.portfolio)
        by = self._by_symbol(portfolio)
        self.assertAlmostEqual(by["F"]["day_pl_pct"], 0.98, delta=0.01)
        for p in by.values():
            self.assertLess(abs(p["day_pl_pct"]), 5.0, p["symbol"])

    def test_assessment_reports_reconstruction_and_keeps_rejected_figure(self):
        assessment, _ = pl_guard.assess_with_reconstruction(self.portfolio)
        self.assertFalse(assessment.verified)
        self.assertTrue(assessment.reconstructed)
        self.assertFalse(assessment.estimated)
        self.assertAlmostEqual(assessment.value, EXPECTED_RECONSTRUCTED_TOTAL, delta=0.1)
        self.assertAlmostEqual(assessment.reported, -26293.02, places=2)
        self.assertEqual(assessment.excluded, [])

    def test_reconstruction_preferred_over_previous_total_estimate(self):
        assessment, _ = pl_guard.assess_with_reconstruction(self.portfolio, prev_total_value=45000.0)
        self.assertTrue(assessment.reconstructed)
        self.assertAlmostEqual(assessment.value, EXPECTED_RECONSTRUCTED_TOTAL, delta=0.1)

    def test_trusted_broker_data_is_left_alone(self):
        portfolio = portfolio_of(195651.41, healthy_positions(), day_pl=-458.65)
        assessment, returned = pl_guard.assess_with_reconstruction(portfolio)
        self.assertTrue(assessment.verified)
        self.assertFalse(assessment.reconstructed)
        self.assertIs(returned, portfolio)

    def test_input_portfolio_is_not_mutated(self):
        pl_guard.assess_with_reconstruction(self.portfolio)
        self.assertEqual(self._by_symbol(self.portfolio)["F"]["day_pl"], -2409.66)

    def test_etf_without_net_change_or_signature_is_not_reconstructed(self):
        positions = corrupt_oct15_positions()
        positions[3] = rpos("GLD", "COLLECTIVE_INVESTMENT", 25.0, 381.07, -8592.88, None, 0.0, 348.795)
        # GLD is ~20% of position value: over the 5% coverage limit
        assessment, _ = pl_guard.assess_with_reconstruction(portfolio_of(40000.0, positions))
        self.assertFalse(assessment.reconstructed)
        self.assertIsNone(assessment.value)

    def test_small_uncoverable_position_is_excluded_but_not_fatal(self):
        positions = corrupt_oct15_positions()
        positions.append(rpos("ODD", "OPTION", 1.0, 100.0, -90.0, None, 0.0, None))
        assessment, portfolio = pl_guard.assess_with_reconstruction(portfolio_of(40000.0, positions))
        self.assertTrue(assessment.reconstructed)
        self.assertEqual(assessment.excluded, ["ODD"])
        odd = self._by_symbol(portfolio)["ODD"]
        self.assertIsNone(odd["day_pl"])
        self.assertIsNone(odd["day_pl_pct"])

    def test_implausible_reconstruction_is_rejected_and_falls_back(self):
        # netChange itself says -40% on every holding: don't launder that.
        positions = [rpos(f"S{i}", "EQUITY", 100.0, 60.0, -3000.0, -40.0, 10000.0, 100.0)
                     for i in range(5)]
        portfolio = portfolio_of(30000.0, positions)
        assessment, _ = pl_guard.assess_with_reconstruction(portfolio, prev_total_value=30500.0)
        self.assertFalse(assessment.reconstructed)
        self.assertTrue(assessment.estimated)
        self.assertAlmostEqual(assessment.value, -500.0)

    def test_short_positions_use_net_quantity(self):
        positions = corrupt_oct15_positions()
        positions.append(rpos("SHRT", "EQUITY", 0.0, 50.0, -400.0, 0.5, 600.0, 40.0, short=20.0))
        positions[-1]["market_value"] = -1000.0
        _, portfolio = pl_guard.assess_with_reconstruction(portfolio_of(40000.0, positions))
        self.assertAlmostEqual(self._by_symbol(portfolio)["SHRT"]["day_pl"], -10.0)  # -20 sh * +0.50


class ReconstructedOutputTests(unittest.TestCase):
    def setUp(self):
        portfolio = portfolio_of(40000.0, corrupt_oct15_positions())
        self.assessment, self.portfolio = pl_guard.assess_with_reconstruction(portfolio)
        self.positions = [p for a in self.portfolio for p in a["positions"]]

    def test_subject_marks_figure_as_reconstructed(self):
        label = pl_guard.subject_day_label(self.assessment)
        self.assertIn("UNVERIFIED", label)
        self.assertIn("reconstructed", label)
        self.assertIn("-214", label)
        self.assertNotIn("26,293", label)

    def test_banner_shows_rejected_and_reconstructed_figures(self):
        banner = pl_guard.email_banner(self.assessment)
        self.assertIn("26,293.02", banner)
        self.assertIn("-214", banner)
        self.assertIn("reconstructed", banner.lower())

    def test_prompt_uses_reconstructed_moves_and_hides_broker_figures(self):
        snapshot = "\n".join(pl_guard.snapshot_lines(self.positions, 40000.0, self.assessment))
        self.assertIn("RECONSTRUCTED", snapshot)
        self.assertIn("F:", snapshot)
        self.assertIn("+0.98% today", snapshot)
        for bogus in ("26,293", "-47.4", "-49.7", "2,409"):
            self.assertNotIn(bogus, snapshot)

    def test_notice_explains_provenance_and_forbids_broker_figures(self):
        notice = pl_guard.integrity_notice(self.assessment).lower()
        self.assertIn("reconstructed", notice)
        self.assertIn("do not", notice)
        self.assertNotIn("26,293", notice)

    def test_unreconstructed_positions_are_listed_without_a_move(self):
        positions = corrupt_oct15_positions()
        positions.append(rpos("ODD", "OPTION", 1.0, 100.0, -90.0, None, 0.0, None))
        assessment, portfolio = pl_guard.assess_with_reconstruction(portfolio_of(40000.0, positions))
        flat = [p for a in portfolio for p in a["positions"]]
        snapshot = "\n".join(pl_guard.snapshot_lines(flat, 40000.0, assessment))
        odd_line = [l for l in snapshot.splitlines() if l.strip().startswith("ODD")][0]
        self.assertIn("unavailable", odd_line)
        self.assertNotIn("% today", odd_line)


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
