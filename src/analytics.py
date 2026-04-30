"""
Analytics engine — computes KPIs from persisted quotes.

All metrics are derived from the quotes table in SQLite.
No live agent state is used here; this is a pure read path.
"""

from datetime import datetime, timezone
from typing import Dict, Any

from src.database import get_all_quotes, DEFAULT_DB_PATH


def compute_analytics(db_path: str = DEFAULT_DB_PATH) -> Dict[str, Any]:
    """
    Compute all KPIs from persisted quotes.

    Returns a dict with:
      - top-level aggregate metrics
      - by_carrier: win rate + avg margin per carrier
      - by_customer: win rate + avg margin per customer
      - by_status: count per pipeline stage
      - carrier_competitiveness: bids, wins, win rate per carrier
      - recent_quotes: last 10 quotes (lightweight)
    """
    quotes = get_all_quotes(db_path)

    if not quotes:
        return _empty_analytics()

    # ── Aggregate metrics ─────────────────────────────────────────────────────
    total = len(quotes)
    approved = [q for q in quotes if q.get("status") == "APPROVED"
                or q.get("status") in ("IN_TRANSIT", "COMPLETED")]
    lost = [q for q in quotes if q.get("status") == "LOST"]
    quote_sent = [q for q in quotes if q.get("status") not in ("INTAKE", "OUT_TO_CARRIERS",
                  "FIRST_ROUND_RECEIVED", "REBID_ROUND")]

    approved_count = len(approved)
    lost_count = len(lost)
    decided = approved_count + lost_count
    win_rate = (approved_count / decided * 100) if decided > 0 else 0.0

    # Pull pricing from final_quote.pricing or markup_result
    def _pricing(q):
        fq = q.get("final_quote") or {}
        return fq.get("pricing") or q.get("markup_result") or {}

    sell_rates = [_pricing(q).get("sell_rate") for q in quotes if _pricing(q).get("sell_rate")]
    costs = [_pricing(q).get("cost") or _pricing(q).get("carrier_cost")
             for q in quotes if _pricing(q).get("cost") or _pricing(q).get("carrier_cost")]
    profits = [_pricing(q).get("gross_profit") for q in quotes if _pricing(q).get("gross_profit")]
    markups = [_pricing(q).get("markup_pct") for q in quotes if _pricing(q).get("markup_pct")]
    margin_pcts = [_pricing(q).get("gross_margin_pct")
                   for q in quotes if _pricing(q).get("gross_margin_pct")]

    def _avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    # ── By status ─────────────────────────────────────────────────────────────
    from src.state_machine import STAGES
    by_status = {stage: 0 for stage in STAGES}
    for q in quotes:
        s = q.get("status", "INTAKE")
        if s in by_status:
            by_status[s] += 1

    # ── By customer ───────────────────────────────────────────────────────────
    by_customer: Dict[str, Any] = {}
    for q in quotes:
        cid = q.get("request", {}).get("customer_id", "UNKNOWN")
        if cid not in by_customer:
            by_customer[cid] = {"quotes": 0, "wins": 0, "profits": []}
        by_customer[cid]["quotes"] += 1
        if q.get("status") in ("APPROVED", "IN_TRANSIT", "COMPLETED"):
            by_customer[cid]["wins"] += 1
        p = _pricing(q).get("gross_profit")
        if p:
            by_customer[cid]["profits"].append(p)

    by_customer_out = {}
    for cid, d in by_customer.items():
        win_r = round(d["wins"] / d["quotes"] * 100, 1) if d["quotes"] > 0 else 0.0
        by_customer_out[cid] = {
            "quotes": d["quotes"],
            "wins": d["wins"],
            "win_rate_pct": win_r,
            "avg_gross_profit": _avg(d["profits"]),
        }

    # ── By carrier (wins + competitiveness) ───────────────────────────────────
    carrier_stats: Dict[str, Any] = {}

    def _track_bid(carrier_name, carrier_id, won=False, profit=None):
        key = carrier_name or carrier_id or "Unknown"
        if key not in carrier_stats:
            carrier_stats[key] = {"bids": 0, "wins": 0, "profits": []}
        carrier_stats[key]["bids"] += 1
        if won:
            carrier_stats[key]["wins"] += 1
        if profit:
            carrier_stats[key]["profits"].append(profit)

    for q in quotes:
        winning = q.get("winning_bid") or {}
        win_carrier = winning.get("carrier_name") or winning.get("carrier_id")
        profit = _pricing(q).get("gross_profit")
        is_won = q.get("status") in ("APPROVED", "IN_TRANSIT", "COMPLETED")

        # Count all round 1 bids
        for bid in q.get("carrier_bids_round1", []):
            name = bid.get("carrier_name") or bid.get("carrier_id")
            won = is_won and name == win_carrier
            _track_bid(name, bid.get("carrier_id"), won=won, profit=profit if won else None)

        # Count round 2 bids (only non-winners re-bid, so don't double-count round1 winner)
        for bid in q.get("carrier_bids_round2", []):
            name = bid.get("carrier_name") or bid.get("carrier_id")
            won = is_won and name == win_carrier
            _track_bid(name, bid.get("carrier_id"), won=won, profit=profit if won else None)

    by_carrier_out = {}
    for name, d in carrier_stats.items():
        win_r = round(d["wins"] / d["bids"] * 100, 1) if d["bids"] > 0 else 0.0
        by_carrier_out[name] = {
            "bids": d["bids"],
            "wins": d["wins"],
            "win_rate_pct": win_r,
            "avg_gross_profit": _avg(d["profits"]),
            "competitiveness_score": round(win_r / 100, 3),
        }

    # ── Turnaround time (INTAKE → QUOTE_SENT) ─────────────────────────────────
    turnarounds = []
    for q in quotes:
        t_intake = None
        t_sent = None
        for entry in q.get("audit_log", []):
            if entry.get("to") == "INTAKE" and t_intake is None:
                t_intake = entry.get("timestamp")
            if entry.get("to") == "QUOTE_SENT":
                t_sent = entry.get("timestamp")
        if t_intake and t_sent:
            try:
                dt_in = datetime.fromisoformat(t_intake)
                dt_out = datetime.fromisoformat(t_sent)
                turnarounds.append((dt_out - dt_in).total_seconds())
            except Exception:
                pass

    avg_turnaround = _avg(turnarounds)

    # ── Quote-to-approval % ───────────────────────────────────────────────────
    sent_count = len(quote_sent)
    q_to_a = round(approved_count / sent_count * 100, 1) if sent_count > 0 else 0.0

    # ── Recent quotes (lightweight, last 10) ──────────────────────────────────
    recent = []
    for q in quotes[:10]:
        fq = q.get("final_quote") or {}
        p = _pricing(q)
        w = q.get("winning_bid") or {}
        sc = fq.get("selected_carrier") or {}
        recent.append({
            "quote_id": q["quote_id"],
            "customer_id": q.get("request", {}).get("customer_id", ""),
            "status": q.get("status"),
            "origin": q.get("request", {}).get("origin", ""),
            "destination": q.get("request", {}).get("destination", ""),
            "weight_lbs": q.get("request", {}).get("weight_lbs"),
            "carrier_name": sc.get("name") or w.get("carrier_name", ""),
            "sell_rate": p.get("sell_rate"),
            "gross_profit": p.get("gross_profit"),
            "markup_pct": p.get("markup_pct"),
            "created_at": q.get("created_at", ""),
        })

    return {
        "total_quotes": total,
        "approved_count": approved_count,
        "lost_count": lost_count,
        "win_rate_pct": round(win_rate, 1),
        "avg_sell_rate": _avg(sell_rates),
        "avg_carrier_cost": _avg(costs),
        "avg_gross_profit": _avg(profits),
        "total_gross_profit": round(sum(profits), 2) if profits else 0.0,
        "avg_markup_pct": _avg(markups),
        "avg_gross_margin_pct": _avg(margin_pcts),
        "avg_turnaround_secs": avg_turnaround,
        "quote_to_approval_pct": q_to_a,
        "by_status": by_status,
        "by_customer": by_customer_out,
        "by_carrier": by_carrier_out,
        "recent_quotes": recent,
    }


def _empty_analytics() -> Dict[str, Any]:
    from src.state_machine import STAGES
    return {
        "total_quotes": 0,
        "approved_count": 0,
        "lost_count": 0,
        "win_rate_pct": 0.0,
        "avg_sell_rate": 0.0,
        "avg_carrier_cost": 0.0,
        "avg_gross_profit": 0.0,
        "total_gross_profit": 0.0,
        "avg_markup_pct": 0.0,
        "avg_gross_margin_pct": 0.0,
        "avg_turnaround_secs": 0.0,
        "quote_to_approval_pct": 0.0,
        "by_status": {stage: 0 for stage in STAGES},
        "by_customer": {},
        "by_carrier": {},
        "recent_quotes": [],
    }
