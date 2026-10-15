"""
Tests for schwab_parse — turning Schwab's accounts+positions JSON into the
portfolio structure, including the fields needed to reconstruct day P&L when
the broker's own figure is corrupt.

Run: python -m unittest test_schwab_parse -v
"""

import unittest

import schwab_parse


def raw_payload():
    """Shape taken from the real Oct 15 2026 response (values trimmed)."""
    return [{
        "securitiesAccount": {
            "type": "CASH",
            "accountNumber": "****3432",
            "currentBalances": {"liquidationValue": 184647.23, "cashBalance": 20775.09},
            "positions": [
                {   # equity: has netChange; broker P&L corrupted by currentDayCost
                    "shortQuantity": 0.0, "longQuantity": 200.0, "averagePrice": 12.166,
                    "marketValue": 2433.54, "currentDayCost": 2433.2,
                    "currentDayProfitLoss": -2409.66, "currentDayProfitLossPercentage": -49.75,
                    "instrument": {"symbol": "F", "assetType": "EQUITY", "netChange": 0.1177},
                },
                {   # ETF: no netChange in the payload
                    "shortQuantity": 0.0, "longQuantity": 25.0, "averagePrice": 348.795,
                    "marketValue": 9526.75, "currentDayCost": 8719.88,
                    "currentDayProfitLoss": -8592.88, "currentDayProfitLossPercentage": -47.42,
                    "instrument": {"symbol": "GLD", "assetType": "COLLECTIVE_INVESTMENT"},
                },
                {   # short leg and missing optional fields
                    "shortQuantity": 10.0, "longQuantity": 0.0,
                    "marketValue": -1000.0,
                    "instrument": {"assetType": "EQUITY", "description": "SOME SHORT"},
                },
            ],
        }
    }]


class ParseAccountsTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = schwab_parse.parse_accounts(raw_payload())
        self.positions = {p["symbol"]: p for p in self.portfolio[0]["positions"]}

    def test_account_level_fields_are_unchanged(self):
        acct = self.portfolio[0]
        self.assertEqual(acct["account_type"], "CASH")
        self.assertEqual(acct["account_id"], "...3432")
        self.assertEqual(acct["total_value"], 184647.23)
        self.assertEqual(acct["cash"], 20775.09)

    def test_existing_position_fields_are_unchanged(self):
        f = self.positions["F"]
        self.assertEqual(f["asset_type"], "EQUITY")
        self.assertEqual(f["quantity"], 200.0)
        self.assertEqual(f["market_value"], 2433.54)
        self.assertEqual(f["day_pl"], -2409.66)
        self.assertEqual(f["day_pl_pct"], -49.75)

    def test_account_day_pl_is_still_the_broker_sum(self):
        self.assertAlmostEqual(self.portfolio[0]["day_pl"], -2409.66 - 8592.88 + 0)

    def test_reconstruction_inputs_are_extracted(self):
        f = self.positions["F"]
        self.assertEqual(f["net_change"], 0.1177)
        self.assertEqual(f["day_cost"], 2433.2)
        self.assertEqual(f["average_price"], 12.166)
        self.assertEqual(f["short_quantity"], 0.0)

    def test_missing_net_change_is_none_not_zero(self):
        self.assertIsNone(self.positions["GLD"]["net_change"])

    def test_missing_optional_fields_default_safely(self):
        short = self.positions["SOME SHORT"]
        self.assertEqual(short["short_quantity"], 10.0)
        self.assertIsNone(short["net_change"])
        self.assertIsNone(short["average_price"])
        self.assertEqual(short["day_cost"], 0)


if __name__ == "__main__":
    unittest.main()
