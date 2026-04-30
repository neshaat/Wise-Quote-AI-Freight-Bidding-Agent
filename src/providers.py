"""
LLM Provider abstraction — supports Ollama (default), OpenAI, and Claude.

Each provider handles:
  - Its own API client and message format
  - Tool definition conversion (Claude format → provider format)
  - Tool call parsing from responses
  - Building assistant + tool result messages for history

Usage:
    from src.providers import get_provider

    provider = get_provider("ollama")   # default
    provider = get_provider("openai",  model="gpt-4o-mini", api_key="sk-...")
    provider = get_provider("claude",  model="claude-sonnet-4-6", api_key="sk-ant-...")
"""

import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple


# ── Shared data class for tool calls ─────────────────────────────────────────

@dataclass
class ToolCall:
    """Normalized tool call returned by any provider."""
    id: str
    name: str
    input: dict


# ── Base class ────────────────────────────────────────────────────────────────

class BaseProvider:
    """
    Abstract LLM provider. Subclasses implement complete() and message-building
    helpers so the agent loop in agent.py stays provider-agnostic.
    """

    name: str = "base"
    model: str = ""

    def complete(
        self,
        messages: list,
        tools: list,
        system: str = "",
    ) -> Tuple[str, List[ToolCall]]:
        """
        Send messages to the LLM and return (response_text, tool_calls).
        tools are in Claude's native format (input_schema key).
        """
        raise NotImplementedError

    def build_assistant_message(self, text: str, tool_calls: List[ToolCall]) -> dict:
        """Return the assistant message to append to history after a response."""
        raise NotImplementedError

    def build_tool_result_messages(
        self, tool_calls: List[ToolCall], results: List[str]
    ) -> list:
        """Return message(s) to append after executing tool calls."""
        raise NotImplementedError

    def _tools_to_openai_format(self, tools: list) -> list:
        """Convert Claude-format tool definitions to OpenAI function-calling format."""
        converted = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            })
        return converted


# ── Ollama provider (OpenAI-compatible endpoint) ──────────────────────────────

class OllamaProvider(BaseProvider):
    """
    Uses Ollama's OpenAI-compatible API at http://localhost:11434/v1.
    Default model: qwen3:1.7b  (small, fast, supports tool calling)

    Setup:
        ollama serve                   # start the server
        ollama pull qwen3:1.7b         # download the model (~1 GB)
    """

    name = "ollama"

    def __init__(self, model: str = "qwen3:1.7b", base_url: str = "http://localhost:11434/v1"):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(base_url=base_url, api_key="ollama")  # api_key required but ignored by Ollama

    def complete(self, messages: list, tools: list, system: str = "") -> Tuple[str, List[ToolCall]]:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            tools=self._tools_to_openai_format(tools) if tools else None,
            tool_choice="auto" if tools else None,
        )

        msg = response.choices[0].message
        text = msg.content or ""
        tool_calls = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        return text, tool_calls

    def build_assistant_message(self, text: str, tool_calls: List[ToolCall]) -> dict:
        msg: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                }
                for tc in tool_calls
            ]
        return msg

    def build_tool_result_messages(self, tool_calls: List[ToolCall], results: List[str]) -> list:
        return [
            {"role": "tool", "tool_call_id": tc.id, "content": result}
            for tc, result in zip(tool_calls, results)
        ]


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAIProvider(BaseProvider):
    """
    Uses OpenAI's API. Requires OPENAI_API_KEY env var or explicit api_key.
    Default model: gpt-4o-mini
    """

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    def complete(self, messages: list, tools: list, system: str = "") -> Tuple[str, List[ToolCall]]:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            tools=self._tools_to_openai_format(tools) if tools else None,
            tool_choice="auto" if tools else None,
        )

        msg = response.choices[0].message
        text = msg.content or ""
        tool_calls = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        return text, tool_calls

    def build_assistant_message(self, text: str, tool_calls: List[ToolCall]) -> dict:
        msg: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                }
                for tc in tool_calls
            ]
        return msg

    def build_tool_result_messages(self, tool_calls: List[ToolCall], results: List[str]) -> list:
        return [
            {"role": "tool", "tool_call_id": tc.id, "content": result}
            for tc, result in zip(tool_calls, results)
        ]


# ── Claude provider (Anthropic) ───────────────────────────────────────────────

class ClaudeProvider(BaseProvider):
    """
    Uses Anthropic's Claude API. Requires ANTHROPIC_API_KEY env var or explicit api_key.
    Default model: claude-sonnet-4-6
    """

    name = "claude"

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: Optional[str] = None):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def complete(self, messages: list, tools: list, system: str = "") -> Tuple[str, List[ToolCall]]:
        kwargs = {}
        if tools:  # Anthropic API rejects an empty tools list
            kwargs["tools"] = tools
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system or "",
            messages=messages,
            **kwargs,
        )

        text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        return text, tool_calls

    def build_assistant_message(self, text: str, tool_calls: List[ToolCall]) -> dict:
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        return {"role": "assistant", "content": content}

    def build_tool_result_messages(self, tool_calls: List[ToolCall], results: List[str]) -> list:
        return [{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                }
                for tc, result in zip(tool_calls, results)
            ],
        }]


# ── Factory ───────────────────────────────────────────────────────────────────

def get_provider(name: str = "ollama", **kwargs) -> BaseProvider:
    """
    Return a configured provider instance.

    Args:
        name: "ollama" | "openai" | "claude"
        **kwargs: passed to the provider constructor (model, api_key, base_url, ...)

    Examples:
        get_provider()                                    # Ollama qwen3:1.7b (default)
        get_provider("ollama", model="llama3.2:3b")      # different Ollama model
        get_provider("openai", model="gpt-4o-mini")      # OpenAI
        get_provider("claude", model="claude-sonnet-4-6") # Claude
    """
    providers = {
        "ollama": OllamaProvider,
        "openai": OpenAIProvider,
        "claude": ClaudeProvider,
    }
    if name not in providers:
        raise ValueError(f"Unknown provider '{name}'. Choose from: {list(providers.keys())}")
    return providers[name](**kwargs)
