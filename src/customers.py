"""
Customer profiles — markup rules and carrier preferences.

Each customer has:
  - markup_pct: the percentage added on top of carrier cost
  - preferred_carriers: list of allowed carrier IDs, or None (= any carrier)
"""

from typing import Dict, List, Optional

CUSTOMERS: Dict[str, dict] = {
    "CUST-A": {
        "name": "Acme Corp",
        "markup_pct": 5,
        # Acme only wants Amazon Freight (they have a relationship with them)
        "preferred_carriers": ["amazon-freight"],
    },
    "CUST-B": {
        "name": "Beta Imports",
        "markup_pct": 12,
        # Beta only wants UPS Freight
        "preferred_carriers": ["ups-freight"],
    },
    "CUST-C": {
        "name": "Gamma LLC",
        "markup_pct": 30,
        # No preference — take the cheapest from any carrier
        "preferred_carriers": None,
    },
    "CUST-D": {
        "name": "Delta Co",
        "markup_pct": 10,
        "preferred_carriers": None,
    },
}

DEFAULT_MARKUP_PCT = 10  # fallback if customer not in DB


def get_customer(customer_id: str) -> dict:
    """
    Returns the customer profile.
    Checks the SQLite DB first (so UI-created customers are found),
    then falls back to the hardcoded dict, then a safe default.
    """
    try:
        from src.database import get_customer_from_db
        db_customer = get_customer_from_db(customer_id)
        if db_customer:
            return db_customer
    except Exception:
        pass  # DB not available — fall through to hardcoded dict

    if customer_id in CUSTOMERS:
        return CUSTOMERS[customer_id]

    return {
        "name": f"Unknown ({customer_id})",
        "markup_pct": DEFAULT_MARKUP_PCT,
        "preferred_carriers": None,
    }
