"""
State Machine — tracks pipeline stage and full audit log for every quote.

Pipeline stages (from Wise Quote scope doc §6):
  INTAKE → OUT_TO_CARRIERS → FIRST_ROUND_RECEIVED → REBID_ROUND →
  QUOTE_SENT → AWAITING_APPROVAL → APPROVED → IN_TRANSIT → COMPLETED
                                             ↘ LOST
"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Valid pipeline stages in order
STAGES = [
    "INTAKE",
    "OUT_TO_CARRIERS",
    "FIRST_ROUND_RECEIVED",
    "REBID_ROUND",
    "QUOTE_SENT",
    "AWAITING_APPROVAL",
    "APPROVED",
    "IN_TRANSIT",
    "COMPLETED",
    "LOST",
]

# Which transitions are allowed (from → set of valid nexts)
VALID_TRANSITIONS: Dict[str, List[str]] = {
    "INTAKE":                ["OUT_TO_CARRIERS"],
    "OUT_TO_CARRIERS":       ["FIRST_ROUND_RECEIVED"],
    "FIRST_ROUND_RECEIVED":  ["REBID_ROUND", "QUOTE_SENT"],  # skip rebid if only 1 carrier
    "REBID_ROUND":           ["QUOTE_SENT"],
    "QUOTE_SENT":            ["AWAITING_APPROVAL"],
    "AWAITING_APPROVAL":     ["APPROVED", "LOST"],
    "APPROVED":              ["IN_TRANSIT"],
    "IN_TRANSIT":            ["COMPLETED"],
    "COMPLETED":             [],
    "LOST":                  [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_quote(request: dict) -> dict:
    """
    Initialize a new quote record from a freight request.
    Generates a deterministic quote ID based on key fields.
    """
    key = f"{request['origin']}|{request['destination']}|{request['customer_id']}|{request.get('pickup_date','')}"
    quote_id = "QT-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()

    quote = {
        "quote_id": quote_id,
        "status": "INTAKE",
        "request": request,
        "carrier_bids_round1": [],
        "carrier_bids_round2": [],
        "winning_bid": None,
        "markup_result": None,
        "final_quote": None,
        "created_at": _now(),
        "audit_log": [],
    }

    # Log the initial state entry
    _append_log(quote, from_state=None, to_state="INTAKE", data={"request": request})
    return quote


def transition(quote: dict, next_state: str, data: Optional[Dict] = None) -> dict:
    """
    Move the quote to the next pipeline stage.
    Validates the transition is legal, appends to audit log.
    Raises ValueError for illegal transitions.
    """
    current = quote["status"]
    allowed = VALID_TRANSITIONS.get(current, [])

    if next_state not in allowed:
        raise ValueError(
            f"Invalid transition: {current} → {next_state}. "
            f"Allowed: {allowed}"
        )

    _append_log(quote, from_state=current, to_state=next_state, data=data or {})
    quote["status"] = next_state
    return quote


def _append_log(quote: dict, from_state: Optional[str], to_state: str, data: Any) -> None:
    quote["audit_log"].append({
        "id": str(uuid.uuid4())[:8],
        "timestamp": _now(),
        "from": from_state,
        "to": to_state,
        "data": data,
    })


def get_audit_summary(quote: dict) -> List[str]:
    """Returns a human-readable list of state transitions for printing."""
    lines = []
    for entry in quote["audit_log"]:
        ts = entry["timestamp"]
        if entry.get("type") == "note":
            note = entry.get("data", {}).get("agent_note", "")
            lines.append(f"  [{ts}] NOTE ({entry['state']}): {note[:80]}")
        elif entry["from"] is None:
            lines.append(f"  [{ts}] CREATED → {entry['to']}")
        else:
            lines.append(f"  [{ts}] {entry['from']} → {entry['to']}")
    return lines
