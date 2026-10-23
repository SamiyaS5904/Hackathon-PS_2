"""
A fake Claude client for tests: it replays a script of responses instead of calling the API.

Why: tests must be free, fast and repeatable (CLAUDE.md: harness tests 1-4 use a fake agent). The fake looks
like the real SDK where engine/agent.py touches it: client.messages.create(...) returns an object with
.content (blocks with .type / .id / .name / .input / .text), .stop_reason and .usage.

Each script step is either a response, an exception to raise (API down, timeout), or a function that receives
the conversation so far and returns a response (to react to what the tools returned).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    type: str
    id: str = ""
    name: str = ""
    input: dict | None = None
    text: str = ""


@dataclass
class Usage:
    input_tokens: int = 1200
    output_tokens: int = 250
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Response:
    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


Step = Response | Exception | Callable[[list], Response]


class ScriptedClient:
    """client.messages.create(**kwargs) -> next scripted step. Records every request in .calls."""

    def __init__(self, steps: list[Step]):
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs) -> Response:
        # Snapshot the conversation: the agent keeps appending to the same list after this call
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.steps:
            raise AssertionError("the agent made more API calls than the script expected")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(kwargs["messages"]) if callable(step) else step


_counter = 0


def tool_call(name: str, tool_input: dict | None = None) -> Response:
    """A response in which Claude calls one tool."""
    global _counter
    _counter += 1
    return Response([Block("tool_use", id=f"toolu_{_counter}", name=name, input=tool_input or {})], "tool_use")


def text_only(text: str = "I think a discount would be nice.") -> Response:
    """A response with no tool call (the agent 'forgot' to decide)."""
    return Response([Block("text", text=text)], "end_turn")


REASONING = {
    "summary": "Overdue regular: 15% off fashion to restart the habit.",
    "signals_used": ["Usually orders every 20 days; last order 20 days ago", "Fashion is 100% of spend"],
    "alternatives_considered": ["Free delivery: weaker for this customer", "Send nothing: habit at risk"],
    "expected_impact": "5% -> 25% chance to buy; expected profit Rs 150",
    "why_now": "Due now and not ordered yet.",
}


def offer_input(**overrides) -> dict:
    """A complete propose_offer / estimate_profit input (strict schemas need every field)."""
    data = {"offer_type": "pct_off", "amount": 15, "product_id": None, "category": "fashion",
            "min_order_rupees": 999, "expiry_hours": 24, "send_at_ist": None,
            "message": "Hi Riya! Get 15% off on fashion above ₹999, valid for 24 hours.", "reasoning": REASONING}
    data.update(overrides)
    return data


def estimate_input(**overrides) -> dict:
    data = offer_input(**overrides)
    for key in ("send_at_ist", "message", "reasoning"):
        data.pop(key, None)
    return data
