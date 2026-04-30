"""
Demo — runs 3 freight scenarios end-to-end through the agent.

Scenario 1: CUST-A (Amazon Freight only, 5% markup)  — Chicago → LA
Scenario 2: CUST-B (UPS Freight only, 12% markup)    — NY → Miami (heavier)
Scenario 3: CUST-C (any carrier, 30% markup)          — Dallas → Seattle

Run with Ollama (default — no API key needed):
    python demo.py

Run with a specific provider:
    python demo.py --provider ollama --model qwen2.5:3b
    python demo.py --provider openai --model gpt-4o-mini
    python demo.py --provider claude --model claude-sonnet-4-6
"""

import argparse
import os
import sys

from agent import run_agent
from src.providers import get_provider


def parse_args():
    parser = argparse.ArgumentParser(description="Wise Quote Freight Bidding Agent Demo")
    parser.add_argument(
        "--provider", default="ollama",
        choices=["ollama", "openai", "claude"],
        help="LLM provider to use (default: ollama)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name override (default: provider's default model)",
    )
    parser.add_argument(
        "--scenario", type=int, default=None,
        help="Run only scenario N (1, 2, or 3). Omit to run all.",
    )
    return parser.parse_args()


SCENARIOS = [
    {
        "label": "Scenario 1 — Preferred Carrier (Amazon Freight) + 5% markup",
        "request": {
            "customer_id": "CUST-A",
            "origin": "Chicago, IL",
            "destination": "Los Angeles, CA",
            "weight_lbs": 4500,
            "cargo_type": "General Merchandise (Palletized)",
            "pickup_date": "2026-04-07",
            "hazmat": False,
        },
    },
    {
        "label": "Scenario 2 — UPS Only + 12% markup (heavier load)",
        "request": {
            "customer_id": "CUST-B",
            "origin": "New York, NY",
            "destination": "Miami, FL",
            "weight_lbs": 12000,
            "cargo_type": "Electronics",
            "pickup_date": "2026-04-08",
            "hazmat": False,
        },
    },
    {
        "label": "Scenario 3 — Any Carrier + 30% markup (re-bid winner demo)",
        "request": {
            "customer_id": "CUST-C",
            "origin": "Dallas, TX",
            "destination": "Seattle, WA",
            "weight_lbs": 6800,
            "cargo_type": "Retail Goods",
            "pickup_date": "2026-04-09",
            "hazmat": False,
        },
    },
]


def build_provider(args):
    """Build the provider from CLI args, with helpful error messages."""
    kwargs = {}
    if args.model:
        kwargs["model"] = args.model

    if args.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY environment variable not set.")
        sys.exit(1)

    if args.provider == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    return get_provider(args.provider, **kwargs)


def main():
    args = parse_args()
    provider = build_provider(args)

    scenarios = SCENARIOS
    if args.scenario:
        if args.scenario not in (1, 2, 3):
            print("--scenario must be 1, 2, or 3")
            sys.exit(1)
        scenarios = [SCENARIOS[args.scenario - 1]]

    print("\n" + "="*60)
    print("  WISE QUOTE FREIGHT BIDDING AGENT — DEMO")
    print(f"  Provider : {provider.name.upper()}")
    print(f"  Model    : {provider.model}")
    print("="*60)
    print(f"  Running {len(scenarios)} scenario(s)...\n")

    results = []
    for scenario in scenarios:
        print(f"\n>>> {scenario['label']}")
        quote = run_agent(scenario["request"], provider=provider, verbose=True)
        results.append(quote)

    # ── Summary table ──────────────────────────────────────────────────────
    completed = [q for q in results if q.get("final_quote")]
    if not completed:
        print("\nNo quotes completed successfully.")
        return

    print("\n" + "="*60)
    print("  SUMMARY — All Quotes")
    print("="*60)
    print(f"  {'Quote ID':<16} {'Customer':<14} {'Carrier':<20} {'Cost':>8} {'Sell':>8} {'GP':>7} {'Margin':>7}")
    print(f"  {'─'*16} {'─'*14} {'─'*20} {'─'*8} {'─'*8} {'─'*7} {'─'*7}")

    for q in completed:
        fq = q["final_quote"]
        p = fq["pricing"]
        c = fq["selected_carrier"]
        cust = fq["customer"]["name"][:13]
        carrier = c["name"][:19]
        print(
            f"  {fq['quote_id']:<16} {cust:<14} {carrier:<20} "
            f"${p['carrier_cost']:>7.2f} ${p['sell_rate']:>7.2f} "
            f"${p['gross_profit']:>6.2f} {p['gross_margin_pct']:>6.1f}%"
        )

    print("="*60)
    print(f"  Done. {len(completed)}/{len(results)} quotes generated.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
