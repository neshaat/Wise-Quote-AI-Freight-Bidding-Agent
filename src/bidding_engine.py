"""
Bidding Engine — orchestrates two-round competitive carrier bidding.

Round 1: collect rates from all eligible carriers within the bidding window.
Round 2: non-winners are shown the current lowest rate and invited to beat it
         within a shorter re-bid window.

Both windows are read from the settings DB (bidding_window_ms / rebid_window_ms)
so they're configurable from the dashboard without a code change.
"""

from typing import Optional

from src.carriers import collect_rates, collect_rebid_rates, CARRIERS
from src.customers import get_customer
from src.state_machine import transition
from src.event_bus import Event, EventCallback


def _get_windows() -> tuple:
    """Read bidding window settings from DB, fall back to hardcoded defaults."""
    try:
        from src.database import get_setting
        w1 = int(get_setting("bidding_window_ms", "3000"))
        w2 = int(get_setting("rebid_window_ms",   "1500"))
        return w1, w2
    except Exception:
        return 3000, 1500


def run_bidding(quote: dict, on_event: Optional[EventCallback] = None) -> dict:
    """
    Run the full two-round bidding process for a quote.

    Updates quote in-place with carrier_bids_round1, carrier_bids_round2,
    carrier_timeouts_round1, carrier_timeouts_round2, and winning_bid.

    Returns the winning bid dict.
    Raises RuntimeError if no carriers respond in Round 1.
    """
    request = quote["request"]
    customer = get_customer(request["customer_id"])
    allowed_carriers = customer.get("preferred_carriers")  # None = any

    bidding_window_ms, rebid_window_ms = _get_windows()

    # ── Round 1 ───────────────────────────────────────────────────────────────
    eligible = CARRIERS if not allowed_carriers else [
        c for c in CARRIERS if c["id"] in allowed_carriers
    ]

    if on_event:
        on_event(Event(
            type="bidding_window_open",
            message=(
                f"Bidding window open — Round 1  "
                f"({bidding_window_ms}ms window, {len(eligible)} carrier(s))"
            ),
            data={"round": 1, "window_ms": bidding_window_ms, "n_eligible": len(eligible)},
        ))

    transition(quote, "OUT_TO_CARRIERS", {
        "message": "Sending rate request to carriers",
        "allowed_carriers": allowed_carriers or "all",
    })

    round1_bids = collect_rates(
        request,
        allowed_carrier_ids=allowed_carriers,
        window_ms=bidding_window_ms,
        on_event=on_event,
    )

    if not round1_bids:
        raise RuntimeError(
            "No carriers responded in Round 1 within the bidding window. "
            "Try increasing the bidding window in ⚙️ Model Settings."
        )

    quote["carrier_bids_round1"] = round1_bids
    responded_ids = {b["carrier_id"] for b in round1_bids}
    quote["carrier_timeouts_round1"] = [
        c["name"] for c in eligible if c["id"] not in responded_ids
    ]

    lowest_round1 = round1_bids[0]

    transition(quote, "FIRST_ROUND_RECEIVED", {
        "bids_received":  len(round1_bids),
        "lowest_rate":    lowest_round1["rate"],
        "lowest_carrier": lowest_round1["carrier_name"],
    })

    timed_out_r1 = len(quote["carrier_timeouts_round1"])
    if on_event:
        on_event(Event(
            type="round_complete",
            message=(
                f"Round 1 done — {len(round1_bids)} responded"
                + (f", {timed_out_r1} timed out" if timed_out_r1 else "")
                + f" — lowest: ${lowest_round1['rate']:.2f} ({lowest_round1['carrier_name']})"
            ),
            data={
                "round": 1,
                "bids_received": len(round1_bids),
                "timed_out": timed_out_r1,
                "lowest_rate": lowest_round1["rate"],
                "lowest_carrier": lowest_round1["carrier_name"],
            },
        ))

    # ── Round 2 (re-bid) — only if 2+ carriers responded in Round 1 ──────────
    round2_bids = []
    if len(round1_bids) >= 2:
        n_challengers = len(round1_bids) - 1
        if on_event:
            on_event(Event(
                type="bidding_window_open",
                message=(
                    f"Re-bid window open — Round 2  "
                    f"({rebid_window_ms}ms window, {n_challengers} challenger(s), "
                    f"beat ${lowest_round1['rate']:.2f})"
                ),
                data={"round": 2, "window_ms": rebid_window_ms,
                      "target_rate": lowest_round1["rate"], "n_challengers": n_challengers},
            ))

        transition(quote, "REBID_ROUND", {
            "message": (
                f"Notifying carriers: current lowest is "
                f"${lowest_round1['rate']:.2f}. Can you beat it?"
            ),
            "challengers": [b["carrier_name"] for b in round1_bids[1:]],
        })

        round2_bids = collect_rebid_rates(
            current_lowest_rate=lowest_round1["rate"],
            round1_bids=round1_bids,
            window_ms=rebid_window_ms,
            on_event=on_event,
            elapsed_ms_offset=bidding_window_ms,
        )
        quote["carrier_bids_round2"] = round2_bids
        quote["carrier_timeouts_round2"] = []

        if on_event:
            if round2_bids:
                best_r2 = round2_bids[0]
                on_event(Event(
                    type="round_complete",
                    message=(
                        f"Round 2 done — {len(round2_bids)} counter-bid(s)"
                        + f" — best: ${best_r2['rate']:.2f} ({best_r2['carrier_name']})"
                    ),
                    data={"round": 2, "bids_received": len(round2_bids),
                          "best_rate": best_r2["rate"], "best_carrier": best_r2["carrier_name"]},
                ))
            else:
                on_event(Event(
                    type="round_complete",
                    message="Round 2 done — no challengers beat the current lowest rate",
                    data={"round": 2, "bids_received": 0},
                ))
    else:
        quote["carrier_bids_round2"] = []
        quote["carrier_timeouts_round2"] = []

    # ── Select winner ─────────────────────────────────────────────────────────
    all_bids = round1_bids + round2_bids
    winner = min(all_bids, key=lambda b: b["rate"])
    quote["winning_bid"] = winner
    return winner
