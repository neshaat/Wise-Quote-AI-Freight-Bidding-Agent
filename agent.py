"""
Freight Bidding Agent — provider-agnostic orchestrator.

Supports Ollama (default), OpenAI, and Claude via the provider abstraction
in src/providers.py. The agent loop is identical regardless of which LLM is
used — only the provider class differs.

Usage:
    from agent import run_agent
    from src.providers import get_provider

    # Ollama (default — no API key needed)
    quote = run_agent(request)

    # OpenAI
    quote = run_agent(request, provider=get_provider("openai"))

    # Claude
    quote = run_agent(request, provider=get_provider("claude"))
"""

import json
import re
import datetime
import time
from typing import Optional

from src.state_machine import create_quote, get_audit_summary
from src.bidding_engine import run_bidding
from src.markup_engine import apply_markup
from src.quote_engine import build_quote
from src.customers import get_customer
from src.providers import get_provider, BaseProvider, ToolCall
from src.database import save_quote
from src.event_bus import Event, EventCallback

# ── Tool definitions (Claude's format — providers convert as needed) ──────────

TOOLS = [
    {
        "name": "collect_and_benchmark_rates",
        "description": (
            "Triggers Round 1 AND Round 2 (re-bid) carrier rate collection for the freight request. "
            "Automatically filters carriers by customer preference, runs the competitive re-bid round, "
            "and selects the lowest valid rate. Returns the winning bid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "quote_id": {
                    "type": "string",
                    "description": "The quote ID to run bidding for.",
                },
            },
            "required": ["quote_id"],
        },
    },
    {
        "name": "apply_customer_markup",
        "description": (
            "Applies the customer-specific markup percentage to the winning carrier cost. "
            "Returns cost, markup %, sell rate, gross profit, and gross margin %."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
            },
            "required": ["quote_id"],
        },
    },
    {
        "name": "generate_final_quote",
        "description": (
            "Assembles and returns the final structured quote that would be sent to the customer. "
            "Includes all bids, pricing breakdown, carrier selection, and audit trail. "
            "Call this as the LAST step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
            },
            "required": ["quote_id"],
        },
    },
    {
        "name": "log_decision",
        "description": "Log a reasoning note or decision to the audit trail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
                "note": {
                    "type": "string",
                    "description": "The reasoning or decision to log.",
                },
            },
            "required": ["quote_id", "note"],
        },
    },
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Wise Quote Freight Bidding Agent. Your job is to process inbound freight requests and produce competitive, accurate quotes for customers as fast as possible.

You MUST follow this exact workflow in order:
1. Call collect_and_benchmark_rates — this runs Round 1 (collect from all eligible carriers) AND Round 2 (re-bid: challengers try to beat the lowest rate). It returns the winning carrier.
2. Call apply_customer_markup — applies the customer's markup % to the winning cost.
3. Call generate_final_quote — assembles the final quote. This is the last step.

Business rules:
- Customer markup rates: CUST-A=5%, CUST-B=12%, CUST-C=30%, CUST-D=10% (default 10%)
- Carrier preferences: CUST-A only uses Amazon Freight, CUST-B only uses UPS Freight
- Re-bid logic: after round 1, non-winners are told the lowest rate and asked to beat it
- Quote expires 4 hours after generation

You may use log_decision at any point to record your reasoning.
Always complete all 3 required steps. Do not skip steps."""


# ── Tool executor ─────────────────────────────────────────────────────────────

def _execute_tool(tool_call: ToolCall, quote_store: dict, on_event: Optional[EventCallback] = None) -> str:
    """Routes a tool call to the actual business logic. Returns a JSON string result."""
    quote_id = tool_call.input.get("quote_id")
    quote = quote_store.get(quote_id)

    if quote is None:
        return json.dumps({"error": f"Quote {quote_id} not found"})

    try:
        if tool_call.name == "collect_and_benchmark_rates":
            # Idempotent: if bidding already ran, return the cached winning bid
            if quote.get("winning_bid"):
                winning_bid = quote["winning_bid"]
            else:
                winning_bid = run_bidding(quote, on_event=on_event)
            return json.dumps({
                "success": True,
                "winning_carrier": winning_bid["carrier_name"],
                "winning_rate": winning_bid["rate"],
                "bid_round": winning_bid["round"],
                "transit_days": winning_bid["transit_days"],
                "total_bids_round1": len(quote["carrier_bids_round1"]),
                "total_bids_round2": len(quote["carrier_bids_round2"]),
            })

        elif tool_call.name == "apply_customer_markup":
            if not quote.get("winning_bid"):
                return json.dumps({"error": "No winning bid yet — run collect_and_benchmark_rates first."})
            # Idempotent: recalculating markup with same inputs always yields the same result
            customer_id = quote["request"]["customer_id"]
            cost = quote["winning_bid"]["rate"]
            markup_result = apply_markup(customer_id, cost)
            quote["markup_result"] = markup_result
            return json.dumps({"success": True, **markup_result})

        elif tool_call.name == "generate_final_quote":
            # Idempotent: if already generated, return the cached result
            if quote.get("final_quote"):
                final = quote["final_quote"]
            else:
                final = build_quote(quote)
            return json.dumps({"success": True, "quote_id": final["quote_id"], "status": "QUOTE_SENT"})

        elif tool_call.name == "log_decision":
            note = tool_call.input.get("note", "")
            quote["audit_log"].append({
                "id": "agent-note",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "note",
                "state": quote["status"],
                "data": {"agent_note": note},
            })
            return json.dumps({"success": True, "logged": note})

        else:
            return json.dumps({"error": f"Unknown tool: {tool_call.name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Fallback: extract tool calls from text output ────────────────────────────

# Tool names the agent is allowed to call
_KNOWN_TOOLS = {t["name"] for t in TOOLS}

def _extract_tool_calls_from_text(text: str, quote_id: str) -> list:
    """
    Fallback parser for when a small model outputs a tool call as JSON text
    instead of using the API's native function-calling mechanism.

    Looks for patterns like:
      {"name": "tool_name", ...}
      <tool_call>{"name": "tool_name", ...}</tool_call>

    Returns a list of ToolCall objects (may be empty).
    """
    tool_calls = []

    # Strip <think>...</think> blocks (qwen3 emits these)
    text_clean = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Look for JSON objects that have a "name" field matching a known tool
    candidates = re.findall(r'\{[^{}]+\}', text_clean, re.DOTALL)
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        name = obj.get("name") or obj.get("function") or obj.get("tool")
        if name not in _KNOWN_TOOLS:
            continue

        # Extract parameters (may be nested under "parameters", "arguments", "input", or top-level)
        params = obj.get("parameters") or obj.get("arguments") or obj.get("input") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {}

        # Ensure quote_id is present (inject if missing)
        if "quote_id" not in params:
            params["quote_id"] = quote_id

        tool_calls.append(ToolCall(
            id=f"fallback-{name}-{len(tool_calls)}",
            name=name,
            input=params,
        ))

    return tool_calls


# ── Main agent loop ───────────────────────────────────────────────────────────

def run_agent(
    freight_request: dict,
    provider: BaseProvider = None,
    verbose: bool = True,
    on_event: Optional[EventCallback] = None,
) -> dict:
    """
    Run the freight bidding agent for a given request.

    Args:
        freight_request: dict with origin, destination, weight_lbs, cargo_type,
                         pickup_date, customer_id, hazmat (optional)
        provider: LLM provider instance (default: Ollama qwen3:1.7b)
        verbose: if True, prints agent reasoning and state transitions

    Returns:
        the completed quote dict
    """
    if provider is None:
        provider = get_provider("ollama")

    start_time = time.time()

    def _emit(event_type: str, message: str, data: dict = None):
        if on_event:
            elapsed = int((time.time() - start_time) * 1000)
            on_event(Event(type=event_type, message=message,
                           data=data or {}, elapsed_ms=elapsed))

    # Initialize state
    quote = create_quote(freight_request)
    _emit("state_transition", "CREATED → INTAKE", {"status": "INTAKE"})
    quote_store = {quote["quote_id"]: quote}
    customer = get_customer(freight_request["customer_id"])

    if verbose:
        print(f"\n{'='*60}")
        print(f"  FREIGHT BIDDING AGENT  [{provider.name.upper()} / {provider.model}]")
        print(f"  Quote: {quote['quote_id']}")
        print(f"{'='*60}")
        print(f"  Customer : {customer['name']} ({freight_request['customer_id']})")
        print(f"  Route    : {freight_request['origin']} → {freight_request['destination']}")
        print(f"  Weight   : {freight_request.get('weight_lbs', '?')} lbs")
        print(f"  Cargo    : {freight_request.get('cargo_type', '?')}")
        print(f"  Pickup   : {freight_request.get('pickup_date', '?')}")
        print(f"{'='*60}\n")

    # Initial user message
    user_message = (
        f"Process this freight request and generate a quote.\n\n"
        f"Quote ID: {quote['quote_id']}\n"
        f"Customer: {customer['name']} (ID: {freight_request['customer_id']})\n"
        f"Origin: {freight_request['origin']}\n"
        f"Destination: {freight_request['destination']}\n"
        f"Weight: {freight_request.get('weight_lbs', 'unknown')} lbs\n"
        f"Cargo Type: {freight_request.get('cargo_type', 'General')}\n"
        f"Pickup Date: {freight_request.get('pickup_date', 'ASAP')}\n"
        f"Hazmat: {freight_request.get('hazmat', False)}\n"
    )

    messages = [{"role": "user", "content": user_message}]

    # ── Agent loop ────────────────────────────────────────────────────────────
    max_iterations = 10  # safety cap
    for iteration in range(max_iterations):
        text, tool_calls = provider.complete(messages, TOOLS, SYSTEM_PROMPT)

        # Strip <think> blocks from display text (qwen3 emits these)
        display_text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        if verbose and display_text:
            print(f"[Agent] {display_text}")
        if display_text:
            _emit("agent_reasoning", display_text[:200], {"text": display_text})

        # Fallback: if the model output JSON tool calls as text instead of API calls
        if not tool_calls and text:
            tool_calls = _extract_tool_calls_from_text(text, quote["quote_id"])
            if tool_calls and verbose:
                print(f"[Fallback] Parsed {len(tool_calls)} tool call(s) from text output")

        # If no tool calls (even after fallback), the agent is done
        if not tool_calls:
            break

        # Execute each tool call
        results = []
        for tc in tool_calls:
            if verbose:
                print(f"[Tool]  → {tc.name}({json.dumps(tc.input)})")
            _emit("tool_call", f"Calling {tc.name}", {"tool": tc.name, "input": tc.input})

            result_str = _execute_tool(tc, quote_store, on_event=on_event)
            results.append(result_str)

            if verbose:
                result_data = json.loads(result_str)
                print(f"[Tool]  ← {json.dumps(result_data, indent=2)}\n")

            result_data = json.loads(result_str)
            if result_data.get("success"):
                # Emit a concise summary for key tool results
                if tc.name == "apply_customer_markup":
                    _emit("tool_result",
                          f"Markup applied — sell rate ${result_data.get('sell_rate', 0):.2f}  "
                          f"profit ${result_data.get('gross_profit', 0):.2f}  "
                          f"margin {result_data.get('gross_margin_pct', 0)}%",
                          result_data)
                elif tc.name == "generate_final_quote":
                    _emit("tool_result",
                          f"Quote assembled: {result_data.get('quote_id')}",
                          result_data)
                else:
                    _emit("tool_result", f"{tc.name} completed", result_data)
            elif result_data.get("error"):
                _emit("tool_result", f"{tc.name} error: {result_data['error']}", result_data)

        # Append assistant message + tool results to history
        messages.append(provider.build_assistant_message(text, tool_calls))
        messages.extend(provider.build_tool_result_messages(tool_calls, results))

        # If generate_final_quote was called, we're done
        if any(tc.name == "generate_final_quote" for tc in tool_calls):
            # Let the model produce a final text summary
            final_text, _ = provider.complete(messages, [], SYSTEM_PROMPT)
            final_display = re.sub(r'<think>.*?</think>', '', final_text, flags=re.DOTALL).strip() if final_text else ""
            if verbose and final_display:
                print(f"[Agent] {final_display}")
            if final_display:
                _emit("agent_reasoning", final_display[:200], {"text": final_display})
            _emit("quote_complete", f"Quote complete: {quote.get('quote_id')}", {"quote_id": quote.get("quote_id")})
            break

    # ── Persist to database ───────────────────────────────────────────────────
    try:
        save_quote(quote)
    except Exception as e:
        if verbose:
            print(f"[DB] Warning: could not save quote to database: {e}")

    # ── Print final result ────────────────────────────────────────────────────
    if verbose and quote.get("final_quote"):
        fq = quote["final_quote"]
        p = fq["pricing"]
        c = fq["selected_carrier"]
        print(f"\n{'─'*60}")
        print(f"  FINAL QUOTE — {fq['quote_id']}")
        print(f"{'─'*60}")
        print(f"  Carrier    : {c['name']}  (Round {c['bid_round']})")
        print(f"  Transit    : {c['transit_days']} days")
        print(f"  Cost       : ${p['carrier_cost']:.2f}")
        print(f"  Markup     : {p['markup_pct']}%")
        print(f"  Sell Rate  : ${p['sell_rate']:.2f}  ← customer pays this")
        print(f"  Gross P&L  : ${p['gross_profit']:.2f}  ({p['gross_margin_pct']}% margin)")
        print(f"  Expires    : {fq['quote_expires_at']}")
        print(f"\n  Audit Trail:")
        for line in get_audit_summary(quote):
            print(line)
        print(f"{'─'*60}\n")

    return quote
