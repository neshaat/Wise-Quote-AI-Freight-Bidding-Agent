# Wise Quote — Freight Bidding Agent

An LLM-powered freight rate automation system. Inbound shipment requests arrive via a manual form or an email inbox, trigger a two-round competitive carrier bidding process, apply customer-specific markups, and produce structured quotes — all orchestrated by an AI agent and managed through a full-featured Streamlit operations dashboard.

---

## How It Works

```
Inbound Request (form or email)
        │
        ▼
  create_quote()          ← State: INTAKE
        │
        ▼
LLM Agent ─────────────── tool_use loop ──────────────────┐
(Ollama / OpenAI / Claude)                                 │
        │                                                  │
        │  1. collect_and_benchmark_rates                  │
        │       ├─ Round 1: eligible carriers bid blind    │
        │       │  (filtered by customer preference)       │
        │       │  carriers outside the time window skip   │
        │       └─ Round 2: non-winners see the floor      │
        │          rate and can counter-bid                │
        │                                                  │
        │  2. apply_customer_markup                        │
        │       └─ sell_rate = cost × (1 + markup%)        │
        │                                                  │
        │  3. generate_final_quote                         │
        │       └─ structured quote JSON → AWAITING        │
        └──────────────────────────────────────────────────┘
              │
              ▼
       Dashboard: approve / reject → in-transit → completed
```

**Key principle:** The LLM sequences tool calls — it never computes rates, selects carriers, or applies markup. All business logic runs in deterministic Python, making every quote auditable, testable, and fully model-independent.

---

## Features

- **Two-round competitive bidding** — Round 1 collects blind bids; Round 2 gives non-winners the floor price and a shorter window to counter. Carriers with simulated response times exceeding the window are excluded automatically.
- **Customer-specific markup engine** — Per-customer markup % and preferred carrier lists stored in SQLite; sell rate, gross profit, and margin calculated on every quote.
- **Email ingestion pipeline** — Paste or load a freight request email; the parser extracts fields via labeled regex and prose heuristics, resolves the customer from the sender domain, and feeds the same `run_agent()` loop as the manual form.
- **Full pipeline state machine** — Every quote moves through validated stages (`INTAKE → OUT_TO_CARRIERS → ... → COMPLETED / LOST`). Illegal transitions raise immediately; every change is appended to an immutable audit log.
- **Live event feed** — Bidding rounds, carrier responses, agent tool calls, and state transitions stream in real time to the Streamlit dashboard via a lightweight event bus.
- **Provider-agnostic** — Works with Ollama (local, free), OpenAI, or Anthropic Claude. Switch models from the dashboard with no code changes.
- **6-tab operations dashboard** — Intake, email inbox, pipeline management, analytics, customer CRUD, and model settings in one place.

---

## Dashboard

| Tab | Purpose |
|-----|---------|
| **New Quote** | Manual intake form → runs agent live → quote card with bid comparison, margin breakdown, and draft customer email |
| **Email Inbox** | Paste a freight email → preview parsed fields → queue → process through agent |
| **Pipeline** | Filterable quote list; approve / reject / mark in-transit / complete; manual bid override; email draft & send |
| **Analytics** | KPI cards, pipeline funnel chart, carrier win rates, customer profitability, competitiveness metrics |
| **Customers** | Add or edit customer profiles: name, markup %, preferred carriers |
| **Model Settings** | Switch LLM provider, enter API keys, tune bidding windows (ms) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent orchestration | [Anthropic Claude](https://anthropic.com) / [OpenAI](https://openai.com) / [Ollama](https://ollama.com) |
| Dashboard | [Streamlit](https://streamlit.io) |
| Charts | [Plotly](https://plotly.com) |
| Data | [Pandas](https://pandas.pydata.org) |
| Persistence | SQLite (via Python `sqlite3`) |
| Language | Python 3.9+ |

---

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo-url>
cd "Wise Quote Bidding Freight Agent"
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Choose an LLM provider

**Ollama — free, runs locally (recommended for quick start)**
```bash
# Install from https://ollama.com, then:
ollama pull qwen3:1.7b    # ~1 GB, fast
ollama serve              # start the local server
```

**OpenAI**
```bash
export OPENAI_API_KEY=sk-...
```

**Anthropic Claude**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

> API keys can also be entered directly in the dashboard under **Model Settings**.

### 3. Launch the dashboard

```bash
streamlit run app.py
```

Opens at **http://localhost:8501**.

---

## Running the Demo (CLI)

Runs 3 predefined scenarios end-to-end and prints a results table — no dashboard needed.

```bash
# Ollama (default)
python demo.py

# Specific model
python demo.py --provider ollama --model qwen3:14b
python demo.py --provider openai --model gpt-4o-mini
python demo.py --provider claude --model claude-sonnet-4-6

# Single scenario (1, 2, or 3)
python demo.py --scenario 1
```

**Scenarios:**

| # | Customer | Route | Markup | Carriers |
|---|----------|-------|--------|---------|
| 1 | Acme Corp | Chicago → Los Angeles | 5% | Amazon Freight only |
| 2 | Beta Imports | New York → Miami | 12% | UPS Freight only |
| 3 | Gamma LLC | Dallas → Seattle | 30% | Any carrier |

---

## Programmatic Usage

```python
from agent import run_agent
from src.providers import get_provider

request = {
    "customer_id": "CUST-A",
    "origin": "Chicago, IL",
    "destination": "Los Angeles, CA",
    "weight_lbs": 4500,
    "cargo_type": "General Merchandise",
    "pickup_date": "2026-04-22",
    "hazmat": False,
}

provider = get_provider("ollama", model="qwen3:1.7b")
# provider = get_provider("claude", model="claude-sonnet-4-6", api_key="sk-ant-...")
# provider = get_provider("openai", model="gpt-4o-mini", api_key="sk-...")

quote = run_agent(request, provider=provider, verbose=True)
print(quote["sell_rate"], quote["winning_carrier_name"])
```

---

## Pipeline States

```
INTAKE → OUT_TO_CARRIERS → FIRST_ROUND_RECEIVED → REBID_ROUND
       → QUOTE_SENT → AWAITING_APPROVAL → APPROVED → IN_TRANSIT → COMPLETED
                                        → LOST
```

Every transition is validated — illegal jumps raise immediately. Every state change, agent note, manual override, and email dispatch is appended to an immutable `audit_log` on the quote.

---

## Project Structure

```
├── agent.py              — agent loop, tool definitions, system prompt, event wiring
├── app.py                — Streamlit 6-tab dashboard
├── demo.py               — CLI demo runner (3 scenarios, --provider / --model / --scenario)
├── requirements.txt
└── src/
    ├── providers.py      — LLM abstraction: Ollama, OpenAI, Claude
    ├── bidding_engine.py — two-round bidding orchestration, window enforcement
    ├── carriers.py       — carrier profiles, simulated response times, rate formulas
    ├── markup_engine.py  — apply_markup(): cost → sell_rate, gross_profit, margin
    ├── quote_engine.py   — build_quote(), customer email generation, bid override
    ├── state_machine.py  — transition(), audit log, pipeline stage definitions
    ├── customers.py      — get_customer(): DB-first with hardcoded fallback
    ├── email_parser.py   — parse_freight_email(), domain → customer_id, demo templates
    ├── database.py       — SQLite schema, CRUD, migrations, default seeding
    ├── event_bus.py      — Event dataclass, EventCallback type alias
    └── analytics.py      — compute_analytics(): KPIs aggregated over all quotes
```

---

## Design Decisions

### LLM orchestrates — tools execute
Business logic (rate selection, markup, state transitions) lives entirely in deterministic Python. The model's only job is to call the right tools in the right order. This means the same input always produces the same quote regardless of which model is used — financials are never at the mercy of model non-determinism.

### One pipeline, two intake channels
The email parser's only responsibility is converting a human-written email into the same structured dictionary that the form produces. After that, both paths call the identical `run_agent()` function. Adding new intake channels (Slack, REST API, WhatsApp) means writing one adapter — the core logic never changes.

### Two-round competitive bidding
Round 1 collects blind bids. Round 2 reveals the lowest rate and gives non-winners a shorter window to counter. This creates genuine price pressure rather than simple rate collection — a carrier who bid $420 blind may return with $385 when told the floor is $390.

### Simulating time without blocking
Carriers are assigned random response times drawn from realistic per-carrier profiles. No `time.sleep()` is used — a carrier is included if its simulated time falls within the configured window, excluded otherwise. In production the only change is the source of the timestamp: a real elapsed API response time rather than a random number.

### State machine as process backbone
Validated transitions and an append-only audit log bring structure to what would otherwise be a loosely coupled agentic workflow. Operations teams always know exactly where every quote stands and have a full tamper-evident history of everything that happened.

---

## Default Customers

Seeded automatically on first run. Editable from the **Customers** tab.

| ID | Name | Markup | Preferred Carriers |
|----|------|--------|--------------------|
| CUST-A | Acme Corp | 5% | Amazon Freight only |
| CUST-B | Beta Imports | 12% | UPS Freight only |
| CUST-C | Gamma LLC | 30% | Any |
| CUST-D | Delta Co | 10% | Any |

---

## Production Roadmap

| Component | Current (MVP) | Production |
|-----------|--------------|------------|
| Carrier rates | Simulated ±25% noise | Real carrier APIs / TMS (project44, MercuryGate) |
| Email ingestion | Paste-in with SQLite queue | IMAP polling / Gmail webhook |
| Persistence | SQLite blob + metadata | Postgres with append-only event table |
| Bidding windows | Numerical simulation (ms) | Real async carrier API calls |
| Customer email | Draft + audit log entry | Gmail API send + threading |
| Quote output | Dashboard JSON | PandaDoc PDF generation |
| Auth | None | OAuth2 / SSO |
| Billing | Not implemented | QuickBooks API on APPROVED transition |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError` | Activate the venv and re-run `pip install -r requirements.txt` |
| Dashboard is blank or errors on start | Run `streamlit run app.py` from the project root, not a subdirectory |
| `ollama: model not found` | Run `ollama pull qwen3:1.7b` and confirm `ollama serve` is running |
| Agent finishes with no quote | Small models sometimes output JSON as text — the built-in fallback parser handles this; try a larger model if it persists |
| `ValueError: Invalid state transition` | The quote is already in a terminal state; check its current status in the Pipeline tab |
