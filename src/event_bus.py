"""
Event bus — structured event system for the freight bidding agent.

Keeps all business logic decoupled from Streamlit.
The UI layer passes an `EventCallback` into run_agent(); the agent and
all downstream modules call it with structured Event objects.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


@dataclass
class Event:
    type: str               # event type identifier (see EVENT_TYPES below)
    message: str            # human-readable one-liner shown in the live feed
    data: dict = field(default_factory=dict)   # structured payload
    elapsed_ms: int = 0     # ms since run_agent() started
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# Recognised event types
EVENT_TYPES = {
    "state_transition",     # pipeline stage change (e.g. INTAKE → OUT_TO_CARRIERS)
    "bidding_window_open",  # a bidding round started with a deadline
    "carrier_bid",          # a carrier responded within the window
    "carrier_timeout",      # a carrier missed the deadline
    "round_complete",       # a bidding round finished (summary)
    "tool_call",            # LLM invoked a tool
    "tool_result",          # tool returned a result
    "agent_reasoning",      # LLM produced reasoning/summary text
    "quote_complete",       # final quote assembled
}

# Type alias for the callback passed through the call chain
EventCallback = Callable[[Event], None]


def noop_callback(event: Event) -> None:
    """Drop-in no-op when no callback is provided."""
    pass
