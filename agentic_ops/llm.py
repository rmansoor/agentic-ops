"""Provider-neutral LLM client with tool calling and structured output.

Internal message format (converted per provider):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- Anthropic


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            # Consecutive tool results must share one user message.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicClient:
    def __init__(self, api_key: str | None, model: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(self, system, messages, tools=None, force_tool=None, max_tokens=4096) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools]
            if force_tool:
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
        resp = self._client.messages.create(**kwargs)
        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
        return LLMResponse(text="".join(text_parts), tool_calls=calls)


# --------------------------------------------------------------------------- OpenAI


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


class OpenAIClient:
    def __init__(self, api_key: str | None, model: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model

    def chat(self, system, messages, tools=None, force_tool=None, max_tokens=4096) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_openai_messages(system, messages),
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.schema},
                }
                for t in tools
            ]
            if force_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
            for tc in (msg.tool_calls or [])
        ]
        return LLMResponse(text=msg.content or "", tool_calls=calls)


# --------------------------------------------------------------------------- Test double


class FakeLLM:
    """Scripted LLM for tests and offline demos. Each item is an LLMResponse or a callable(messages)."""

    def __init__(self, script: list[LLMResponse | Callable[[list[dict[str, Any]]], LLMResponse]]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, system, messages, tools=None, force_tool=None, max_tokens=4096) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools, "force": force_tool})
        if not self.script:
            raise LLMError("FakeLLM script exhausted")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


# --------------------------------------------------------------------------- Helpers


def make_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, settings.llm_model)
    if settings.llm_provider == "openai":
        return OpenAIClient(settings.openai_api_key, settings.llm_model)
    raise LLMError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (use 'anthropic' or 'openai')")


def structured_call(
    llm: LLMClient,
    system: str,
    prompt: str,
    tool: ToolSpec,
    model_cls: type[T],
    retries: int = 1,
    max_tokens: int = 8192,
) -> T:
    """Force a single tool call and validate its arguments against a Pydantic model (one repair retry)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    last_error = ""
    for _ in range(retries + 1):
        resp = llm.chat(system, messages, tools=[tool], force_tool=tool.name, max_tokens=max_tokens)
        call = next((c for c in resp.tool_calls if c.name == tool.name), None)
        if call is None:
            last_error = "model did not call the required tool"
            messages += [
                {"role": "assistant", "content": resp.text or "(no tool call)"},
                {"role": "user", "content": f"You must call the `{tool.name}` tool."},
            ]
            continue
        try:
            return model_cls.model_validate(call.arguments)
        except ValidationError as exc:
            last_error = str(exc)
            messages += [
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": f"Schema validation failed, fix and call again:\n{exc}",
                },
            ]
    raise LLMError(f"Structured output failed: {last_error}")
