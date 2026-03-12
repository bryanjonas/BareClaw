"""
Agentic loop — orchestrates LLM + tool calls until the model stops calling tools.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from bareclaw.config import AgentConfig, AppConfig
from bareclaw.core import memory as mem_mod
from bareclaw.core import projects as proj_mod
from bareclaw.core import superpowers as sp_mod
from bareclaw.core.tools import AGENT_TOOL_NAMES, MEMORY_TOOL_NAMES, PROJECT_TOOL_NAMES, SUPERPOWER_TOOL_NAMES, get_tool_schemas
from bareclaw.executor import cli as executor

logger = logging.getLogger(__name__)

# Type alias: maps provider id -> client instance (OllamaClient or OpenAIClient)
LLMClients = dict[str, Any]


def _resolve_client(agent: AgentConfig, clients: LLMClients) -> Any:
    client = clients.get(agent.provider)
    if client is None:
        fallback_id = next(iter(clients), None)
        if fallback_id is None:
            raise RuntimeError("No LLM clients configured.")
        logger.warning(
            "Provider '%s' not found for agent '%s' — falling back to '%s'.",
            agent.provider, agent.id, fallback_id,
        )
        return clients[fallback_id]
    return client


def _build_system_content(
    agent: AgentConfig,
    user_messages: list[dict[str, Any]],
    platform_identity: str = "",
) -> str:
    """
    Build the effective system prompt, appending relevant memories and superpowers.
    Keyword-matches the user messages against all memories and superpowers and injects matches.
    """
    user_text = " ".join(
        m.get("content", "") for m in user_messages if m.get("role") == "user"
    )
    # Start with platform identity (if provided), then agent's system prompt
    system_content = ""
    if platform_identity:
        system_content = platform_identity.strip() + "\n\n"
    system_content += agent.system_prompt

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
            + "".join(f"{k}: {v}\n" for k, v in sp.config.items())
            + (
                f"Credentials: source {sp.secrets_path}"
                + (f"  # exports: {', '.join(sp.secrets.keys())}" if sp.secrets else "")
                + "\n"
                if sp.secrets_path else ""
            )
            for sp in relevant_sps
        )
        logger.info(
            "Injecting %d superpower(s) for agent '%s': %s",
            len(relevant_sps), agent.id, [sp.id for sp in relevant_sps],
        )
        system_content += sp_block

    relevant_projs = proj_mod.find_relevant(user_text)
    if relevant_projs:
        proj_block = "\n\n## Relevant projects\n"
        for proj in relevant_projs:
            proj_block += f"\n### {proj.name}\n"
            if proj.description:
                proj_block += f"{proj.description}\n"
            if proj.memories:
                proj_block += f"Memories: {', '.join(proj.memories)}\n"
            if proj.tasks:
                proj_block += "Tasks:\n"
                for t in proj.tasks:
                    proj_block += f"- {t.id}: {t.name}"
                    if t.description:
                        proj_block += f" — {t.description}"
                    proj_block += "\n"
        logger.info(
            "Injecting %d project(s) for agent '%s': %s",
            len(relevant_projs), agent.id, [p.id for p in relevant_projs],
        )
        system_content += proj_block

    return system_content


def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    workspace: str,
    command_timeout: int = 30,
    config: AppConfig | None = None,
) -> str:
    """Execute a tool call and return the result as a string."""
    if name == "run_command":
        command = arguments.get("command", "")
        logger.info("Tool run_command: %s (workspace=%s)", command, workspace)
        return executor.run_command(command, workspace, timeout=command_timeout)
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
        for k, v in sp.config.items():
            lines.append(f"{k}: {v}")
        if sp.secrets_path:
            lines.append(f"Credentials: source {sp.secrets_path}")
            if sp.secrets:
                lines.append(f"Available vars: {', '.join(sp.secrets.keys())}")
        return "\n".join(lines)
    if name == "list_projects":
        projs = proj_mod.load_all()
        if not projs:
            return "No projects configured."
        return "\n".join(
            f"- {p.id}: {p.name}" + (f" — {p.description}" if p.description else "")
            for p in projs
        )
    if name == "read_project":
        pid = arguments.get("id", "")
        proj = proj_mod.load_one(pid)
        if not proj:
            return f"[error] Project '{pid}' not found."
        lines = [f"# {proj.name}"]
        if proj.description:
            lines.append(proj.description)
        if proj.memories:
            lines.append(f"Memories: {', '.join(proj.memories)}")
        if proj.tasks:
            lines.append("\nTasks:")
            for t in proj.tasks:
                lines.append(f"- {t.id}: {t.name}")
                if t.description:
                    lines.append(f"  {t.description}")
                if t.prompt:
                    lines.append(f"  Prompt: {t.prompt.strip()}")
        return "\n".join(lines)
    if name == "list_agents":
        if not config or not config.agents:
            return "No agents configured."
        lines = []
        for agent_id, agent in config.agents.items():
            # Extract first line of system_prompt as summary
            summary = agent.system_prompt.split("\n")[0].strip() if agent.system_prompt else ""
            # Remove markdown headers
            if summary.startswith("##"):
                summary = summary.lstrip("#").strip()
            lines.append(
                f"- {agent_id}: {agent.name} ({agent.provider}/{agent.model})"
                + (f" — {summary}" if summary else "")
            )
        return "\n".join(lines)
    if name == "read_agent":
        if not config:
            return "[error] Agent configuration not available."
        agent_id = arguments.get("id", "")
        agent = config.agents.get(agent_id)
        if not agent:
            return f"[error] Agent '{agent_id}' not found."
        lines = [f"# {agent.name} ({agent.id})"]
        lines.append(f"Provider: {agent.provider}")
        lines.append(f"Model: {agent.model}")
        lines.append(f"Temperature: {agent.temperature}")
        lines.append(f"Workspace: {agent.workspace}")
        lines.append(f"Max iterations: {agent.max_iterations}")
        if agent.tools:
            lines.append(f"Tools: {', '.join(agent.tools)}")
        lines.append("\n## System Prompt")
        lines.append(agent.system_prompt)
        return "\n".join(lines)
    return f"[error] Unknown tool: {name}"


async def run_agent(
    agent: AgentConfig,
    clients: LLMClients,
    user_messages: list[dict[str, Any]],
    platform_identity: str = "",
    config: AppConfig | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Run the agentic loop for *agent* given an initial list of *user_messages*.

    Returns (final_text_response, full_message_history).
    """
    llm = _resolve_client(agent, clients)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_content(agent, user_messages, platform_identity)},
        *user_messages,
    ]
    tools = (get_tool_schemas(agent.tools)
             + get_tool_schemas(MEMORY_TOOL_NAMES)
             + get_tool_schemas(SUPERPOWER_TOOL_NAMES)
             + get_tool_schemas(PROJECT_TOOL_NAMES)
             + get_tool_schemas(AGENT_TOOL_NAMES))

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
            tool_result = _dispatch_tool(tool_name, tool_args, agent.workspace, agent.command_timeout, config)
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
    platform_identity: str = "",
    config: AppConfig | None = None,
) -> AsyncIterator[str]:
    """
    Run the agentic loop, streaming the final text response token by token.

    Tool-calling iterations are non-streaming; only the last purely-conversational
    response is yielded as a stream.
    """
    llm = _resolve_client(agent, clients)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_content(agent, user_messages, platform_identity)},
        *user_messages,
    ]
    tools = (get_tool_schemas(agent.tools)
             + get_tool_schemas(MEMORY_TOOL_NAMES)
             + get_tool_schemas(SUPERPOWER_TOOL_NAMES)
             + get_tool_schemas(PROJECT_TOOL_NAMES)
             + get_tool_schemas(AGENT_TOOL_NAMES))

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
            tool_result = _dispatch_tool(tool_name, tool_args, agent.workspace, agent.command_timeout, config)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

    yield "I hit the maximum number of steps without completing the task."
