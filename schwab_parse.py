"""
Turns Schwab's accounts+positions JSON into the portfolio structure used by the
briefing. Kept free of Schwab/Anthropic imports so it can be tested without
credentials.
"""


def parse_accounts(accounts_data: list) -> list:
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
                # Inputs for pl_guard's reconstruction when the broker's day
                # P&L is corrupt (see pl_guard.assess_with_reconstruction).
                "short_quantity": pos.get("shortQuantity", 0.0),
                "net_change": instrument.get("netChange"),   # per-share day change; absent for some ETFs
                "day_cost": pos.get("currentDayCost", 0),
                "average_price": pos.get("averagePrice"),
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
