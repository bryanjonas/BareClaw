from __future__ import annotations

from bareclaw.config import AppConfig
from bareclaw.core import memory as mem_mod
from bareclaw.core import projects as proj_mod
from bareclaw.core.agent import LLMClients, run_agent


class TaskRunError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


async def run_project_task(
    project_id: str,
    task_id: str,
    config: AppConfig,
    clients: LLMClients,
) -> str:
    project, task = proj_mod.load_task(project_id, task_id)
    if not project:
        raise TaskRunError(f"Project '{project_id}' not found", 404)
    if not task:
        raise TaskRunError(f"Task '{task_id}' not found", 404)
    if not task.prompt:
        raise TaskRunError("Task has no prompt defined", 400)

    agent_id = task.agent or project.agent or config.default_agent
    agent = config.agents.get(agent_id)
    if not agent:
        raise TaskRunError(f"Agent '{agent_id}' not found", 404)

    # Build enhanced prompt with project memories injected
    prompt_parts = []

    # Add project context header
    prompt_parts.append(f"[Project: {project.name}]")
    if project.description:
        prompt_parts.append(f"{project.description}")

    # Load and inject project memories
    if project.memories:
        loaded_memories = []
        for mem_id in project.memories:
            memory = mem_mod.load_one(mem_id)
            if memory:
                loaded_memories.append(memory)

        if loaded_memories:
            prompt_parts.append("\n## Project Knowledge")
            for mem in loaded_memories:
                prompt_parts.append(f"\n### {mem.title}\n{mem.content}")

    # Add the actual task prompt
    prompt_parts.append(f"\n## Task: {task.name}")
    prompt_parts.append(task.prompt)

    enhanced_prompt = "\n".join(prompt_parts)

    response, _ = await run_agent(agent, clients, [{"role": "user", "content": enhanced_prompt}], config.platform_identity, config)
    return response
