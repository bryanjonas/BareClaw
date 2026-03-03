"""
Config loader — reads config.yaml plus all agent/cron/webhook YAML definitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    id: str
    type: str = "ollama"   # "ollama" | "openai"  (openai covers any OpenAI-compatible API)
    base_url: str = ""
    api_key: str = ""


@dataclass
class TelegramConfig:
    token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class AgentConfig:
    id: str = ""
    name: str = ""
    model: str = "llama3.2"
    provider: str = "ollama"   # references a provider id from config.providers
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    workspace: str = ""
    tools: list[str] = field(default_factory=list)
    max_iterations: int = 10  # safety cap on agentic loop


@dataclass
class CronConfig:
    id: str = ""
    schedule: str = ""
    agent: str = "default"
    command: str = ""        # optional shell command whose output feeds the LLM
    prompt: str = ""
    notify_telegram: bool = False


@dataclass
class WebhookConfig:
    id: str = ""
    path: str = ""
    secret: str = ""         # optional HMAC-SHA256 secret
    agent: str = "default"
    prompt_template: str = "An external event was received:\n{{ body }}"


@dataclass
class AppConfig:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    api_key: str = "changeme"
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    default_agent: str = "default"
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    crons: dict[str, CronConfig] = field(default_factory=dict)
    webhooks: dict[str, WebhookConfig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_providers(raw: dict[str, Any]) -> dict[str, ProviderConfig]:
    """
    Parse providers from config.yaml.

    Supports two formats:

    New (providers map):
        providers:
          ollama:
            type: ollama
            base_url: http://localhost:11434
          lm-studio:
            type: openai
            base_url: http://localhost:1234/v1
            api_key: lm-studio

    Legacy (separate ollama/openai keys — auto-migrated):
        ollama:
          base_url: http://localhost:11434
        openai:
          api_key: sk-...
          base_url: ""
    """
    if "providers" in raw:
        providers: dict[str, ProviderConfig] = {}
        for pid, pdata in (raw["providers"] or {}).items():
            pdata = pdata or {}
            providers[pid] = ProviderConfig(
                id=pid,
                type=pdata.get("type", "ollama"),
                base_url=pdata.get("base_url", ""),
                api_key=pdata.get("api_key", ""),
            )
        return providers

    # Legacy format — synthesise from ollama: and openai: keys
    providers = {}
    ollama_raw = raw.get("ollama", {}) or {}
    providers["ollama"] = ProviderConfig(
        id="ollama",
        type="ollama",
        base_url=ollama_raw.get("base_url", "http://localhost:11434"),
    )
    openai_raw = raw.get("openai", {}) or {}
    if openai_raw.get("api_key") or openai_raw.get("base_url"):
        providers["openai"] = ProviderConfig(
            id="openai",
            type="openai",
            api_key=openai_raw.get("api_key", ""),
            base_url=openai_raw.get("base_url", ""),
        )
    return providers


def _load_agents(agents_dir: Path) -> dict[str, AgentConfig]:
    agents: dict[str, AgentConfig] = {}
    if not agents_dir.exists():
        return agents
    for p in agents_dir.glob("*.yaml"):
        if p.stem == "example":
            continue
        data = _load_yaml(p)
        agent = AgentConfig(
            id=data.get("id", p.stem),
            name=data.get("name", p.stem),
            model=data.get("model", "llama3.2"),
            provider=data.get("provider", "ollama"),
            system_prompt=data.get("system_prompt", "You are a helpful assistant."),
            temperature=float(data.get("temperature", 0.7)),
            workspace=data.get("workspace", str(Path.home())),
            tools=data.get("tools", []),
            max_iterations=int(data.get("max_iterations", 10)),
        )
        agents[agent.id] = agent
    return agents


def _load_crons(crons_dir: Path) -> dict[str, CronConfig]:
    crons: dict[str, CronConfig] = {}
    if not crons_dir.exists():
        return crons
    for p in crons_dir.glob("*.yaml"):
        if p.stem == "example":
            continue
        data = _load_yaml(p)
        cron = CronConfig(
            id=data.get("id", p.stem),
            schedule=data.get("schedule", ""),
            agent=data.get("agent", "default"),
            command=data.get("command", ""),
            prompt=data.get("prompt", ""),
            notify_telegram=bool(data.get("notify_telegram", False)),
        )
        crons[cron.id] = cron
    return crons


def _load_webhooks(webhooks_dir: Path) -> dict[str, WebhookConfig]:
    webhooks: dict[str, WebhookConfig] = {}
    if not webhooks_dir.exists():
        return webhooks
    for p in webhooks_dir.glob("*.yaml"):
        if p.stem == "example":
            continue
        data = _load_yaml(p)
        wh = WebhookConfig(
            id=data.get("id", p.stem),
            path=data.get("path", f"/webhooks/{p.stem}"),
            secret=data.get("secret", ""),
            agent=data.get("agent", "default"),
            prompt_template=data.get(
                "prompt_template",
                "An external event was received:\n{{ body }}",
            ),
        )
        webhooks[wh.id] = wh
    return webhooks


def load_config(root: Path = ROOT) -> AppConfig:
    config_path = root / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")

    raw = _load_yaml(config_path)
    telegram_raw = raw.get("telegram", {}) or {}

    cfg = AppConfig(
        providers=_load_providers(raw),
        api_key=raw.get("api_key", "changeme"),
        telegram=TelegramConfig(
            token=telegram_raw.get("token", ""),
            allowed_user_ids=telegram_raw.get("allowed_user_ids", []),
        ),
        default_agent=raw.get("default_agent", "default"),
    )

    cfg.agents = _load_agents(root / "agents")
    cfg.crons = _load_crons(root / "crons")
    cfg.webhooks = _load_webhooks(root / "webhooks_config")

    return cfg
