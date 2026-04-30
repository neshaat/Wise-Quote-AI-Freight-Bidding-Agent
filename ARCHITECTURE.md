# Wise Quote Freight Bidding Agent — Architecture

## System Overview

A freight rate automation system built around an LLM agent loop. Inbound requests arrive via a manual form or an email inbox simulator. The agent orchestrates two-round competitive carrier bidding, applies customer markup, and produces a structured quote ready to send. A Streamlit dashboard covers the full ops workflow: intake, bidding, quoting, approval pipeline, email drafting, analytics, and customer management.

---

## Agent Loop

```
Inbound Request (UI form or email inbox)
      │
      ▼
 create_quote()          ← State: INTAKE
      │
      ▼
LLM Provider ──────────── tool_use loop ──────────────────┐
(Ollama / OpenAI / Claude)                                 │
      │                                                    │
      │  1. collect_and_benchmark_rates                    │
      │       ├─ Round 1: eligible carriers bid            │
      │       │  (filtered by customer preference)         │
      │       │  carriers assigned simulated response_time │
      │       │  those exceeding bidding_window_ms skipped │
      │       ├─ Lowest rate identified                    │
      │       └─ Round 2 re-bid: non-winners told the      │
      │          floor rate, 40% chance to counter-bid     │
      │          within rebid_window_ms                    │
      │                                                    │
      │  2. apply_customer_markup                          │
      │       └─ sell_rate = cost × (1 + markup%)          │
      │                                                    │
      │  3. generate_final_quote                           │
      │       └─ structured quote JSON, state → AWAITING   │
      │                                                    │
      │  (optional) log_decision                           │
      │       └─ append reasoning note to audit log        │
      └────────────────────────────────────────────────────┘
            │
            ▼
     Final Quote (AWAITING_APPROVAL)
            │
            ▼
     Dashboard: draft customer email → mark sent
     Dashboard: Approve / Reject → IN_TRANSIT → COMPLETED
```

---

## Pipeline States

```
INTAKE → OUT_TO_CARRIERS → FIRST_ROUND_RECEIVED → REBID_ROUND
      → QUOTE_SENT → AWAITING_APPROVAL → APPROVED → IN_TRANSIT → COMPLETED
                                       → LOST
```

Every transition is validated by `state_machine.py` (illegal jumps raise `ValueError`) and appended to `quote["audit_log"]` with timestamp, from/to, and a data payload. Audit entries also record: agent notes, manual bid overrides, and customer email dispatch.

---

## File Map

```
agent.py                  — agent loop, tool executor, EventCallback wiring
src/
  providers.py            — LLM provider abstraction (Ollama / OpenAI / Claude)
  state_machine.py        — transition(), STAGES, audit log
  bidding_engine.py       — two-round bidding orchestration, window enforcement
  carriers.py             — carrier simulation, CARRIER_PROFILES, collect_rates()
  markup_engine.py        — apply_markup(customer_id, cost)
  quote_engine.py         — build_quote(), apply_bid_override(), generate_customer_email(), log_email_sent()
  customers.py            — get_customer() — DB-first, hardcoded fallback
  email_parser.py         — parse_freight_email(), domain→customer_id map, DEMO_EMAIL_TEMPLATES
  database.py             — SQLite: quotes, customers, settings, inbox_messages tables
  state_machine.py        — transition(), create_quote(), get_audit_summary()
  event_bus.py            — Event dataclass, EventCallback type alias
  analytics.py            — compute_analytics() — aggregates over all quotes
app.py                    — Streamlit 6-tab dashboard
```

---

## Dashboard Tabs

| # | Tab | Purpose |
|---|-----|---------|
| 1 | New Quote | Manual intake form → live agent run → result card + email draft |
| 2 | Email Inbox | Paste/load email → parse preview → queue → process via agent |
| 3 | Pipeline | Filterable quote list, approve/reject/transit/complete actions, bid override, email draft |
| 4 | Analytics | KPI cards, pipeline funnel, carrier win rate, customer profit, competitiveness table |
| 5 | Customers | Add/edit customers with markup % and carrier preferences |
| 6 | Model Settings | Ollama live model list, OpenAI/Claude API key + model, bidding window settings |

---

## Event System

`src/event_bus.py` defines a lightweight `Event` dataclass and `EventCallback = Callable[[Event], None]`. The callback is threaded through the entire call chain — `run_agent → _execute_tool → run_bidding → collect_rates` — with no dependency on Streamlit. The UI passes its own callback that accumulates events in `st.session_state` and re-renders a live markdown feed via `st.empty()` on every event.

**Event types:** `state_transition`, `bidding_window_open`, `carrier_bid`, `carrier_timeout`, `round_complete`, `tool_call`, `tool_result`, `agent_reasoning`, `quote_complete`

---

## Time-Simulated Bidding

Carriers are assigned a random `response_time_ms` drawn from per-carrier profiles (Amazon Freight: 200–1800ms; Saia LTL: 700–5000ms). No actual `time.sleep()` is added — the simulation is purely numerical: a carrier's bid is included if `response_time_ms ≤ window_ms`, excluded otherwise. Window thresholds (`bidding_window_ms`, `rebid_window_ms`) are stored in the settings DB and configurable from the dashboard. This maps to real clock time in production (e.g. 3000ms sim ≈ 2-hour real response window).

---

## Email Ingestion

Emails are pasted into the Email Inbox tab (or loaded from four realistic demo templates). `email_parser.py` performs two-pass extraction:

1. **Labeled patterns** — regex for `Origin:`, `Weight:`, `Pickup Date:`, etc.
2. **Prose heuristics** — route pattern `from City, ST to City, ST`, inline weight `about 4,800 lbs`, date proximity scan near pickup keywords

Customer identity is resolved from the **sender email domain** (e.g. `@acmecorp.com → CUST-A`) rather than a field in the email body — customers never see or know their internal ID. Messages queue to an `inbox_messages` SQLite table and are processed one-at-a-time through the same `run_agent()` loop as manual quotes. Email-originated quotes are stamped `source='email'` in the DB and show a `📧 EMAIL` badge in the Pipeline tab.

---

## Provider Abstraction

`src/providers.py` defines `BaseProvider` with three methods: `complete()`, `build_assistant_message()`, `build_tool_result_messages()`. Concrete implementations exist for Ollama, OpenAI, and Anthropic. The agent loop is identical regardless of provider — tool definitions use Claude's format and each provider converts them as needed. Provider and model are saved to the settings DB and loaded automatically on each run.

---

## Database Schema (SQLite)

| Table | Purpose |
|-------|---------|
| `quotes` | Full quote blob + indexed metadata columns (status, customer, sell_rate, source, …) |
| `customers` | Customer ID, name, markup %, preferred carrier list |
| `settings` | Key-value store: provider, model, API keys, bidding windows |
| `inbox_messages` | Email queue: body, sender, status (PENDING/PROCESSED/FAILED), linked quote_id |

`init_db()` runs on every startup — idempotent, seeds defaults, and handles migrations (e.g. `ALTER TABLE` to add `source` column to existing DBs without data loss).

---

## Production Swap-In Guide

| Component | MVP | Production |
|-----------|-----|------------|
| Carrier rates | Simulated ±25% noise per profile | Real carrier APIs or TMS (project44, MercuryGate) |
| Email ingestion | Paste-in with SQLite queue | IMAP polling / Gmail webhook → same parser |
| State persistence | SQLite blob + metadata | Postgres: `quotes` + append-only `quote_events` |
| Bidding windows | Numerical simulation (ms) | Real clock time with async carrier API calls |
| Customer email | Draft + audit log entry | Gmail API send + threading |
| Quote output | Dashboard JSON | PandaDoc PDF generation |
| Carrier notification on approval | Not implemented | Email/API call to winning carrier + BOL generation |
| Billing | Not implemented | QuickBooks API on APPROVED transition |
| Auth | None | OAuth2 / SSO for dashboard access |

---

## Failure Modes

| Failure | Handling |
|---------|---------|
| No carriers respond in Round 1 | `RuntimeError` in `bidding_engine.py`; surfaces to UI as agent error |
| Only 1 carrier responds | Re-bid round skipped (`len(bids) >= 2` guard) |
| Customer not in DB | `get_customer()` falls back to hardcoded dict (10% markup, any carrier) |
| LLM produces text tool call instead of API call | `_extract_tool_calls_from_text()` fallback parser in `agent.py` |
| Illegal state transition | `ValueError` from `transition()` — prevents corruption, surfaced immediately |
| Email missing required fields | Parser returns confidence score + field-level errors; message stays PENDING |
| Bid override on completed quote | UI blocks override for COMPLETED/LOST statuses |
| Existing DB missing `source` column | `PRAGMA table_info` check in `init_db()` → `ALTER TABLE` migration |
