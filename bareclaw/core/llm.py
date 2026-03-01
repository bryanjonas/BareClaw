"""
LLM client wrappers — Ollama and OpenAI with a unified interface.

Both clients expose:
  chat(model, messages, tools, temperature) -> dict
  chat_stream(model, messages, temperature) -> AsyncIterator[str]

Canonical internal message format (provider-agnostic):
  System:    {"role": "system",    "content": "..."}
  User:      {"role": "user",      "content": "..."}
  Assistant: {"role": "assistant", "content": "...",
              "tool_calls": [{"id": "...", "function": {"name": "...", "arguments": {...}}}]}
  Tool:      {"role": "tool",      "tool_call_id": "...", "content": "..."}

Each client converts to/from its native format internally so agent.py stays
provider-agnostic.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

import ollama
from openai import AsyncOpenAI


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self._client = ollama.AsyncClient(host=base_url)

    def _to_ollama_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip fields Ollama doesn't understand from the canonical format."""
        result = []
        for msg in messages:
            m: dict[str, Any] = {"role": msg["role"], "content": msg.get("content") or ""}
            if "tool_calls" in msg:
                # Ollama doesn't use id or type on tool_calls
                m["tool_calls"] = [
                    {
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        }
                    }
                    for tc in msg["tool_calls"]
                ]
            # Ollama tool messages only need role + content (no tool_call_id)
            result.append(m)
        return result

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns normalised response dict."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "options": {"temperature": temperature},
        }
        if tools:
            kwargs["tools"] = tools

        response = await self._client.chat(**kwargs)
        msg = response.message
        result: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    # Ollama doesn't return IDs — generate them so agent.py can
                    # include them as tool_call_id in the subsequent tool message.
                    "id": str(uuid.uuid4()),
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                        if isinstance(tc.function.arguments, dict)
                        else json.loads(tc.function.arguments or "{}"),
                    },
                }
                for tc in msg.tool_calls
            ]
        return result

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Streaming chat — yields text tokens. Does not support tool calls."""
        async for chunk in await self._client.chat(
            model=model,
            messages=self._to_ollama_messages(messages),
            options={"temperature": temperature},
            stream=True,
        ):
            if chunk.message and chunk.message.content:
                yield chunk.message.content

    async def list_models(self) -> list[str]:
        resp = await self._client.list()
        return [m.model for m in resp.models]


class OpenAIClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)

    def _to_openai_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical messages to OpenAI API format."""
        result = []
        for msg in messages:
            role = msg["role"]
            m: dict[str, Any] = {"role": role, "content": msg.get("content") or ""}
            if "tool_calls" in msg:
                m["tool_calls"] = [
                    {
                        "id": tc.get("id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            # OpenAI requires arguments as a JSON string
                            "arguments": json.dumps(tc["function"]["arguments"])
                            if isinstance(tc["function"]["arguments"], dict)
                            else (tc["function"]["arguments"] or "{}"),
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            if role == "tool":
                m["tool_call_id"] = msg.get("tool_call_id", "")
            result.append(m)
        return result

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns normalised response dict."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        result: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        # Normalise back to dict for internal consistency
                        "arguments": json.loads(tc.function.arguments)
                        if isinstance(tc.function.arguments, str)
                        else (tc.function.arguments or {}),
                    },
                }
                for tc in msg.tool_calls
            ]
        return result

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Streaming chat — yields text tokens."""
        stream = await self._client.chat.completions.create(
            model=model,
            messages=self._to_openai_messages(messages),
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content
