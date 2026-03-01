"""
Agentic loop — orchestrates LLM + tool calls until the model stops calling tools.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from bareclaw.config import AgentConfig
from bareclaw.core import memory as mem_mod
from bareclaw.core import superpowers as sp_mod
from bareclaw.core.llm import OllamaClient
from bareclaw.core.tools import MEMORY_TOOL_NAMES, SUPERPOWER_TOOL_NAMES, get_tool_schemas
from bareclaw.executor import cli as executor

logger = logging.getLogger(__name__)

# Type alias: maps provider name -> client instance (OllamaClient or OpenAIClient)
LLMClients = dict[str, Any]

# Cache of OllamaClients keyed by base_url for agents with ollama_base_url set
_ollama_client_cache: dict[str, OllamaClient] = {}


def _resolve_client(agent: AgentConfig, clients: LLMClients) -> Any:
    # Agent-level Ollama URL overrides the global client
    if agent.provider == "ollama" and agent.ollama_base_url:
        url = agent.ollama_base_url
        if url not in _ollama_client_cache:
            logger.info("Creating OllamaClient for agent '%s' at %s", agent.id, url)
            _ollama_client_cache[url] = OllamaClient(base_url=url)
        return _ollama_client_cache[url]

    client = clients.get(agent.provider)
    if client is None:
        fallback = clients.get("ollama")
        if fallback is None:
            raise RuntimeError(
                f"No LLM client available for provider '{agent.provider}' "
                f"and no 'ollama' fallback configured."
            )
        logger.warning(
            "Provider '%s' not configured for agent '%s' — falling back to ollama.",
            agent.provider, agent.id,
        )
        return fallback
    return client


def _build_system_content(agent: AgentConfig, user_messages: list[dict[str, Any]]) -> str:
    """
    Build the effective system prompt, appending relevant memories and superpowers.
    Keyword-matches the user messages against all memories and superpowers and injects matches.
    """
    user_text = " ".join(
        m.get("content", "") for m in user_messages if m.get("role") == "user"
    )
    system_content = agent.system_prompt

    relevant_mems = mem_mod.find_relevant(user_text)
    if relevant_mems:
        mem_block = "\n\n## Relevant memories\n" + "".join(
            f"\n### {m.title}\n{m.content}\n" for m in relevant_mems
        )
        logger.info(
            "Injecting %d memory/memories for agent '%s': %s",
            len(relevant_mems), agent.id, [m.id for m in relevant_mems],
        )
        system_content += mem_block

    relevant_sps = sp_mod.find_relevant(user_text)
    if relevant_sps:
        sp_block = "\n\n## Available superpowers\n" + "".join(
            f"\n### {sp.name}\n"
            + (f"{sp.description}\n" if sp.description else "")
            + "".join(f"{k}: {v}\n" for k, v in {**sp.config, **sp.secrets}.items())
            for sp in relevant_sps
        )
        logger.info(
            "Injecting %d superpower(s) for agent '%s': %s",
            len(relevant_sps), agent.id, [sp.id for sp in relevant_sps],
        )
        system_content += sp_block

    return system_content


def _dispatch_tool(name: str, arguments: dict[str, Any], workspace: str) -> str:
    """Execute a tool call and return the result as a string."""
    if name == "run_command":
        command = arguments.get("command", "")
        logger.info("Tool run_command: %s (workspace=%s)", command, workspace)
        return executor.run_command(command, workspace)
    if name == "read_file":
        path = arguments.get("path", "")
        logger.info("Tool read_file: %s (workspace=%s)", path, workspace)
        return executor.read_file(path, workspace)
    if name == "list_memories":
        mems = mem_mod.load_all()
        if not mems:
            return "No memories found."
        return "\n".join(
            f"- {m.id}: {m.title} [keywords: {', '.join(m.keywords)}]" for m in mems
        )
    if name == "read_memory":
        mid = arguments.get("id", "")
        m = mem_mod.load_one(mid)
        return m.content if m else f"[error] Memory '{mid}' not found."
    if name == "write_memory":
        mid = arguments.get("id", "")
        mem_mod.save(
            memory_id=mid,
            title=arguments.get("title", mid),
            keywords=arguments.get("keywords", []),
            content=arguments.get("content", ""),
        )
        logger.info("Tool write_memory: saved '%s'", mid)
        return f"Memory '{mid}' saved."
    if name == "list_superpowers":
        sps = sp_mod.load_all()
        if not sps:
            return "No superpowers configured."
        return "\n".join(
            f"- {sp.id}: {sp.name} [keywords: {', '.join(sp.keywords)}]" for sp in sps
        )
    if name == "read_superpower":
        sid = arguments.get("id", "")
        sp = sp_mod.load_one(sid)
        if not sp:
            return f"[error] Superpower '{sid}' not found."
        lines = [f"# {sp.name}"]
        if sp.description:
            lines.append(sp.description)
        for k, v in {**sp.config, **sp.secrets}.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)
    return f"[error] Unknown tool: {name}"


async def run_agent(
    agent: AgentConfig,
    clients: LLMClients,
    user_messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Run the agentic loop for *agent* given an initial list of *user_messages*.

    Returns (final_text_response, full_message_history).
    """
    llm = _resolve_client(agent, clients)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_content(agent, user_messages)},
        *user_messages,
    ]
    tools = (get_tool_schemas(agent.tools)
             + get_tool_schemas(MEMORY_TOOL_NAMES)
             + get_tool_schemas(SUPERPOWER_TOOL_NAMES))

    for iteration in range(agent.max_iterations):
        response = await llm.chat(
            model=agent.model,
            messages=messages,
            tools=tools if tools else None,
            temperature=agent.temperature,
        )

        messages.append({
            "role": "assistant",
            "content": response.get("content", ""),
            **{k: v for k, v in response.items() if k not in ("role", "content")},
        })

        tool_calls = response.get("tool_calls", [])
        if not tool_calls:
            return response.get("content", ""), messages

        for tc in tool_calls:
            fn = tc["function"]
            tool_name = fn["name"]
            tool_args = fn.get("arguments", {})
            tool_result = _dispatch_tool(tool_name, tool_args, agent.workspace)
            logger.debug("Tool %s result: %s", tool_name, tool_result[:200])
            messages.append({
                "role": "tool",
                # tool_call_id is required by OpenAI; Ollama ignores it harmlessly
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

    logger.warning("Agent %s hit max_iterations (%d)", agent.id, agent.max_iterations)
    last_content = next(
        (m.get("content", "") for m in reversed(messages) if m["role"] == "assistant"),
        "I hit the maximum number of steps without completing the task.",
    )
    return last_content, messages


async def run_agent_stream(
    agent: AgentConfig,
    clients: LLMClients,
    user_messages: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """
    Run the agentic loop, streaming the final text response token by token.

    Tool-calling iterations are non-streaming; only the last purely-conversational
    response is yielded as a stream.
    """
    llm = _resolve_client(agent, clients)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_content(agent, user_messages)},
        *user_messages,
    ]
    tools = (get_tool_schemas(agent.tools)
             + get_tool_schemas(MEMORY_TOOL_NAMES)
             + get_tool_schemas(SUPERPOWER_TOOL_NAMES))

    for iteration in range(agent.max_iterations):
        response = await llm.chat(
            model=agent.model,
            messages=messages,
            tools=tools if tools else None,
            temperature=agent.temperature,
        )

        tool_calls = response.get("tool_calls", [])

        if not tool_calls:
            messages.append({"role": "assistant", "content": response.get("content", "")})
            content = response.get("content", "")
            if content:
                yield content
            return

        messages.append({
            "role": "assistant",
            "content": response.get("content", ""),
            **{k: v for k, v in response.items() if k not in ("role", "content")},
        })

        for tc in tool_calls:
            fn = tc["function"]
            tool_name = fn["name"]
            tool_args = fn.get("arguments", {})
            tool_result = _dispatch_tool(tool_name, tool_args, agent.workspace)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

    yield "I hit the maximum number of steps without completing the task."
