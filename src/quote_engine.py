"""
Quote Engine — assembles the final structured quote output.

This is what gets "sent to the customer" (in production: emailed via Gmail,
or pushed to PandaDoc, or returned via API).
"""

import uuid
from datetime import datetime, timezone, timedelta
from src.customers import get_customer
from src.state_machine import transition
from src.markup_engine import apply_markup


QUOTE_EXPIRY_HOURS = 4  # customer has 4 hours to approve before re-quote


def build_quote(quote: dict) -> dict:
    """
    Assembles the final quote JSON from the populated quote record.
    Transitions state to QUOTE_SENT → AWAITING_APPROVAL.

    Returns:
        the final_quote dict (also stored on quote["final_quote"])
    """
    request = quote["request"]
    customer_id = request["customer_id"]
    customer = get_customer(customer_id)
    winning_bid = quote["winning_bid"]
    markup = quote["markup_result"]

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=QUOTE_EXPIRY_HOURS)).isoformat()

    final = {
        "quote_id": quote["quote_id"],
        "status": "QUOTE_SENT",
        "customer": {
            "id": customer_id,
            "name": customer["name"],
        },
        "shipment": {
            "origin": request.get("origin"),
            "destination": request.get("destination"),
            "weight_lbs": request.get("weight_lbs"),
            "cargo_type": request.get("cargo_type"),
            "pickup_date": request.get("pickup_date"),
            "hazmat": request.get("hazmat", False),
        },
        "selected_carrier": {
            "id": winning_bid["carrier_id"],
            "name": winning_bid["carrier_name"],
            "transit_days": winning_bid["transit_days"],
            "bid_round": winning_bid["round"],
        },
        "pricing": {
            "carrier_cost": markup["cost"],
            "markup_pct": markup["markup_pct"],
            "sell_rate": markup["sell_rate"],
            "gross_profit": markup["gross_profit"],
            "gross_margin_pct": markup["gross_margin_pct"],
        },
        "all_bids": {
            "round_1": quote["carrier_bids_round1"],
            "round_2": quote["carrier_bids_round2"],
        },
        "quote_expires_at": expires_at,
        "created_at": quote["created_at"],
        "audit_log": quote["audit_log"],
    }

    quote["final_quote"] = final

    transition(quote, "QUOTE_SENT", {"sell_rate": markup["sell_rate"], "expires_at": expires_at})
    transition(quote, "AWAITING_APPROVAL", {"message": "Quote delivered to customer"})

    return final


def generate_customer_email(quote: dict) -> str:
    """
    Compose a ready-to-send quote email for the customer.
    Shows only what the customer should see: shipment details, carrier,
    sell rate, transit time, and expiry. No internal cost/markup data.
    """
    fq = quote.get("final_quote") or {}
    req = quote.get("request", {})
    carrier = fq.get("selected_carrier", {})
    pricing = fq.get("pricing", {})
    customer_name = fq.get("customer", {}).get("name", "Valued Customer")
    quote_id = quote.get("quote_id", "")

    origin      = req.get("origin", "—")
    destination = req.get("destination", "—")
    weight      = req.get("weight_lbs", "—")
    cargo       = req.get("cargo_type", "General Merchandise")
    pickup      = req.get("pickup_date", "—")
    hazmat      = req.get("hazmat", False)

    carrier_name  = carrier.get("name", "—")
    transit_days  = carrier.get("transit_days", "—")
    sell_rate     = pricing.get("sell_rate")
    expires_raw   = fq.get("quote_expires_at", "")
    expires_fmt   = expires_raw[:16].replace("T", " ") + " UTC" if expires_raw else "—"

    sell_rate_str = f"${sell_rate:,.2f}" if sell_rate else "—"
    weight_str    = f"{weight:,.0f} lbs" if isinstance(weight, (int, float)) else str(weight)
    hazmat_line   = "\n- ⚠️  Hazardous materials — carrier is certified for this shipment" if hazmat else ""

    return (
        f"Subject: Freight Quote #{quote_id} — {origin} → {destination}\n"
        f"{'─' * 60}\n\n"
        f"Dear {customer_name},\n\n"
        f"Thank you for your freight inquiry. We're pleased to share the following quote for your shipment:\n\n"
        f"── Shipment Details ──────────────────────────────────────\n"
        f"  Route:        {origin} → {destination}\n"
        f"  Cargo:        {cargo}\n"
        f"  Weight:       {weight_str}\n"
        f"  Pickup Date:  {pickup}{hazmat_line}\n\n"
        f"── Quote Summary ─────────────────────────────────────────\n"
        f"  Carrier:          {carrier_name}\n"
        f"  Estimated Transit: {transit_days} business day(s)\n"
        f"  Freight Rate:     {sell_rate_str}\n"
        f"  Quote Reference:  {quote_id}\n"
        f"  Valid Until:      {expires_fmt}\n\n"
        f"To confirm this shipment, simply reply to this email or contact your account manager. "
        f"Please note this rate is valid for a limited time — we recommend confirming promptly.\n\n"
        f"If you have any questions or need adjustments, don't hesitate to reach out.\n\n"
        f"Best regards,\n"
        f"Wise Quote Logistics Team\n"
        f"freight@wisequote.com\n"
    )


def log_email_sent(quote: dict, email_text: str) -> dict:
    """
    Record that the customer quote email was dispatched.
    Stores the sent email body in the quote and appends an audit entry.
    Does NOT change quote status — the quote stays in AWAITING_APPROVAL.
    """
    from src.database import save_quote

    now = datetime.now(timezone.utc).isoformat()
    quote["customer_email_sent"] = {
        "body": email_text,
        "sent_at": now,
    }
    quote["audit_log"].append({
        "id":        str(uuid.uuid4())[:8],
        "timestamp": now,
        "type":      "customer_email_sent",
        "state":     quote.get("status", ""),
        "data": {
            "note": "Quote email composed and marked as sent to customer.",
            "sent_by": "dashboard",
        },
    })
    save_quote(quote)
    return quote


def apply_bid_override(
    quote: dict,
    override_carrier_name: str,
    override_rate: float,
    override_transit_days: int = 3,
    override_carrier_id: str = "",
    override_reason: str = "",
) -> dict:
    """
    Replace the winning bid with a manually specified carrier and rate.
    Recalculates markup and updates final_quote pricing in-place.
    Appends an audit log entry. Does NOT change quote status.

    Returns the updated quote dict (same object, mutated).
    """
    from src.database import save_quote

    carrier_id = override_carrier_id or override_carrier_name.lower().replace(" ", "-")

    new_bid = {
        "carrier_id":       carrier_id,
        "carrier_name":     override_carrier_name,
        "rate":             override_rate,
        "transit_days":     override_transit_days,
        "round":            "manual_override",
        "accessorials":     [],
        "response_time_ms": 0,
    }

    old_bid = quote.get("winning_bid")
    quote["winning_bid"] = new_bid

    # Recalculate markup
    customer_id = quote["request"]["customer_id"]
    new_markup = apply_markup(customer_id, override_rate)
    quote["markup_result"] = new_markup

    # Update final_quote in-place if it exists (avoids re-running state transitions)
    if quote.get("final_quote"):
        fq = quote["final_quote"]
        fq["selected_carrier"] = {
            "id":          carrier_id,
            "name":        override_carrier_name,
            "transit_days": override_transit_days,
            "bid_round":   "manual_override",
        }
        fq["pricing"] = {
            "carrier_cost":     new_markup["cost"],
            "markup_pct":       new_markup["markup_pct"],
            "sell_rate":        new_markup["sell_rate"],
            "gross_profit":     new_markup["gross_profit"],
            "gross_margin_pct": new_markup["gross_margin_pct"],
        }
        fq["override_applied"] = True

    # Audit log entry
    now = datetime.now(timezone.utc).isoformat()
    quote["audit_log"].append({
        "id":        str(uuid.uuid4())[:8],
        "timestamp": now,
        "type":      "manual_override",
        "state":     quote.get("status", ""),
        "data": {
            "previous_carrier": old_bid.get("carrier_name") if old_bid else None,
            "previous_rate":    old_bid.get("rate") if old_bid else None,
            "new_carrier":      override_carrier_name,
            "new_rate":         override_rate,
            "reason":           override_reason,
            "overridden_by":    "dashboard",
        },
    })

    save_quote(quote)
    return quote
