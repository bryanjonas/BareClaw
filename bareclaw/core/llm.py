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
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import ollama
from openai import AsyncOpenAI

CODEX_CLIENT_ID    = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL    = "https://auth.openai.com/oauth/token"
CODEX_BACKEND_URL  = "https://chatgpt.com/backend-api/codex"
CODEX_SECRETS_FILE = Path(__file__).parents[2] / "secrets" / "codex.env"
_REFRESH_BUFFER    = 300  # seconds before expiry to trigger refresh


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


class CodexOAuthClient:
    """Client that authenticates via Codex CLI OAuth tokens and uses the Responses API.

    Reads access tokens from ~/.codex/auth.json (written by `codex login`).
    Automatically refreshes expired tokens using the stored refresh token.
    Uses the OpenAI Responses API (POST /responses) as required by the Codex backend.
    """

    def __init__(
        self,
        secrets_file: Path = CODEX_SECRETS_FILE,
        base_url: str | None = None,
    ) -> None:
        self._secrets_file = secrets_file
        self._base_url = base_url or CODEX_BACKEND_URL

    def _read_secrets(self) -> dict[str, str]:
        from bareclaw.config import _parse_dotenv
        if not self._secrets_file.exists():
            raise FileNotFoundError("Codex not connected. Visit /settings to authenticate.")
        return _parse_dotenv(self._secrets_file)

    def _write_secrets(self, access_token: str, refresh_token: str, expiry: int) -> None:
        self._secrets_file.parent.mkdir(parents=True, exist_ok=True)
        self._secrets_file.write_text(
            f"access_token={access_token}\nrefresh_token={refresh_token}\ntoken_expiry={expiry}\n"
        )
        self._secrets_file.chmod(0o600)

    async def _get_token(self) -> str:
        s = self._read_secrets()
        access_token = s.get("access_token", "")
        expiry = int(s.get("token_expiry", "0"))
        if access_token and time.time() + _REFRESH_BUFFER < expiry:
            return access_token
        # Token expired or expiring soon — refresh via OAuth token endpoint.
        # Must be form-encoded, not JSON (standard OAuth 2.0 requirement).
        async with httpx.AsyncClient() as http:
            resp = await http.post(CODEX_TOKEN_URL, data={
                "grant_type":    "refresh_token",
                "refresh_token": s["refresh_token"],
                "client_id":     CODEX_CLIENT_ID,
            })
            resp.raise_for_status()
            data = resp.json()
        new_expiry = int(time.time()) + int(data.get("expires_in", 3600))
        self._write_secrets(
            data["access_token"],
            data.get("refresh_token", s["refresh_token"]),
            new_expiry,
        )
        return data["access_token"]

    def _make_client(self, token: str) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=token, base_url=self._base_url)

    def _to_responses_input(self, messages: list[dict[str, Any]]) -> list[Any]:
        """Convert canonical messages to Responses API input items.

        System messages are handled separately as `instructions`; skip them here.
        Assistant tool calls become function_call items; tool results become
        function_call_output items.
        """
        items: list[Any] = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                continue
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg["tool_call_id"],
                    "output": msg.get("content") or "",
                })
            elif role == "assistant" and msg.get("tool_calls"):
                # If there's also text content, emit it as a message first.
                if msg.get("content"):
                    items.append({"role": "assistant", "content": msg["content"], "type": "message"})
                for tc in msg["tool_calls"]:
                    items.append({
                        "type": "function_call",
                        "call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": json.dumps(tc["function"]["arguments"])
                        if isinstance(tc["function"]["arguments"], dict)
                        else (tc["function"]["arguments"] or "{}"),
                    })
            else:
                items.append({"role": role, "content": msg.get("content") or "", "type": "message"})
        return items

    def _to_responses_tools(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert chat-completions tool schemas to Responses API tool format."""
        result = []
        for t in tools:
            fn = t.get("function", {})
            result.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        return result

    def _parse_response(self, response: Any) -> dict[str, Any]:
        """Convert a Responses API response to a canonical message dict."""
        text = ""
        tool_calls = []
        for item in response.output:
            if item.type == "function_call":
                tool_calls.append({
                    "id": item.call_id,
                    "function": {
                        "name": item.name,
                        "arguments": json.loads(item.arguments)
                        if isinstance(item.arguments, str)
                        else (item.arguments or {}),
                    },
                })
            elif item.type == "message":
                for block in (item.content or []):
                    if hasattr(block, "text"):
                        text += block.text
        result: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            result["tool_calls"] = tool_calls
        return result

    def _get_instructions(self, messages: list[dict[str, Any]]) -> str:
        """Extract system message content as Responses API instructions."""
        for msg in messages:
            if msg["role"] == "system":
                return msg.get("content") or ""
        return ""

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        # Codex backend requires stream=True — collect via the streaming API.
        token = await self._get_token()
        client = self._make_client(token)
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": self._get_instructions(messages),
            "input": self._to_responses_input(messages),
            "store": False,
        }
        if tools:
            kwargs["tools"] = self._to_responses_tools(tools)
            kwargs["tool_choice"] = "auto"
        async with client.responses.stream(**kwargs) as stream:
            response = await stream.get_final_response()
        return self._parse_response(response)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        token = await self._get_token()
        client = self._make_client(token)
        async with client.responses.stream(
            model=model,
            instructions=self._get_instructions(messages),
            input=self._to_responses_input(messages),
            store=False,
        ) as stream:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta
