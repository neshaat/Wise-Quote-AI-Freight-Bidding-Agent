# Wise Quote Freight Bidding Agent — Setup Guide

## Overview

This system automates freight rate procurement using an LLM-powered agent that runs competitive two-round carrier bidding, applies customer-specific markups, and produces structured freight quotes. It ships with a Streamlit operations dashboard and a CLI demo runner. The agent works with Ollama (local, free), OpenAI, or Anthropic Claude.

---

## Prerequisites

- Python 3.9+
- One LLM backend (see options below)

---

## Installation

```bash
cd "Wise Quote Bidding Freight Agent"
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## LLM Provider Setup

Choose one of the three options. Ollama requires no API key and is the default.

### Option A — Ollama (default, free, runs locally)

1. Install Ollama: https://ollama.com/download
2. Pull a model:
   ```bash
   ollama pull qwen3:1.7b     # ~1 GB, fast — recommended for quick start
   ollama pull qwen3:14b      # ~9 GB, higher quality
   ```
3. Start the server (if not already running as a background service):
   ```bash
   ollama serve
   ```
   Ollama listens at `http://localhost:11434` by default.

### Option B — OpenAI

```bash
export OPENAI_API_KEY=sk-...
```

### Option C — Anthropic Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

API keys can also be entered inside the dashboard (Model Settings tab) instead of environment variables.

---

## Running the Dashboard

```bash
streamlit run app.py
```

Opens at **http://localhost:8501**.

### First-run walkthrough

1. **Model Settings tab** — select your provider; enter API key if using OpenAI or Claude
2. **New Quote tab** — fill in origin, destination, weight, cargo type, customer, pickup date → click **Run Agent**
3. Watch the live event feed: bidding round open → carrier bids arrive → round 2 rebid → winner selected
4. Review the quote card: sell rate, carrier breakdown, gross margin, and draft customer email
5. **Pipeline tab** — approve or reject the quote; override the winning carrier/rate manually if needed

### Dashboard tabs

| Tab | Purpose |
|-----|---------|
| New Quote | Manual intake form → agent → live bid feed → quote card |
| Email Inbox | Paste a freight request email → auto-extract fields → process through agent |
| Pipeline | All quotes with status filters; approve / reject / mark in-transit / complete |
| Analytics | KPI cards, carrier win rates, customer profitability, funnel chart |
| Customers | Add or edit customer profiles (name, markup %, preferred carriers) |
| Model Settings | Switch provider, enter API keys, tune bidding windows (ms) |

---

## Running the Demo (CLI)

Runs 3 predefined shipping scenarios end-to-end and prints a results table.

```bash
# Default — Ollama with qwen3:1.7b
python demo.py

# Specific Ollama model
python demo.py --provider ollama --model qwen3:14b

# OpenAI
python demo.py --provider openai --model gpt-4o-mini

# Anthropic Claude
python demo.py --provider claude --model claude-sonnet-4-6

# Run a single scenario (1, 2, or 3)
python demo.py --scenario 1
```

**Scenarios:**
1. CUST-A — Acme Corp, Amazon Freight only, 5% markup (Chicago → LA)
2. CUST-B — Beta Imports, UPS Freight only, 12% markup (NY → Miami)
3. CUST-C — Gamma LLC, any carrier, 30% markup (Dallas → Seattle)

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

# Ollama (no key needed)
provider = get_provider("ollama", model="qwen3:1.7b")

# Claude
# provider = get_provider("claude", model="claude-sonnet-4-6", api_key="sk-ant-...")

# OpenAI
# provider = get_provider("openai", model="gpt-4o-mini", api_key="sk-...")

quote = run_agent(request, provider=provider, verbose=True)
print(quote["sell_rate"], quote["winning_carrier_name"])
```

---

## Environment Variables

| Variable | Required for |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI provider |
| `ANTHROPIC_API_KEY` | Anthropic Claude provider |

Neither variable is needed for Ollama.

---

## Database

`freight_quotes.db` (SQLite) is created automatically in the working directory on first run. No manual setup is needed.

**Default customers seeded automatically:**

| ID | Name | Markup | Preferred Carriers |
|----|------|--------|--------------------|
| CUST-A | Acme Corp | 5% | Amazon Freight only |
| CUST-B | Beta Imports | 12% | UPS Freight only |
| CUST-C | Gamma LLC | 30% | Any |
| CUST-D | Delta Co | 10% | Any |

Add or edit customers via the **Customers** tab in the dashboard.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError` | Activate the venv (`source .venv/bin/activate`) and re-run `pip install -r requirements.txt` |
| Dashboard is blank or throws a Streamlit error | Run `streamlit run app.py` from the project root directory, not a subdirectory |
| `ollama: model not found` | Run `ollama pull qwen3:1.7b` and confirm `ollama serve` is running |
| Agent finishes with no quote / no tool calls | Small models sometimes output JSON as plain text — the built-in fallback parser handles this automatically; if it still fails, try a larger model |
| `ValueError: Invalid state transition` | The quote is already in a terminal state; check its current status in the Pipeline tab |
| Dashboard shows "Ollama not running" warning | Start Ollama with `ollama serve` before opening the dashboard |
