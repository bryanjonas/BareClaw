# bareclaw

<img src="logo/bareclaw-vector.svg" alt="bareclaw" height="60">

A self-hosted AI agent platform that connects to multiple LLM providers through a unified interface: web chat UI, Telegram bot, HTTP webhooks, and deterministic scheduled jobs. Agents can autonomously run CLI commands in a restricted workspace.

## Features

- **Agents** — Define agents in YAML with their own model, system prompt, tools, and workspace
- **Cron jobs** — Deterministic schedules that trigger either a project task or an explicit command
- **Webhooks** — Register HTTP POST endpoints for external services to trigger agent interactions
- **Telegram bot** — Chat with any agent via Telegram; receive cron notifications
- **Web UI** — Browser-based chat with streaming responses, plus dashboards for crons and webhooks
- **CLI tool calling** — Agents can autonomously run shell commands (restricted to their workspace)
- **Multi-provider LLM** — Pluggable provider system; run local models or connect to external APIs, per agent
- **Memories** — Persistent knowledge files that are keyword-matched and auto-injected into agent system prompts; agents can read and write them via tools
- **Superpowers** — Named external service bundles (config + secrets) keyword-injected into agent prompts; includes a "Bootstrap Memory" button to auto-document APIs
- **Projects** — Multi-component workflows with runnable tasks; includes a "Bootstrap Runbook" button that executes tasks and documents operational knowledge; project memories are automatically injected into task context

## Requirements

- Python 3.11+
- At least one configured LLM provider (e.g. a local [Ollama](https://ollama.com) instance)

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Edit config
#    Set your api_key, Ollama URL, and optionally Telegram token
nano config.yaml

# 3. (Optional) Create a workspace directory for agent CLI execution
mkdir -p ~/workspace
```

## Deployment

### Systemd (bare-metal)

Runs as a systemd user service — no sudo required after initial setup. Agents have direct access to the host filesystem within their configured workspace.

```bash
# Install the unit file
mkdir -p ~/.config/systemd/user
cp bareclaw.service ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now bareclaw

# View logs
journalctl --user -u bareclaw -f
```

To keep the service running at boot without an active login session, a sudoer must run once:

```bash
sudo loginctl enable-linger bareclaw
```

### Docker

Easy to spin up, but agent file access is limited to volumes explicitly mounted into the container. By default only `workspace/` is available to agents — to give agents access to other host paths, add volume mounts to `docker-compose.yml` and update the agent's `workspace:` in its YAML.

```bash
mkdir -p workspace          # agent CLI sandbox (host-side)
docker compose up -d --build
docker compose logs -f
```

**Ollama URL**: inside the container `localhost` is the container itself. Set in `config.yaml`:
```yaml
providers:
  ollama:
    type: ollama
    base_url: http://host.docker.internal:11434
```
`extra_hosts: host.docker.internal:host-gateway` in `docker-compose.yml` enables this on Linux. Mac/Windows Docker Desktop handles it automatically.

Volumes mounted from host (edit without rebuilding):
- `config.yaml`, `agents/`, `crons/`, `webhooks_config/` — config
- `data/` — SQLite DB (persists across restarts)
- `memories/`, `superpowers/`, `secrets/`, `projects/` — persistent knowledge, credentials, workflows
- `workspace/` → `/root/workspace` — agent CLI sandbox

Open http://localhost:8000 and log in with your API key.

## Configuration

### `config.yaml`

```yaml
# Named providers — add as many as you need.
# type "ollama"  uses the Ollama SDK.
# type "openai"  works with OpenAI, LM Studio, vLLM, llama.cpp, OpenRouter, Groq, etc.
providers:
  ollama:
    type: ollama
    base_url: http://localhost:11434
  openai:
    type: openai
    api_key: ""        # set to enable real OpenAI
    base_url: ""       # optional: override for any OpenAI-compatible endpoint
  # openrouter:
  #   type: openai
  #   base_url: https://openrouter.ai/api/v1
  #   # api_key loaded from secrets/openrouter.env
  # lm-studio:
  #   type: openai
  #   base_url: http://localhost:1234/v1
  #   api_key: lm-studio

api_key: "changeme"        # Web UI + webhook auth

telegram:
  token: ""                # BotFather token
  allowed_user_ids: []     # Your Telegram numeric user ID

default_agent: default
```

Agents reference providers by id (`provider: ollama`, `provider: lm-studio`, etc.). The legacy single-provider format (`ollama:` / `openai:` top-level keys) is still accepted for backwards compatibility.

### Config file conventions

Each config directory (`agents/`, `crons/`, `webhooks_config/`, `memories/`, `superpowers/`) contains an `example.yaml` that documents all available fields. These example files are:

- **Ignored at runtime** — never loaded as real agents, crons, or webhooks
- **The only YAML files committed to git** — your real configs are gitignored (as is `config.yaml`)

To add a real config, copy the relevant `example.yaml`, rename it, and fill in your values:

```bash
cp agents/example.yaml agents/my-agent.yaml
```

### Agents (`agents/*.yaml`)

```yaml
id: default
name: "Default Assistant"
provider: ollama           # LLM provider; more coming
model: llama3.2
ollama_base_url: ""        # Optional: override global ollama.base_url for this agent
system_prompt: |
  You are a helpful assistant.
temperature: 0.7
workspace: ~/workspace     # CLI commands confined here
tools:
  - run_command
  - read_file
max_iterations: 10
```

**Available tools:** `run_command`, `read_file`

### Cron jobs (`crons/*.yaml`)

```yaml
id: disk-check
schedule: "0 * * * *"     # Standard 5-field cron expression
project: ops
task: check-disk
notify_telegram: true
```

Exactly one target must be defined per cron job:

- `project` + `task` for a scheduled project task
- `command` for an explicit shell command, optionally with `workspace` and `timeout`

Example command cron:

```yaml
id: disk-check
schedule: "0 * * * *"
command: "df -h"
workspace: ~/workspace
timeout: 30
notify_telegram: true
```

Project-task crons stay deterministic because the scheduler resolves `project` + `task` directly in Python and runs that task's prompt with agent resolution `task.agent → project.agent → config.default_agent`. Command crons run the configured shell command directly in the specified workspace. Cron jobs do not self-call the HTTP API.

### Webhooks (`webhooks_config/*.yaml`)

```yaml
id: my-webhook
path: /webhooks/my-webhook
secret: ""                 # Optional HMAC-SHA256 secret (GitHub-style)
agent: default
prompt_template: |
  An event was received:
  {{ body }}
  Summarize what happened.
```

Call it:
```bash
curl -X POST http://localhost:8000/webhooks/my-webhook \
  -H "X-API-Key: changeme" \
  -H "Content-Type: application/json" \
  -d '{"event": "test"}'
```

### Memories (`memories/*.yaml`)

Persistent knowledge files agents can read and write. On each agent call, memories whose keywords match the conversation are automatically appended to the system prompt under `## Relevant memories`. No restart needed — files are loaded fresh each call.

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

**Memory tools** (available to all agents, no YAML config needed):

| Tool | Description |
|---|---|
| `list_memories` | Returns id, title, and keywords for all memories |
| `read_memory(id)` | Returns the full content of one memory |
| `write_memory(id, title, keywords, content)` | Creates or overwrites a memory file |

### Superpowers (`superpowers/*.yaml` + `secrets/*.yaml`)

Named external service capabilities bundling config, secrets, and an optional bootstrap prompt. On each agent call, superpowers whose keywords match the conversation are automatically appended to the system prompt under `## Available superpowers`.

**`superpowers/<id>.yaml`** (safe to commit — no secrets):
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

**`secrets/<id>.yaml`** (always gitignored — flat key/value; filename must match superpower id):
```yaml
token: "your-token-here"
```

Consider `chmod 600 secrets/<id>.yaml`. Placeholders like `{token}` in `bootstrap_prompt` are interpolated from the merged config + secrets at bootstrap time.

**Superpower tools** (available to all agents, no YAML config needed):

| Tool | Description |
|---|---|
| `list_superpowers` | Returns id, name, description, and keywords for all superpowers |
| `read_superpower(id)` | Returns full config + secrets for the agent to use |

**Bootstrap**: clicking "Bootstrap Memory" on the `/superpowers` page runs the configured agent with the interpolated `bootstrap_prompt`. The agent typically uses `run_command` (curl) and `write_memory` to document the external API into memories. The button uses the `bootstrap_agent` (or falls back to the default agent).

### Projects (`projects/*.yaml`)

Multi-component workflows the agent has explored and can execute. Each project defines named **tasks** — runnable prompts that can be triggered from the `/projects` UI and scheduled by cron jobs. On each agent call, projects whose keywords match the conversation are automatically appended to the system prompt under `## Relevant projects`.

**When a task runs, all memories listed in `memories:` are automatically injected into the task's context** — no need for the agent to explicitly call `read_memory()`.

```yaml
id: home-network-security
name: "Home Network Security"
description: "Packet capture pipeline and security dashboard"
keywords:
  - packet capture
  - pcap
  - network security
agent: default              # default agent; falls back to config.default_agent
memories:                   # auto-injected into task context when tasks run
  - home-network-security-runbook
  - home-network-troubleshooting
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
bootstrap_prompt: |
  You are bootstrapping the project "{name}" (ID: {id}).

  Description: {description}

  Available tasks: {tasks}

  Execute each task and document operational knowledge gained.
  Create a memory called '{id}-runbook' with execution flow, file locations,
  dependencies, timing, and troubleshooting tips.
bootstrap_agent: ""         # optional; uses project.agent or config.default_agent
```

Projects contain no secrets and are the canonical home for reusable operational prompts. Cron jobs reference these tasks by id instead of embedding their own prompts. Agent resolution: `task.agent → project.agent → config.default_agent`.

**Bootstrap**: clicking "Bootstrap Runbook" on the `/projects` page runs the configured agent with the interpolated `bootstrap_prompt` (placeholders: `{id}`, `{name}`, `{description}`, `{tasks}`, `{memories}`). The agent typically executes the project's tasks and uses `write_memory` to create an operational runbook. The button disappears once the `{id}-runbook` memory exists.

**Project tools** (available to all agents, no YAML config needed):

| Tool | Description |
|---|---|
| `list_projects` | Returns id, name, description for all projects |
| `read_project(id)` | Returns full project details including tasks and prompts |

## Web UI Routes

| Route | Description |
|---|---|
| `GET /` | Chat interface |
| `GET /superpowers` | Superpower cards (config, secrets masked, bootstrap) |
| `GET /projects` | Project cards with per-task Run buttons |
| `GET /memories` | Memory browser |
| `GET /crons` | Cron job list + run history |
| `GET /webhooks` | Webhook list + call history |
| `WS /ws/chat?token=<key>` | WebSocket chat endpoint |

## Telegram Commands

| Command | Description |
|---|---|
| `/start` or `/help` | Show available commands |
| `/agents` | List configured agents |
| `/agent <id>` | Switch active agent |
| `/crons` | List cron jobs |
| `/clear` | Clear conversation history |
