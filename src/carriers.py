"""
Mock carrier simulation.

Simulates 6 real freight carriers returning rates based on shipment details.
Round 1: all eligible carriers respond with initial rates — subject to a
         configurable response window. Carriers that miss the window are excluded.
Round 2 (re-bid): non-winners are shown the current lowest rate and have a
                  chance to beat it, again within a shorter window.

Each carrier has a realistic response-time profile (faster carriers: Amazon/UPS;
slower: Estes/Saia). Response times are simulated numerically — no actual sleep
is added. A carrier is excluded if its response_time_ms exceeds window_ms.
"""

import random
from typing import Optional, List, Dict

from src.event_bus import EventCallback, Event

# The 6 carriers used at Wise Quote
CARRIERS = [
    {"id": "amazon-freight",  "name": "Amazon Freight"},
    {"id": "ups-freight",     "name": "UPS Freight"},
    {"id": "xpo-logistics",   "name": "XPO Logistics"},
    {"id": "old-dominion",    "name": "Old Dominion"},
    {"id": "estes-express",   "name": "Estes Express"},
    {"id": "saia-ltl",        "name": "Saia LTL Freight"},
]

CARRIER_MAP = {c["id"]: c for c in CARRIERS}

# Simulated response-time profiles per carrier (min/max ms).
# Lower = faster / more reliable. In production this maps to real email latency.
CARRIER_PROFILES = {
    "amazon-freight": {"min_ms": 200,  "max_ms": 1800},
    "ups-freight":    {"min_ms": 300,  "max_ms": 2200},
    "xpo-logistics":  {"min_ms": 400,  "max_ms": 3000},
    "old-dominion":   {"min_ms": 500,  "max_ms": 3500},
    "estes-express":  {"min_ms": 600,  "max_ms": 4500},
    "saia-ltl":       {"min_ms": 700,  "max_ms": 5000},
}
_DEFAULT_PROFILE = {"min_ms": 500, "max_ms": 4000}

# Default windows used when callers don't pass one (fallback only — callers
# should always read from the settings DB via bidding_engine.py)
DEFAULT_BIDDING_WINDOW_MS = 3000
DEFAULT_REBID_WINDOW_MS   = 1500


def _base_rate(weight_lbs: float, origin: str, destination: str) -> float:
    """
    Simple mock rate formula: base cost scales with weight and a
    pseudo-distance derived from the first chars of origin/destination.
    Real prod would call carrier APIs or a TMS.
    """
    distance_factor = (ord(origin[0]) + ord(destination[0])) % 20 + 5  # 5–24
    base = (weight_lbs / 100) * distance_factor * 0.85
    return round(base, 2)


def _simulated_response_time(carrier_id: str) -> int:
    """Return a random response time in ms drawn from the carrier's profile."""
    profile = CARRIER_PROFILES.get(carrier_id, _DEFAULT_PROFILE)
    return random.randint(profile["min_ms"], profile["max_ms"])


def collect_rates(
    request: dict,
    allowed_carrier_ids: Optional[List[str]] = None,
    window_ms: int = DEFAULT_BIDDING_WINDOW_MS,
    on_event: Optional[EventCallback] = None,
    elapsed_ms_offset: int = 0,
) -> List[Dict]:
    """
    Round 1: collect initial rates from all eligible carriers.

    Carriers are assigned a simulated response_time_ms. Those that respond
    within window_ms are included; the rest are treated as timed-out.

    Args:
        request:            the freight request dict
        allowed_carrier_ids: if set, only query these carriers
        window_ms:          deadline in simulated ms; late carriers are excluded
        on_event:           optional callback for carrier_bid / carrier_timeout events
        elapsed_ms_offset:  base elapsed time to add to each event's elapsed_ms

    Returns:
        list of in-time bid dicts, sorted by rate ascending
    """
    eligible = CARRIERS
    if allowed_carrier_ids:
        eligible = [c for c in CARRIERS if c["id"] in allowed_carrier_ids]

    base = _base_rate(
        weight_lbs=request.get("weight_lbs", 1000),
        origin=request.get("origin", "A"),
        destination=request.get("destination", "B"),
    )

    # Build all carrier responses with simulated timing
    candidates = []
    for carrier in eligible:
        response_time = _simulated_response_time(carrier["id"])
        noise = random.uniform(0.75, 1.25)
        rate = round(base * noise, 2)
        transit_days = random.randint(1, 5)
        candidates.append({
            "carrier_id":       carrier["id"],
            "carrier_name":     carrier["name"],
            "rate":             rate,
            "transit_days":     transit_days,
            "accessorials":     [],
            "round":            1,
            "response_time_ms": response_time,
        })

    # Sort by response time to emit events in arrival order
    candidates.sort(key=lambda b: b["response_time_ms"])

    in_time = []
    for bid in candidates:
        simulated_elapsed = elapsed_ms_offset + bid["response_time_ms"]
        if bid["response_time_ms"] <= window_ms:
            in_time.append(bid)
            if on_event:
                on_event(Event(
                    type="carrier_bid",
                    message=(
                        f"{bid['carrier_name']:<22} ${bid['rate']:>8.2f}   "
                        f"{bid['transit_days']} day(s)   "
                        f"(responded in {bid['response_time_ms']}ms)"
                    ),
                    data=bid,
                    elapsed_ms=simulated_elapsed,
                ))
        else:
            if on_event:
                on_event(Event(
                    type="carrier_timeout",
                    message=(
                        f"{bid['carrier_name']:<22} —        —        "
                        f"(timeout — missed {window_ms}ms window)"
                    ),
                    data={"carrier_id": bid["carrier_id"], "carrier_name": bid["carrier_name"],
                          "response_time_ms": bid["response_time_ms"], "window_ms": window_ms},
                    elapsed_ms=simulated_elapsed,
                ))

    return sorted(in_time, key=lambda b: b["rate"])


def collect_rebid_rates(
    current_lowest_rate: float,
    round1_bids: List[Dict],
    window_ms: int = DEFAULT_REBID_WINDOW_MS,
    on_event: Optional[EventCallback] = None,
    elapsed_ms_offset: int = 0,
) -> List[Dict]:
    """
    Round 2 (re-bid): non-winners are told the current lowest rate and
    asked "Can you beat $X?"

    Each challenger is assigned a new simulated response_time_ms. Those within
    window_ms AND choosing to counter-bid are included.

    40% chance each challenger submits a counter-bid that beats the current
    lowest by 1–8%.

    Returns:
        list of new bids from carriers that responded in time and chose to counter
    """
    winner_id = round1_bids[0]["carrier_id"]
    challengers = [b for b in round1_bids if b["carrier_id"] != winner_id]

    rebids = []
    for bid in challengers:
        response_time = _simulated_response_time(bid["carrier_id"])
        simulated_elapsed = elapsed_ms_offset + response_time

        if response_time > window_ms:
            if on_event:
                on_event(Event(
                    type="carrier_timeout",
                    message=(
                        f"{bid['carrier_name']:<22} —        "
                        f"(timeout — missed {window_ms}ms re-bid window)"
                    ),
                    data={"carrier_id": bid["carrier_id"], "carrier_name": bid["carrier_name"],
                          "response_time_ms": response_time, "window_ms": window_ms, "round": 2},
                    elapsed_ms=simulated_elapsed,
                ))
            continue  # missed the window

        if random.random() < 0.40:  # 40% chance to counter
            improvement = random.uniform(0.01, 0.08)
            new_rate = round(current_lowest_rate * (1 - improvement), 2)
            rebid = {
                "carrier_id":       bid["carrier_id"],
                "carrier_name":     bid["carrier_name"],
                "rate":             new_rate,
                "transit_days":     bid["transit_days"],
                "accessorials":     [],
                "round":            2,
                "beat_by_pct":      round(improvement * 100, 1),
                "response_time_ms": response_time,
            }
            rebids.append(rebid)
            if on_event:
                on_event(Event(
                    type="carrier_bid",
                    message=(
                        f"{bid['carrier_name']:<22} ${new_rate:>8.2f}   "
                        f"beat by {rebid['beat_by_pct']}%   "
                        f"(responded in {response_time}ms)"
                    ),
                    data={**rebid, "round": 2},
                    elapsed_ms=simulated_elapsed,
                ))
        # else: declined to counter — no event emitted (silent decline)

    return sorted(rebids, key=lambda b: b["rate"])
