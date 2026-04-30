"""
Wise Quote Freight Agent — Streamlit Dashboard

Three tabs:
  1. New Quote  — form intake + agent runner + result card
  2. Pipeline   — filterable quote table + approve/reject actions
  3. Analytics  — KPI cards + Plotly charts
"""

import json
import sqlite3
import sys
import os

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import run_agent
from src.providers import get_provider
from src.customers import CUSTOMERS
import requests as _requests

from src.database import (
    list_quotes, load_quote, update_quote_status, save_quote, init_db, DEFAULT_DB_PATH,
    save_customer, list_customers,
    get_setting, set_setting,
    queue_inbox_message, list_inbox_messages, mark_inbox_processed,
)
from src.email_parser import parse_freight_email, validate_parsed_request, normalize_freight_request, DEMO_EMAIL_TEMPLATES
from src.quote_engine import apply_bid_override, generate_customer_email, log_email_sent
from src.carriers import CARRIERS
from src.analytics import compute_analytics
from src.state_machine import STAGES

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Wise Quote Freight Agent",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Ensure DB exists on startup
init_db()

# ── Model settings helpers ────────────────────────────────────────────────────

def _fetch_ollama_models() -> list[str]:
    """Query the local Ollama server for downloaded models. Returns [] if unreachable."""
    try:
        resp = _requests.get("http://localhost:11434/api/tags", timeout=2)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def _load_model_settings() -> dict:
    """Return the saved provider/model/api-key settings, with Ollama defaults."""
    return {
        "provider":          get_setting("provider", "ollama"),
        "ollama_model":      get_setting("ollama_model", "qwen3:14b"),
        "openai_model":      get_setting("openai_model", "gpt-4o-mini"),
        "openai_api_key":    get_setting("openai_api_key", ""),
        "anthropic_model":   get_setting("anthropic_model", "claude-sonnet-4-6"),
        "anthropic_api_key": get_setting("anthropic_api_key", ""),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "INTAKE":              "#6c757d",
    "OUT_TO_CARRIERS":     "#0d6efd",
    "FIRST_ROUND_RECEIVED": "#0dcaf0",
    "REBID_ROUND":         "#6610f2",
    "QUOTE_SENT":          "#fd7e14",
    "AWAITING_APPROVAL":   "#ffc107",
    "APPROVED":            "#198754",
    "IN_TRANSIT":          "#20c997",
    "COMPLETED":           "#0d6efd",
    "LOST":                "#dc3545",
}

STATUS_EMOJI = {
    "INTAKE":              "📥",
    "OUT_TO_CARRIERS":     "📤",
    "FIRST_ROUND_RECEIVED": "📊",
    "REBID_ROUND":         "🔄",
    "QUOTE_SENT":          "📨",
    "AWAITING_APPROVAL":   "⏳",
    "APPROVED":            "✅",
    "IN_TRANSIT":          "🚛",
    "COMPLETED":           "🎉",
    "LOST":                "❌",
}

def _load_customer_options() -> dict:
    """Load customers from DB for dropdowns. Falls back to hardcoded dict."""
    try:
        db_customers = list_customers()
        if db_customers:
            return {
                c["customer_id"]: f"{c['customer_id']} — {c['name']} ({c['markup_pct']}% markup)"
                for c in db_customers
            }
    except Exception:
        pass
    return {
        cid: f"{cid} — {info['name']} ({info['markup_pct']}% markup)"
        for cid, info in CUSTOMERS.items()
    }

PROVIDER_OPTIONS = ["claude", "openai", "ollama"]


def _fmt_usd(val):
    if val is None:
        return "—"
    return f"${val:,.2f}"


def _status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#6c757d")
    emoji = STATUS_EMOJI.get(status, "")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:12px;font-size:0.8em;font-weight:600">{emoji} {status}</span>'


# ── Tab layout ────────────────────────────────────────────────────────────────
tab_new, tab_email, tab_pipeline, tab_analytics, tab_customers, tab_models = st.tabs(
    ["➕ New Quote", "📧 Email Inbox", "📋 Pipeline", "📊 Analytics", "👥 Customers", "⚙️ Model Settings"]
)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — New Quote
# ═══════════════════════════════════════════════════════════════════════════════
with tab_new:
    st.header("New Freight Quote")
    st.caption("Fill in the shipment details and run the agent to generate a competitive quote.")

    with st.form("quote_form"):
        col1, col2 = st.columns(2)
        with col1:
            origin = st.text_input("Origin", placeholder="Chicago, IL")
            destination = st.text_input("Destination", placeholder="Los Angeles, CA")
            weight_lbs = st.number_input("Weight (lbs)", min_value=1, max_value=100000, value=4500)
            cargo_type = st.text_input("Cargo Type", value="General Merchandise (Palletized)")

        with col2:
            pickup_date = st.date_input("Pickup Date")
            customer_options = _load_customer_options()
            customer_id = st.selectbox(
                "Customer",
                options=list(customer_options.keys()),
                format_func=lambda k: customer_options[k],
            )
            hazmat = st.checkbox("Hazmat", value=False)

        submitted = st.form_submit_button("▶ Run Agent", type="primary", use_container_width=True)

    _active = _load_model_settings()
    _active_provider = _active["provider"]
    _active_model = _active.get(f"{_active_provider}_model") or _active.get("ollama_model", "—")
    st.caption(f"Agent will use **{_active_provider} / {_active_model}** — change in ⚙️ Model Settings")

    if submitted:
        if not origin or not destination:
            st.error("Origin and Destination are required.")
        else:
            freight_request = {
                "origin": origin,
                "destination": destination,
                "weight_lbs": weight_lbs,
                "cargo_type": cargo_type,
                "pickup_date": str(pickup_date),
                "customer_id": customer_id,
                "hazmat": hazmat,
            }
            # Clear any previous result before starting a new run
            st.session_state.pop("last_quote", None)
            st.session_state.pop("last_quote_error", None)
            st.session_state["quote_events"] = []

            # ── Event type metadata ─────────────────────────────────────────
            _EVENT_ICON = {
                "state_transition":   "🔄",
                "bidding_window_open": "📤",
                "carrier_bid":        "✅",
                "carrier_timeout":    "⌛",
                "round_complete":     "📊",
                "tool_call":          "🔧",
                "tool_result":        "💡",
                "agent_reasoning":    "🤖",
                "quote_complete":     "🎉",
            }

            with st.status("Running freight bidding agent…", expanded=True) as _status:
                feed_placeholder = st.empty()

                def _on_event(event):
                    evs = st.session_state.get("quote_events", [])
                    evs.append({
                        "type":       event.type,
                        "message":    event.message,
                        "elapsed_ms": event.elapsed_ms,
                        "timestamp":  event.timestamp,
                    })
                    st.session_state["quote_events"] = evs
                    # Re-render the live feed
                    lines = []
                    for ev in evs:
                        icon = _EVENT_ICON.get(ev["type"], "•")
                        secs = ev["elapsed_ms"] / 1000
                        lines.append(f"`{secs:>5.1f}s` {icon} {ev['message']}")
                    feed_placeholder.markdown("\n\n".join(lines))

                try:
                    _saved = _load_model_settings()
                    _provider_name = _saved["provider"]
                    if _provider_name == "ollama":
                        _model = _saved["ollama_model"]
                    elif _provider_name == "openai":
                        _model = _saved["openai_model"]
                        if _saved.get("openai_api_key"):
                            os.environ.setdefault("OPENAI_API_KEY", _saved["openai_api_key"])
                    else:
                        _model = _saved["anthropic_model"]
                        if _saved.get("anthropic_api_key"):
                            os.environ.setdefault("ANTHROPIC_API_KEY", _saved["anthropic_api_key"])
                    provider = get_provider(_provider_name, model=_model)
                    quote = run_agent(freight_request, provider=provider, verbose=False, on_event=_on_event)
                    st.session_state["last_quote"] = quote
                    _status.update(label="Agent complete ✅", state="complete", expanded=False)
                except Exception as e:
                    st.session_state["last_quote_error"] = str(e)
                    _status.update(label="Agent error ❌", state="error", expanded=False)

    # ── Result display — reads from session_state so it survives tab switches ──
    if st.session_state.get("last_quote_error"):
        st.error(f"Agent error: {st.session_state['last_quote_error']}")

    quote = st.session_state.get("last_quote")
    if quote and quote.get("final_quote"):
        fq = quote["final_quote"]
        p = fq["pricing"]
        c = fq["selected_carrier"]

        col_title, col_clear = st.columns([5, 1])
        col_title.success(f"Quote generated: **{fq['quote_id']}**")
        if col_clear.button("✕ Clear", key="clear_quote"):
            st.session_state.pop("last_quote", None)
            st.session_state.pop("last_quote_error", None)
            st.rerun()

        # Result card
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Carrier", c["name"])
        r2.metric("Sell Rate", _fmt_usd(p["sell_rate"]))
        r3.metric("Gross Profit", _fmt_usd(p["gross_profit"]))
        r4.metric("Margin", f"{p['gross_margin_pct']}%")

        with st.expander("📊 Full Bid Comparison"):
            bids_r1 = quote.get("carrier_bids_round1", [])
            bids_r2 = quote.get("carrier_bids_round2", [])
            if bids_r1:
                st.subheader("Round 1")
                st.dataframe(
                    pd.DataFrame(bids_r1)[["carrier_name", "rate", "transit_days"]],
                    use_container_width=True,
                    hide_index=True,
                )
            if bids_r2:
                st.subheader("Round 2 (Re-bid)")
                st.dataframe(
                    pd.DataFrame(bids_r2)[["carrier_name", "rate", "transit_days"]],
                    use_container_width=True,
                    hide_index=True,
                )

        with st.expander("🗂 Audit Trail"):
            for entry in quote.get("audit_log", []):
                ts = entry.get("timestamp", "")[:16].replace("T", " ")
                etype = entry.get("type", "")
                if etype == "note":
                    st.text(f"  [{ts}] 🤖 NOTE: {entry['data'].get('agent_note','')[:100]}")
                elif etype == "manual_override":
                    d = entry.get("data", {})
                    st.text(f"  [{ts}] ✏️  OVERRIDE: {d.get('previous_carrier')} ${d.get('previous_rate')} → {d.get('new_carrier')} ${d.get('new_rate')}")
                elif etype == "customer_email_sent":
                    st.text(f"  [{ts}] 📨 EMAIL SENT TO CUSTOMER")
                elif entry.get("from") is None:
                    st.text(f"  [{ts}] CREATED → {entry.get('to')}")
                else:
                    st.text(f"  [{ts}] {entry.get('from')} → {entry.get('to')}")

        with st.expander("🔍 Raw JSON"):
            st.json(fq)

        # ── Customer Email Draft ────────────────────────────────────────────
        _email_key = f"draft_email_{quote['quote_id']}"
        if _email_key not in st.session_state:
            st.session_state[_email_key] = generate_customer_email(quote)

        with st.expander("📨 Customer Email Draft", expanded=False):
            st.caption(
                "Review and edit the quote email below, then mark it as sent. "
                "No external connection needed — this logs the action in the audit trail."
            )
            draft_text = st.text_area(
                "Email draft",
                value=st.session_state[_email_key],
                height=340,
                key=f"textarea_{_email_key}",
                label_visibility="collapsed",
            )
            already_sent = bool(quote.get("customer_email_sent"))
            if already_sent:
                sent_at = quote["customer_email_sent"].get("sent_at", "")[:16].replace("T", " ")
                st.success(f"✅ Email marked as sent at {sent_at} UTC")
                if st.button("📨 Re-send (log again)", key=f"resend_{quote['quote_id']}"):
                    updated = load_quote(quote["quote_id"]) or quote
                    log_email_sent(updated, draft_text)
                    st.session_state["last_quote"] = updated
                    st.rerun()
            else:
                if st.button("📨 Mark as Sent to Customer", type="primary", key=f"send_{quote['quote_id']}"):
                    updated = load_quote(quote["quote_id"]) or quote
                    log_email_sent(updated, draft_text)
                    st.session_state["last_quote"] = updated
                    st.success("Logged as sent — check the Audit Trail.")
                    st.rerun()

        # ── Execution Trail (collapsed by default) ──────────────────────────
        events = st.session_state.get("quote_events", [])
        if events:
            _EVENT_ICON_TRAIL = {
                "state_transition":   "🔄",
                "bidding_window_open": "📤",
                "carrier_bid":        "✅",
                "carrier_timeout":    "⌛",
                "round_complete":     "📊",
                "tool_call":          "🔧",
                "tool_result":        "💡",
                "agent_reasoning":    "🤖",
                "quote_complete":     "🎉",
            }
            with st.expander(f"⏱ Execution Trail ({len(events)} events)", expanded=False):
                lines = []
                for ev in events:
                    icon = _EVENT_ICON_TRAIL.get(ev["type"], "•")
                    secs = ev["elapsed_ms"] / 1000
                    lines.append(f"`{secs:>5.1f}s` {icon} {ev['message']}")
                st.markdown("\n\n".join(lines))

    elif quote:
        st.warning("Agent ran but did not produce a final quote. Check provider/model settings.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Pipeline
# ═══════════════════════════════════════════════════════════════════════════════
with tab_pipeline:
    st.header("Freight Pipeline")

    # Filters
    fcol1, fcol2, fcol3 = st.columns([2, 2, 1])
    with fcol1:
        filter_status = st.selectbox(
            "Filter by Status",
            options=["All"] + STAGES,
            index=0,
            key="pipeline_status_filter",
        )
    with fcol2:
        filter_customer = st.selectbox(
            "Filter by Customer",
            options=["All"] + list(CUSTOMERS.keys()),
            index=0,
            key="pipeline_customer_filter",
        )
    with fcol3:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    status_arg = None if filter_status == "All" else filter_status
    customer_arg = None if filter_customer == "All" else filter_customer
    rows = list_quotes(status=status_arg, customer_id=customer_arg)

    if not rows:
        st.info("No quotes found. Run the agent from the New Quote tab to get started.")
    else:
        st.caption(f"{len(rows)} quote(s) found")

        for row in rows:
            qid = row["quote_id"]
            status = row["status"]
            cid = row["customer_id"]
            cname = CUSTOMERS.get(cid, {}).get("name", cid)
            route = f"{row.get('origin','?')} → {row.get('destination','?')}"
            sell = _fmt_usd(row.get("sell_rate"))
            profit = _fmt_usd(row.get("gross_profit"))
            carrier = row.get("winning_carrier_name") or "—"
            created = (row.get("created_at") or "")[:16].replace("T", " ")

            src_badge = "📧 EMAIL" if row.get("source") == "email" else "🖥 UI"
            with st.expander(
                f"**{qid}** &nbsp; {STATUS_EMOJI.get(status,'')} {status} &nbsp;|&nbsp; "
                f"{cname} &nbsp;|&nbsp; {route} &nbsp;|&nbsp; Sell: {sell} &nbsp;|&nbsp; {src_badge}",
                expanded=False,
            ):
                dc1, dc2, dc3, dc4 = st.columns(4)
                dc1.metric("Customer", f"{cname} ({cid})")
                dc2.metric("Carrier", carrier)
                dc3.metric("Sell Rate", sell)
                dc4.metric("Gross Profit", profit)

                sc1, sc2, sc3 = st.columns(3)
                sc1.metric("Weight", f"{row.get('weight_lbs','?')} lbs")
                sc2.metric("Markup", f"{row.get('markup_pct','?')}%")
                sc3.metric("Created", created)

                # Action buttons for quotes awaiting approval
                if status == "AWAITING_APPROVAL":
                    st.divider()
                    st.write("**Take Action**")
                    ac1, ac2 = st.columns(2)
                    if ac1.button("✅ Approve", key=f"approve_{qid}", type="primary"):
                        try:
                            update_quote_status(qid, "APPROVED", {"approved_by": "dashboard"})
                            st.success(f"{qid} → APPROVED")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

                    with ac2.popover("❌ Reject"):
                        lost_reason = st.text_area(
                            "Reason for rejection",
                            key=f"lost_reason_{qid}",
                            placeholder="e.g. Rate too high, customer went with competitor",
                        )
                        if st.button("Confirm Reject", key=f"confirm_reject_{qid}", type="secondary"):
                            try:
                                update_quote_status(
                                    qid, "LOST",
                                    {"lost_reason": lost_reason, "rejected_by": "dashboard"}
                                )
                                st.success(f"{qid} → LOST")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")

                # Advance IN_TRANSIT quotes to COMPLETED
                elif status == "IN_TRANSIT":
                    st.divider()
                    if st.button("🎉 Mark Completed", key=f"complete_{qid}", type="primary"):
                        try:
                            update_quote_status(qid, "COMPLETED", {"completed_by": "dashboard"})
                            st.success(f"{qid} → COMPLETED")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

                # Advance APPROVED quotes to IN_TRANSIT
                elif status == "APPROVED":
                    st.divider()
                    if st.button("🚛 Mark In Transit", key=f"transit_{qid}", type="primary"):
                        try:
                            update_quote_status(qid, "IN_TRANSIT", {"updated_by": "dashboard"})
                            st.success(f"{qid} → IN_TRANSIT")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

                # ── Override carrier bid ───────────────────────────────────
                full_for_override = load_quote(qid)
                if full_for_override and full_for_override.get("winning_bid") and status not in ("COMPLETED", "LOST"):
                    with st.expander("✏️ Override Carrier Bid"):
                        st.caption(
                            "Manually replace the winning carrier with a negotiated or spot rate. "
                            "Markup is recalculated automatically."
                        )
                        oc1, oc2, oc3 = st.columns(3)
                        ov_carrier = oc1.text_input(
                            "Carrier Name",
                            placeholder="e.g. XPO Logistics",
                            key=f"ov_carrier_{qid}",
                        )
                        ov_rate = oc2.number_input(
                            "Carrier Rate ($)",
                            min_value=0.01,
                            value=float(row.get("carrier_cost") or 100.0),
                            step=10.0,
                            key=f"ov_rate_{qid}",
                        )
                        ov_days = oc3.number_input(
                            "Transit Days",
                            min_value=1,
                            max_value=30,
                            value=int(full_for_override["winning_bid"].get("transit_days") or 3),
                            key=f"ov_days_{qid}",
                        )
                        ov_reason = st.text_input(
                            "Reason (optional)",
                            placeholder="Negotiated spot rate",
                            key=f"ov_reason_{qid}",
                        )
                        # Live preview
                        markup_pct = float(row.get("markup_pct") or 10.0)
                        preview_sell = round(ov_rate * (1 + markup_pct / 100), 2)
                        preview_profit = round(preview_sell - ov_rate, 2)
                        pc1, pc2, pc3 = st.columns(3)
                        pc1.metric("Preview Sell Rate", _fmt_usd(preview_sell),
                                   delta=f"{preview_sell - (row.get('sell_rate') or 0):+.2f} vs current")
                        pc2.metric("Preview Gross Profit", _fmt_usd(preview_profit))
                        pc3.metric("Markup %", f"{markup_pct}%")

                        if st.button("Apply Override", key=f"apply_ov_{qid}", type="primary"):
                            if not ov_carrier.strip():
                                st.error("Carrier name is required.")
                            else:
                                try:
                                    apply_bid_override(
                                        quote=full_for_override,
                                        override_carrier_name=ov_carrier.strip(),
                                        override_rate=ov_rate,
                                        override_transit_days=int(ov_days),
                                        override_reason=ov_reason,
                                    )
                                    st.success(f"Override applied — new sell rate {_fmt_usd(preview_sell)}")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Override failed: {e}")

                # ── Customer email draft ───────────────────────────────────
                _pl_full = load_quote(qid)
                if _pl_full and _pl_full.get("final_quote"):
                    _pl_email_key = f"draft_email_{qid}"
                    if _pl_email_key not in st.session_state:
                        st.session_state[_pl_email_key] = generate_customer_email(_pl_full)

                    with st.expander("📨 Customer Email Draft", expanded=False):
                        st.caption(
                            "Edit the quote email and mark it as sent. "
                            "This logs the action in the audit trail — no external inbox needed."
                        )
                        pl_draft = st.text_area(
                            "Email draft",
                            value=st.session_state[_pl_email_key],
                            height=320,
                            key=f"pl_textarea_{qid}",
                            label_visibility="collapsed",
                        )
                        _already_sent = bool(_pl_full.get("customer_email_sent"))
                        if _already_sent:
                            _sent_at = _pl_full["customer_email_sent"].get("sent_at", "")[:16].replace("T", " ")
                            st.success(f"✅ Email marked as sent at {_sent_at} UTC")
                            if st.button("📨 Re-send (log again)", key=f"pl_resend_{qid}"):
                                log_email_sent(_pl_full, pl_draft)
                                st.rerun()
                        else:
                            if st.button("📨 Mark as Sent to Customer", type="primary", key=f"pl_send_{qid}"):
                                log_email_sent(_pl_full, pl_draft)
                                st.success("Logged as sent.")
                                st.rerun()

                # Full detail expander
                with st.expander("🔍 Full Quote Detail + Audit Log"):
                    full = load_quote(qid)
                    if full:
                        if full.get("carrier_bids_round1"):
                            st.subheader("Round 1 Bids")
                            st.dataframe(
                                pd.DataFrame(full["carrier_bids_round1"])[
                                    ["carrier_name", "rate", "transit_days"]
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )
                        if full.get("carrier_bids_round2"):
                            st.subheader("Round 2 Bids")
                            st.dataframe(
                                pd.DataFrame(full["carrier_bids_round2"])[
                                    ["carrier_name", "rate", "transit_days"]
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )
                        st.subheader("Audit Log")
                        for entry in full.get("audit_log", []):
                            ts = entry.get("timestamp", "")[:16].replace("T", " ")
                            etype = entry.get("type", "")
                            if etype == "note":
                                st.text(f"  [{ts}] 🤖 NOTE: {entry['data'].get('agent_note','')[:120]}")
                            elif etype == "manual_override":
                                d = entry.get("data", {})
                                st.text(f"  [{ts}] ✏️  OVERRIDE: {d.get('previous_carrier')} ${d.get('previous_rate')} → {d.get('new_carrier')} ${d.get('new_rate')}")
                            elif etype == "customer_email_sent":
                                st.text(f"  [{ts}] 📨 EMAIL SENT TO CUSTOMER")
                            elif entry.get("from") is None:
                                st.text(f"  [{ts}] CREATED → {entry.get('to')}")
                            else:
                                st.text(f"  [{ts}] {entry.get('from')} → {entry.get('to')}")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Email Inbox
# ═══════════════════════════════════════════════════════════════════════════════
with tab_email:
    st.header("Email Inbox")
    st.caption(
        "Simulate inbound freight requests sent to **freight@wisequote.com**. "
        "Paste or load a demo email, preview the parsed fields, queue it, then process it through the agent."
    )

    # ── Section A: Compose / Paste ─────────────────────────────────────────────
    st.subheader("Compose / Paste Email")

    demo_options = ["— blank —"] + [t["label"] for t in DEMO_EMAIL_TEMPLATES]
    prev_choice = st.session_state.get("_email_demo_prev", "— blank —")
    demo_choice = st.selectbox("Load demo template", options=demo_options, key="email_demo_choice")

    # When the user picks a new template, overwrite the text fields in session state
    if demo_choice != "— blank —" and demo_choice != prev_choice:
        _tmpl = next(t for t in DEMO_EMAIL_TEMPLATES if t["label"] == demo_choice)
        st.session_state["email_subject_input"] = _tmpl["subject"]
        st.session_state["email_sender_input"]  = _tmpl["sender"]
        st.session_state["email_body_input"]    = _tmpl["body"]
    st.session_state["_email_demo_prev"] = demo_choice

    ec1, ec2 = st.columns(2)
    email_subject = ec1.text_input("Subject", key="email_subject_input")
    email_sender  = ec2.text_input("Sender",  placeholder="logistics@customer.com", key="email_sender_input")
    email_body = st.text_area("Email body", height=200, key="email_body_input")

    btn_parse, btn_queue = st.columns(2)

    # Parse preview (no DB write)
    if btn_parse.button("🔍 Parse Preview", use_container_width=True):
        if not email_body.strip():
            st.warning("Enter an email body first.")
        else:
            parsed = parse_freight_email(email_body, subject=email_subject, sender=email_sender)
            is_valid, missing = validate_parsed_request(parsed)
            conf = parsed["_parse_confidence"]
            conf_color = "green" if conf >= 0.8 else ("orange" if conf >= 0.4 else "red")
            st.markdown(f"**Parse confidence:** :{conf_color}[{conf*100:.0f}%]")
            if not is_valid:
                st.warning(f"Missing required fields: **{', '.join(missing)}** — correct before queuing.")

            rows_preview = [
                {"Field": "Origin",       "Value": parsed["origin"]       or "—", "Status": "✅" if parsed["origin"]       else "⚠️"},
                {"Field": "Destination",  "Value": parsed["destination"]  or "—", "Status": "✅" if parsed["destination"]  else "⚠️"},
                {"Field": "Weight (lbs)", "Value": str(parsed["weight_lbs"] or "—"), "Status": "✅" if parsed["weight_lbs"] else "⚠️ (will default to 1000)"},
                {"Field": "Cargo Type",   "Value": parsed["cargo_type"]   or "—", "Status": "✅" if parsed["cargo_type"]   else "⚠️ (will default to General Merchandise)"},
                {"Field": "Pickup Date",  "Value": parsed["pickup_date"]  or "—", "Status": "✅" if parsed["pickup_date"]  else "⚠️ (will default to today)"},
                {"Field": "Customer ID",  "Value": parsed["customer_id"]  or "—", "Status": "✅" if parsed["customer_id"]  else "❌ required"},
                {"Field": "Hazmat",       "Value": str(parsed["hazmat"]),          "Status": "✅"},
            ]
            st.dataframe(pd.DataFrame(rows_preview), use_container_width=True, hide_index=True)
            st.session_state["email_parsed_preview"] = parsed

    # Queue for processing
    if btn_queue.button("📥 Queue for Processing", type="primary", use_container_width=True):
        if not email_body.strip():
            st.warning("Enter an email body first.")
        else:
            parsed = parse_freight_email(email_body, subject=email_subject, sender=email_sender)
            is_valid, missing = validate_parsed_request(parsed)
            if not is_valid:
                st.error(f"Cannot queue — missing required fields: {', '.join(missing)}. Fill them in the email body.")
            else:
                msg_id = queue_inbox_message(
                    body=email_body,
                    subject=email_subject,
                    sender=email_sender,
                )
                st.success(f"Message #{msg_id} queued. Scroll down to the Inbox Queue to process it.")
                st.rerun()

    st.divider()

    # ── Section B: Inbox Queue ─────────────────────────────────────────────────
    st.subheader("Inbox Queue")

    icol1, icol2 = st.columns([4, 1])
    inbox_filter = icol1.selectbox(
        "Filter",
        options=["All", "PENDING", "PROCESSED", "FAILED"],
        key="inbox_filter",
    )
    if icol2.button("🔄 Refresh", key="inbox_refresh", use_container_width=True):
        st.rerun()

    inbox_msgs = list_inbox_messages(status=None if inbox_filter == "All" else inbox_filter)

    if not inbox_msgs:
        st.info("No messages yet. Paste an email above and click Queue.")
    else:
        st.caption(f"{len(inbox_msgs)} message(s)")
        for msg in inbox_msgs:
            mid     = msg["id"]
            mstatus = msg["status"]
            msubj   = msg["subject"] or "(no subject)"
            msender = msg["sender"] or "(unknown sender)"
            mqid    = msg["quote_id"] or "—"
            mtime   = (msg["received_at"] or "")[:16].replace("T", " ")
            badge   = {"PENDING": "🟡", "PROCESSED": "✅", "FAILED": "❌"}.get(mstatus, "•")

            with st.expander(f"{badge} **#{mid}** — {msubj} | {msender} | {mtime} | Quote: {mqid}", expanded=False):
                st.text_area("Body", value=msg["body"], height=120, disabled=True, key=f"inbox_body_{mid}")

                if msg.get("error_msg"):
                    st.error(f"Error: {msg['error_msg']}")

                if mstatus == "PENDING":
                    if st.button("▶ Process Now", key=f"inbox_proc_{mid}", type="primary"):
                        parsed = parse_freight_email(msg["body"], subject=msg["subject"] or "", sender=msg["sender"] or "")
                        is_valid, missing = validate_parsed_request(parsed)
                        if not is_valid:
                            mark_inbox_processed(mid, error=f"Missing required fields: {', '.join(missing)}")
                            st.error(f"Failed: missing {', '.join(missing)}")
                            st.rerun()
                        else:
                            try:
                                freight_request = normalize_freight_request(parsed)
                            except ValueError as ve:
                                mark_inbox_processed(mid, error=str(ve))
                                st.error(str(ve))
                                st.rerun()
                                st.stop()

                            _EVENT_ICON_EMAIL = {
                                "state_transition": "🔄", "bidding_window_open": "📤",
                                "carrier_bid": "✅", "carrier_timeout": "⌛",
                                "round_complete": "📊", "tool_call": "🔧",
                                "tool_result": "💡", "agent_reasoning": "🤖",
                                "quote_complete": "🎉",
                            }
                            with st.status(f"Processing message #{mid}…", expanded=True) as _estatus:
                                efeed = st.empty()
                                _eevents = []

                                def _email_on_event(event, _eevents=_eevents, _efeed=efeed):
                                    _eevents.append(event)
                                    lines = []
                                    for ev in _eevents:
                                        icon = _EVENT_ICON_EMAIL.get(ev.type, "•")
                                        secs = ev.elapsed_ms / 1000
                                        lines.append(f"`{secs:>5.1f}s` {icon} {ev.message}")
                                    _efeed.markdown("\n\n".join(lines))

                                try:
                                    _saved = _load_model_settings()
                                    _pname = _saved["provider"]
                                    if _pname == "ollama":
                                        _model = _saved["ollama_model"]
                                    elif _pname == "openai":
                                        _model = _saved["openai_model"]
                                        if _saved.get("openai_api_key"):
                                            os.environ.setdefault("OPENAI_API_KEY", _saved["openai_api_key"])
                                    else:
                                        _model = _saved["anthropic_model"]
                                        if _saved.get("anthropic_api_key"):
                                            os.environ.setdefault("ANTHROPIC_API_KEY", _saved["anthropic_api_key"])
                                    _provider = get_provider(_pname, model=_model)
                                    result_quote = run_agent(
                                        freight_request,
                                        provider=_provider,
                                        verbose=False,
                                        on_event=_email_on_event,
                                    )
                                    mark_inbox_processed(mid, quote_id=result_quote["quote_id"], parse_result=parsed)
                                    _estatus.update(label=f"Done ✅ → {result_quote['quote_id']}", state="complete", expanded=False)
                                    st.success(f"Quote created: **{result_quote['quote_id']}** — check the Pipeline tab.")
                                    st.rerun()
                                except Exception as e:
                                    mark_inbox_processed(mid, error=str(e))
                                    _estatus.update(label="Failed ❌", state="error", expanded=False)
                                    st.error(f"Agent error: {e}")
                                    st.rerun()

                elif mstatus == "FAILED":
                    if st.button("↩ Retry", key=f"inbox_retry_{mid}"):
                        with sqlite3.connect(DEFAULT_DB_PATH) as _conn:
                            _conn.execute(
                                "UPDATE inbox_messages SET status='PENDING', error_msg=NULL, processed_at=NULL WHERE id=?",
                                (mid,),
                            )
                        st.rerun()

                elif mstatus == "PROCESSED" and mqid != "—":
                    st.caption(f"Quote generated: **{mqid}** — view it in the Pipeline tab.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Analytics
# ═══════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.header("Analytics Dashboard")

    if st.button("🔄 Refresh Analytics"):
        st.rerun()

    stats = compute_analytics()

    if stats["total_quotes"] == 0:
        st.info("No quote data yet. Run some quotes from the New Quote tab first.")
    else:
        # ── KPI Cards ──────────────────────────────────────────────────────────
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Total Quotes", stats["total_quotes"])
        k2.metric("Win Rate", f"{stats['win_rate_pct']}%",
                  delta=f"{stats['approved_count']} won / {stats['lost_count']} lost")
        k3.metric("Avg Gross Profit", _fmt_usd(stats["avg_gross_profit"]))
        k4.metric("Avg Margin %", f"{stats['avg_gross_margin_pct']}%")
        k5.metric("Total Profit", _fmt_usd(stats["total_gross_profit"]))

        st.divider()

        row1_col1, row1_col2 = st.columns(2)

        # ── Pipeline Funnel ────────────────────────────────────────────────────
        with row1_col1:
            st.subheader("Pipeline Stage Distribution")
            by_status = stats["by_status"]
            stage_labels = [s for s in STAGES if by_status.get(s, 0) > 0]
            stage_values = [by_status[s] for s in stage_labels]

            if stage_labels:
                fig_funnel = go.Figure(go.Funnel(
                    y=stage_labels,
                    x=stage_values,
                    textinfo="value+percent initial",
                    marker={"color": [STATUS_COLORS.get(s, "#6c757d") for s in stage_labels]},
                ))
                fig_funnel.update_layout(margin=dict(l=0, r=0, t=20, b=0), height=350)
                st.plotly_chart(fig_funnel, use_container_width=True)
            else:
                st.info("No stage data.")

        # ── Win Rate by Carrier ────────────────────────────────────────────────
        with row1_col2:
            st.subheader("Carrier Win Rate (%)")
            by_carrier = stats["by_carrier"]
            if by_carrier:
                carriers_sorted = sorted(by_carrier.items(), key=lambda x: -x[1]["win_rate_pct"])
                c_names = [k for k, _ in carriers_sorted]
                c_rates = [v["win_rate_pct"] for _, v in carriers_sorted]
                fig_carrier = px.bar(
                    x=c_rates, y=c_names,
                    orientation="h",
                    labels={"x": "Win Rate (%)", "y": ""},
                    color=c_rates,
                    color_continuous_scale="Blues",
                )
                fig_carrier.update_layout(
                    margin=dict(l=0, r=0, t=20, b=0),
                    height=350,
                    showlegend=False,
                    coloraxis_showscale=False,
                )
                st.plotly_chart(fig_carrier, use_container_width=True)
            else:
                st.info("No carrier data.")

        row2_col1, row2_col2 = st.columns(2)

        # ── Avg Profit by Customer ────────────────────────────────────────────
        with row2_col1:
            st.subheader("Avg Gross Profit by Customer")
            by_cust = stats["by_customer"]
            if by_cust:
                cust_sorted = sorted(by_cust.items(), key=lambda x: -x[1]["avg_gross_profit"])
                cust_names = [
                    f"{k} ({CUSTOMERS.get(k, {}).get('name', k)})"
                    for k, _ in cust_sorted
                ]
                cust_profits = [v["avg_gross_profit"] for _, v in cust_sorted]
                fig_cust = px.bar(
                    x=cust_names, y=cust_profits,
                    labels={"x": "", "y": "Avg Gross Profit ($)"},
                    color=cust_profits,
                    color_continuous_scale="Greens",
                )
                fig_cust.update_layout(
                    margin=dict(l=0, r=0, t=20, b=0),
                    height=300,
                    coloraxis_showscale=False,
                )
                st.plotly_chart(fig_cust, use_container_width=True)
            else:
                st.info("No customer data.")

        # ── Carrier Competitiveness Table ─────────────────────────────────────
        with row2_col2:
            st.subheader("Carrier Competitiveness")
            if by_carrier:
                comp_rows = []
                for name, d in sorted(by_carrier.items(), key=lambda x: -x[1]["competitiveness_score"]):
                    comp_rows.append({
                        "Carrier": name,
                        "Bids": d["bids"],
                        "Wins": d["wins"],
                        "Win Rate": f"{d['win_rate_pct']}%",
                        "Avg Profit": _fmt_usd(d["avg_gross_profit"]),
                        "Score": f"{d['competitiveness_score']:.3f}",
                    })
                st.dataframe(
                    pd.DataFrame(comp_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No carrier data.")

        st.divider()

        # ── Additional KPIs ───────────────────────────────────────────────────
        st.subheader("Operational Metrics")
        m1, m2, m3, m4 = st.columns(4)
        turnaround = stats["avg_turnaround_secs"]
        if turnaround > 0:
            mins = int(turnaround // 60)
            secs = int(turnaround % 60)
            turnaround_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
        else:
            turnaround_str = "—"
        m1.metric("Avg Turnaround", turnaround_str)
        m2.metric("Quote → Approval %", f"{stats['quote_to_approval_pct']}%")
        m3.metric("Avg Sell Rate", _fmt_usd(stats["avg_sell_rate"]))
        m4.metric("Avg Carrier Cost", _fmt_usd(stats["avg_carrier_cost"]))

        # ── Recent Quotes Table ───────────────────────────────────────────────
        st.divider()
        st.subheader("Recent Quotes")
        recent = stats.get("recent_quotes", [])
        if recent:
            df_recent = pd.DataFrame(recent)
            df_recent["sell_rate"] = df_recent["sell_rate"].apply(
                lambda x: f"${x:,.2f}" if x else "—"
            )
            df_recent["gross_profit"] = df_recent["gross_profit"].apply(
                lambda x: f"${x:,.2f}" if x else "—"
            )
            df_recent["markup_pct"] = df_recent["markup_pct"].apply(
                lambda x: f"{x}%" if x else "—"
            )
            df_recent["created_at"] = df_recent["created_at"].str[:16].str.replace("T", " ")
            st.dataframe(
                df_recent[[
                    "quote_id", "customer_id", "status", "origin", "destination",
                    "carrier_name", "sell_rate", "gross_profit", "markup_pct", "created_at"
                ]].rename(columns={
                    "quote_id": "Quote ID",
                    "customer_id": "Customer",
                    "status": "Status",
                    "origin": "Origin",
                    "destination": "Destination",
                    "carrier_name": "Carrier",
                    "sell_rate": "Sell Rate",
                    "gross_profit": "Gross Profit",
                    "markup_pct": "Markup",
                    "created_at": "Created",
                }),
                use_container_width=True,
                hide_index=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — Customers
# ═══════════════════════════════════════════════════════════════════════════════
with tab_customers:
    st.header("Customer Management")

    CARRIER_OPTIONS = {c["id"]: c["name"] for c in CARRIERS}

    # ── Existing customers table ───────────────────────────────────────────────
    st.subheader("Existing Customers")
    all_customers = list_customers()
    if all_customers:
        rows_display = []
        for c in all_customers:
            prefs = c["preferred_carriers"]
            if prefs:
                pref_str = ", ".join(CARRIER_OPTIONS.get(cid, cid) for cid in prefs)
            else:
                pref_str = "Any carrier"
            rows_display.append({
                "ID":                c["customer_id"],
                "Name":              c["name"],
                "Markup %":          f"{c['markup_pct']}%",
                "Carrier Preference": pref_str,
                "Created":           (c.get("created_at") or "")[:10],
            })
        st.dataframe(
            pd.DataFrame(rows_display),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No customers found.")

    st.divider()

    # ── Add / Edit customer form ───────────────────────────────────────────────
    st.subheader("Add / Edit Customer")
    st.caption("Customer ID must be unique. Submitting an existing ID will update that customer's settings.")

    with st.form("customer_form"):
        cf1, cf2 = st.columns(2)
        with cf1:
            new_cid = st.text_input(
                "Customer ID",
                placeholder="CUST-E",
                help="Unique identifier used in freight requests (e.g. CUST-E)",
            )
            new_name = st.text_input("Company Name", placeholder="Epsilon Trading Co")
        with cf2:
            new_markup = st.number_input(
                "Markup %",
                min_value=0.0,
                max_value=200.0,
                value=10.0,
                step=0.5,
                help="Percentage added on top of the winning carrier cost",
            )
            carrier_pref_selection = st.multiselect(
                "Carrier Preference",
                options=list(CARRIER_OPTIONS.keys()),
                format_func=lambda k: CARRIER_OPTIONS[k],
                help="Leave empty to allow any carrier. Select one or more to restrict this customer to specific carriers.",
            )

        save_submitted = st.form_submit_button("💾 Save Customer", type="primary", use_container_width=True)

    if save_submitted:
        if not new_cid or not new_name:
            st.error("Customer ID and Company Name are required.")
        elif not new_cid.replace("-", "").replace("_", "").isalnum():
            st.error("Customer ID may only contain letters, numbers, hyphens, and underscores.")
        else:
            customer_record = {
                "customer_id":        new_cid.strip().upper(),
                "name":               new_name.strip(),
                "markup_pct":         new_markup,
                "preferred_carriers": carrier_pref_selection if carrier_pref_selection else None,
            }
            try:
                save_customer(customer_record)
                pref_display = (
                    ", ".join(CARRIER_OPTIONS[c] for c in carrier_pref_selection)
                    if carrier_pref_selection else "any carrier"
                )
                st.success(
                    f"✅ **{new_name}** ({new_cid.upper()}) saved — "
                    f"{new_markup}% markup, prefers {pref_display}"
                )
                st.rerun()
            except Exception as e:
                st.error(f"Error saving customer: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 6 — Model Settings
# ═══════════════════════════════════════════════════════════════════════════════
with tab_models:
    st.header("Model Settings")
    st.caption("Configure the default LLM provider and model used by the freight agent.")

    ms = _load_model_settings()

    # ── Provider selector ──────────────────────────────────────────────────────
    saved_provider = ms["provider"]
    provider_tab_index = PROVIDER_OPTIONS.index(saved_provider) if saved_provider in PROVIDER_OPTIONS else 0
    pt_ollama, pt_openai, pt_claude = st.tabs(["🦙 Ollama (Local)", "🤖 OpenAI", "✳️ Anthropic Claude"])

    # ── Ollama ─────────────────────────────────────────────────────────────────
    with pt_ollama:
        st.subheader("Ollama — Local Models")

        col_status, col_refresh = st.columns([4, 1])
        ollama_models = _fetch_ollama_models()

        if ollama_models:
            col_status.success(f"Ollama is running — {len(ollama_models)} model(s) available")
        else:
            col_status.warning("Ollama is not running or unreachable at http://localhost:11434")

        if col_refresh.button("🔄 Refresh", key="refresh_ollama"):
            st.rerun()

        if ollama_models:
            saved_ollama_model = ms["ollama_model"]
            default_idx = ollama_models.index(saved_ollama_model) if saved_ollama_model in ollama_models else 0

            selected_ollama_model = st.radio(
                "Select model",
                options=ollama_models,
                index=default_idx,
                key="ollama_model_radio",
            )

            if st.button("💾 Save as Default (Ollama)", type="primary", key="save_ollama"):
                set_setting("provider", "ollama")
                set_setting("ollama_model", selected_ollama_model)
                st.success(f"Default set to **Ollama / {selected_ollama_model}**")
                st.rerun()
        else:
            st.info("Start Ollama (`ollama serve`) and click Refresh to see available models.")

        if saved_provider == "ollama":
            st.info(f"**Currently active:** Ollama / `{ms['ollama_model']}`")

    # ── OpenAI ─────────────────────────────────────────────────────────────────
    with pt_openai:
        st.subheader("OpenAI")

        with st.form("openai_settings_form"):
            openai_api_key = st.text_input(
                "API Key",
                value=ms["openai_api_key"],
                type="password",
                placeholder="sk-...",
                help="Your OpenAI API key. Stored locally in the SQLite database.",
            )
            openai_model = st.text_input(
                "Model",
                value=ms["openai_model"],
                placeholder="gpt-4o-mini",
                help="Any OpenAI chat model ID, e.g. gpt-4o, gpt-4o-mini, gpt-4-turbo",
            )
            save_openai = st.form_submit_button("💾 Save & Set as Default", type="primary", use_container_width=True)

        if save_openai:
            if not openai_api_key.strip():
                st.error("API key is required.")
            elif not openai_model.strip():
                st.error("Model name is required.")
            else:
                set_setting("provider", "openai")
                set_setting("openai_model", openai_model.strip())
                set_setting("openai_api_key", openai_api_key.strip())
                os.environ["OPENAI_API_KEY"] = openai_api_key.strip()
                st.success(f"Default set to **OpenAI / {openai_model.strip()}**")
                st.rerun()

        if saved_provider == "openai":
            st.info(f"**Currently active:** OpenAI / `{ms['openai_model']}`")

        st.caption(
            "API key is stored in the local SQLite database (`freight_quotes.db`). "
            "Do not use on shared machines."
        )

    # ── Anthropic Claude ───────────────────────────────────────────────────────
    with pt_claude:
        st.subheader("Anthropic Claude")

        CLAUDE_MODELS = [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5-20251001",
        ]

        with st.form("anthropic_settings_form"):
            anthropic_api_key = st.text_input(
                "API Key",
                value=ms["anthropic_api_key"],
                type="password",
                placeholder="sk-ant-...",
                help="Your Anthropic API key. Stored locally in the SQLite database.",
            )
            # Let user pick from known models or type a custom one
            use_custom_claude = st.checkbox("Enter custom model ID", value=ms["anthropic_model"] not in CLAUDE_MODELS)
            if use_custom_claude:
                anthropic_model = st.text_input(
                    "Model ID",
                    value=ms["anthropic_model"],
                    placeholder="claude-sonnet-4-6",
                )
            else:
                saved_claude_idx = CLAUDE_MODELS.index(ms["anthropic_model"]) if ms["anthropic_model"] in CLAUDE_MODELS else 0
                anthropic_model = st.selectbox(
                    "Model",
                    options=CLAUDE_MODELS,
                    index=saved_claude_idx,
                )
            save_anthropic = st.form_submit_button("💾 Save & Set as Default", type="primary", use_container_width=True)

        if save_anthropic:
            if not anthropic_api_key.strip():
                st.error("API key is required.")
            elif not anthropic_model.strip():
                st.error("Model name is required.")
            else:
                set_setting("provider", "claude")
                set_setting("anthropic_model", anthropic_model.strip())
                set_setting("anthropic_api_key", anthropic_api_key.strip())
                os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key.strip()
                st.success(f"Default set to **Claude / {anthropic_model.strip()}**")
                st.rerun()

        if saved_provider == "claude":
            st.info(f"**Currently active:** Claude / `{ms['anthropic_model']}`")

        st.caption(
            "API key is stored in the local SQLite database (`freight_quotes.db`). "
            "Do not use on shared machines."
        )

    # ── Bidding Settings ───────────────────────────────────────────────────────
    st.divider()
    st.subheader("Bidding Window Settings")
    st.caption(
        "Controls how long each round waits for carrier responses. "
        "In production this maps to real clock time (e.g. 3000ms sim ≈ 2hr real). "
        "Reduce to exclude slow carriers; increase to collect more bids."
    )

    with st.form("bidding_settings_form"):
        bs1, bs2 = st.columns(2)
        with bs1:
            round1_window = st.number_input(
                "Round 1 — Carrier Response Window (ms)",
                min_value=100,
                max_value=30000,
                value=int(get_setting("bidding_window_ms", "3000")),
                step=100,
                help="Carriers that respond after this deadline are excluded from Round 1.",
            )
        with bs2:
            round2_window = st.number_input(
                "Round 2 — Re-bid Window (ms)",
                min_value=100,
                max_value=30000,
                value=int(get_setting("rebid_window_ms", "1500")),
                step=100,
                help="Shorter window for the re-bid round. Only Round 1 non-winners compete.",
            )
        save_bidding = st.form_submit_button("💾 Save Bidding Settings", type="primary", use_container_width=True)

    if save_bidding:
        set_setting("bidding_window_ms", str(round1_window))
        set_setting("rebid_window_ms",   str(round2_window))
        st.success(f"Bidding windows saved — Round 1: {round1_window}ms, Round 2: {round2_window}ms")
        st.rerun()
