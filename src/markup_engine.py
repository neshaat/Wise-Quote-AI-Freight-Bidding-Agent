"""
Markup Engine — applies customer-specific markup to the winning carrier cost.

Outputs:
  cost          — what Wise Quote pays the carrier
  markup_pct    — percentage from the customer's profile
  sell_rate     — what the customer is charged
  gross_profit  — sell_rate - cost
  gross_margin_pct — gross_profit / sell_rate * 100
"""

from src.customers import get_customer


def apply_markup(customer_id: str, cost: float) -> dict:
    """
    Apply the customer-specific markup to the carrier cost.

    Args:
        customer_id: e.g. "CUST-A"
        cost: the winning carrier rate in dollars

    Returns:
        dict with full pricing breakdown
    """
    customer = get_customer(customer_id)
    markup_pct = customer["markup_pct"]

    sell_rate = round(cost * (1 + markup_pct / 100), 2)
    gross_profit = round(sell_rate - cost, 2)
    gross_margin_pct = round((gross_profit / sell_rate) * 100, 2) if sell_rate > 0 else 0

    return {
        "cost": cost,
        "markup_pct": markup_pct,
        "sell_rate": sell_rate,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
    }
