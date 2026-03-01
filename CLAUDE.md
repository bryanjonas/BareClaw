# Bareclaw — CLAUDE.md

Self-hosted AI agent platform. Local Ollama LLM (optional OpenAI) exposed via web UI, Telegram, webhooks, and cron jobs. Single asyncio event loop runs all subsystems concurrently.

## Running

```bash
pip install -r requirements.txt
# Edit config.yaml (set api_key at minimum; Ollama must be running)
python main.py
# Web UI: http://localhost:8000
```

No test suite. Verify changes by running the app and exercising the affected interface.

## Docker

```bash
mkdir -p workspace          # agent CLI sandbox (host-side)
docker compose up -d --build
docker compose logs -f
```

**Ollama URL**: inside the container `localhost` is the container itself. Change `config.yaml`:
```yaml
ollama:
  base_url: http://host.docker.internal:11434
```
`extra_hosts: host.docker.internal:host-gateway` in `docker-compose.yml` makes this work on Linux. Mac/Windows Docker Desktop handles it automatically.

Volumes mounted from host (edit without rebuilding):
- `config.yaml`, `agents/`, `crons/`, `webhooks_config/` — config
- `data/` — SQLite DB (persists across restarts)
- `memories/`, `superpowers/`, `secrets/`, `projects/` — persistent knowledge, credentials, workflows
- `workspace/` → `/root/workspace` — agent CLI sandbox

## Architecture

```
main.py
  └── load_config(ROOT)          # reads config.yaml + agents/ crons/ webhooks_config/
  └── db.init_db()               # SQLite at data/bareclaw.db
  └── OllamaClient / OpenAIClient
  └── build_app(config, clients) # FastAPI + WebSocket chat + dynamic webhook routes
  └── scheduler (APScheduler)    # cron jobs → agent runs → optional Telegram notify
  └── telegram bot (optional)
```

**Agentic loop** (`bareclaw/core/agent.py`): system prompt + messages → LLM → if tool_calls → dispatch → loop; capped by `max_iterations`.

**Two LLM clients** (`bareclaw/core/llm.py`): `OllamaClient` and `OpenAIClient` both normalise to the same canonical message dict. `agent.py` is provider-agnostic; `clients["ollama"]` / `clients["openai"]` resolved from `agent.provider`.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, wires everything |
| `config.yaml` | Global config |
| `bareclaw/config.py` | Dataclass config loader |
| `bareclaw/db.py` | SQLite schema + log helpers |
| `bareclaw/core/llm.py` | OllamaClient + OpenAIClient |
| `bareclaw/core/agent.py` | Agentic loop (`run_agent`, `run_agent_stream`) |
| `bareclaw/core/tools.py` | Tool registry + schemas |
| `bareclaw/executor/cli.py` | `run_command` / `read_file` (workspace-sandboxed) |
| `bareclaw/scheduler/jobs.py` | APScheduler cron dispatch |
| `bareclaw/webhooks/handler.py` | Dynamic webhook route registration |
| `bareclaw/telegram/bot.py` | Telegram bot + per-chat agent sessions |
| `bareclaw/web/routes.py` | FastAPI routes + WebSocket chat |
| `bareclaw/web/auth.py` | API key middleware (Bearer / cookie / WS query param) |

## Config-Driven Entities

All loaded at startup; restart required for changes.

### Agent (`agents/<id>.yaml`)
```yaml
id: my-agent
name: "My Agent"
provider: ollama          # "ollama" | "openai"
model: llama3.2
system_prompt: |
  You are ...
temperature: 0.7
workspace: ~/workspace    # CLI execution sandbox
tools:
  - run_command           # tools defined in bareclaw/core/tools.py
  - read_file
max_iterations: 10
```

### Cron job (`crons/<id>.yaml`)
```yaml
id: my-cron
schedule: "0 * * * *"    # 5-field cron expression
agent: my-agent
command: "df -h"          # optional; output prepended to prompt
prompt: "Analyse the above and alert if ..."
notify_telegram: false
```

### Webhook (`webhooks_config/<id>.yaml`)
```yaml
id: my-webhook
path: /webhooks/my-webhook
secret: ""                # optional HMAC-SHA256 (GitHub-style)
agent: my-agent
prompt_template: |
  Event received:
  {{ body }}
```
Auth: HMAC signature OR `X-API-Key` header. Fire-and-forget background task.

## Memories (`memories/<id>.yaml`)

Persistent knowledge files agents can read/write. Loaded fresh on each agent call (no restart needed).

```yaml
id: homeassistant-api
title: "Home Assistant API"
keywords:
  - homeassistant
  - home assistant
  - hass
content: |
  Base URL: http://homeassistant.local:8123
  ...
```

**Auto-injection**: before each LLM call, `_build_system_content()` in `bareclaw/core/agent.py` keyword-matches the user messages against all memories and appends matching ones to the system prompt under `## Relevant memories`.

**Tools** (always available to all agents — no YAML config needed):
- `list_memories` — returns id + title + keywords for all memories
- `read_memory(id)` — returns full content of one memory
- `write_memory(id, title, keywords, content)` — creates or overwrites a memory file

`memories/example.yaml` is ignored at runtime and is the only memory file committed to git. The `memories/` dir is mounted as a volume in Docker so files persist and are editable on the host.

Core module: `bareclaw/core/memory.py` — `load_all()`, `load_one()`, `find_relevant()`, `save()`

## Superpowers (`superpowers/<id>.yaml` + `secrets/<id>.yaml`)

Named external service capabilities bundling config, secrets, and an optional bootstrap prompt. Loaded fresh on each agent call.

**`superpowers/<id>.yaml`** (safe to commit — contains no secrets):
```yaml
id: homeassistant
name: "Home Assistant"
description: "Local Home Assistant automation hub"
config:
  base_url: "http://homeassistant.local:8123"
keywords:
  - homeassistant
  - home assistant
  - lights
bootstrap_prompt: |
  Explore the Home Assistant API at {base_url} using Bearer {token}.
  Write a memory 'homeassistant-api' with your findings.
bootstrap_agent: default  # optional; defaults to app's default_agent
```

**`secrets/<id>.yaml`** (always gitignored — flat key/value):
```yaml
token: "your-token-here"
```
The filename must match the superpower `id`. Consider `chmod 600 secrets/<id>.yaml`.

**Auto-injection**: `_build_system_content()` in `bareclaw/core/agent.py` keyword-matches user messages against all superpowers and appends matching ones (including secrets) to the system prompt under `## Available superpowers`.

**Bootstrap**: clicking Bootstrap in the `/superpowers` UI POSTs to `/api/superpowers/{id}/bootstrap`. The server interpolates `{key}` placeholders in `bootstrap_prompt` with merged config+secrets values, then runs the bootstrap agent. The agent typically uses `run_command` (curl) + `write_memory` to document findings.

**Tools** (always available to all agents — no YAML config needed):
- `list_superpowers` — returns id, name, description, keywords for all superpowers
- `read_superpower(id)` — returns full config + secrets so the agent can use them

`superpowers/example.yaml` and `secrets/example.yaml` are committed to git; all other files in both dirs are gitignored. Both dirs are mounted as volumes in Docker.

Core module: `bareclaw/core/superpowers.py` — `load_all()`, `load_one()`, `find_relevant()`, `_load_secrets()`, `interpolate()`

## Projects (`projects/<id>.yaml`)

Multi-component workflows the agent has explored and can execute. Each project defines named **tasks** — runnable prompts triggerable from the `/projects` UI or by agents via tools. Loaded fresh on each agent call (no restart needed). Safe to commit (no secrets).

```yaml
id: home-network-security
name: "Home Network Security"
description: "Packet capture pipeline and security dashboard"
keywords:
  - packet capture
  - pcap
  - network security
agent: default              # default agent for tasks; falls back to config.default_agent
memories:                   # related memory IDs shown in UI and injected into system prompt
  - home-network-architecture
  - pcap-pipeline-process
tasks:
  - id: run-pipeline
    name: "Run Pipeline"
    description: "Copy latest pcaps and run analysis containers"
    prompt: |
      Copy the latest pcap files from the router and run the analysis pipeline.
  - id: check-dashboard
    name: "Check Dashboard"
    description: "Review dashboard for anomalies in the last 24h"
    prompt: |
      Check the security dashboard for anomalies in the last 24 hours.
    agent: ""               # optional per-task agent override
```

**Auto-injection**: `_build_system_content()` keyword-matches user messages against all projects and appends matching ones under `## Relevant projects`, including task summaries and referenced memory IDs.

**Task execution**: clicking Run in the `/projects` UI POSTs to `/api/projects/{id}/tasks/{task_id}/run`. Agent resolved as `task.agent → project.agent → config.default_agent`.

**Tools** (always available to all agents — no YAML config needed):
- `list_projects` — returns id, name, description for all projects
- `read_project(id)` — returns full project details including tasks and prompts

`projects/example.yaml` is the only project file committed to git; all others are gitignored.

Core module: `bareclaw/core/projects.py` — `load_all()`, `load_one()`, `find_relevant()`

## Adding a New Tool

1. Implement the tool function in `bareclaw/executor/cli.py` (return a string).
2. Add the tool schema to `TOOL_SCHEMAS` in `bareclaw/core/tools.py`.
3. Add dispatch in `_dispatch_tool()` in `bareclaw/core/agent.py`.
4. List the tool name in the agent YAML under `tools:`.

## Canonical Message Format

Internal representation used across the codebase:

```python
{"role": "system",    "content": "..."}
{"role": "user",      "content": "..."}
{"role": "assistant", "content": "...", "tool_calls": [{"id": "...", "function": {"name": "...", "arguments": {...}}}]}
{"role": "tool",      "tool_call_id": "...", "content": "..."}
```

- `OllamaClient` strips `tool_call_id` / `type` before sending; generates UUIDs for tool call IDs (Ollama omits them).
- `OpenAIClient` serialises `arguments` to JSON string; adds `type: "function"`.

## Code Conventions

- Python 3.11+, type hints throughout (`from __future__ import annotations`).
- `async`/`await` everywhere (FastAPI, aiosqlite, Ollama, OpenAI, APScheduler).
- Dataclasses (not Pydantic) for all config types.
- Standard `logging` module; logger per module via `logging.getLogger(__name__)`.
- Server-rendered Jinja2 templates + vanilla JS. No React, no HTMX.
- YAML for static config, SQLite (`data/bareclaw.db`) for runtime state.
- No environment variables — everything in `config.yaml`.

## Security Notes

- Workspace restriction: `executor/cli.py` resolves paths and checks `target.relative_to(workspace)`; raises on traversal.
- `HOME` is pinned to workspace for subprocess execution.
- API key auth is a shared secret — change the default `"changeme"` in `config.yaml`.
- Webhook HMAC uses `hmac.compare_digest` (constant-time).
- No automatic user input sanitisation beyond workspace sandboxing — do not expose to untrusted networks without review.
