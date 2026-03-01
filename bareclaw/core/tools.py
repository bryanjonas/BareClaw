"""
Tool registry — defines tools available to agents and generates Ollama tool schemas.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tool schema definitions (Ollama function-calling format)
# ---------------------------------------------------------------------------

# Memory tools are always added to every agent's tool list — no need to
# list them in agent YAML (though you can to make them visible there).
MEMORY_TOOL_NAMES = ["list_memories", "read_memory", "write_memory"]

# Superpower tools are always added to every agent's tool list as well.
SUPERPOWER_TOOL_NAMES = ["list_superpowers", "read_superpower"]

# Project tools are always added to every agent's tool list as well.
PROJECT_TOOL_NAMES = ["list_projects", "read_project"]

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "run_command": {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command in the agent's restricted workspace. "
                "Use this to inspect files, run scripts, check system status, etc. "
                "Only use commands that are safe and relevant to the task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file within the agent's workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file (must be within workspace).",
                    }
                },
                "required": ["path"],
            },
        },
    },
    "list_memories": {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": (
                "List all saved memories with their id, title, and keywords. "
                "Use this to discover what reference knowledge is available before reading a specific memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    "read_memory": {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read the full content of a specific memory by its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The memory id (filename without .yaml).",
                    }
                },
                "required": ["id"],
            },
        },
    },
    "write_memory": {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": (
                "Create or update a memory. Memories persist across conversations and are "
                "auto-injected into future prompts when their keywords match the user's message. "
                "Use this to save useful reference information, API details, credentials, or task notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique identifier (slug, e.g. 'homeassistant-api'). Used as filename.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title shown in memory listings.",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Words or phrases that trigger auto-injection of this memory. "
                            "Use specific, distinctive terms relevant to the topic."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The memory content. Markdown is supported.",
                    },
                },
                "required": ["id", "title", "keywords", "content"],
            },
        },
    },
    "list_superpowers": {
        "type": "function",
        "function": {
            "name": "list_superpowers",
            "description": (
                "List all configured superpowers with their id, name, description, and keywords. "
                "Use this to discover what external service capabilities are available."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    "read_superpower": {
        "type": "function",
        "function": {
            "name": "read_superpower",
            "description": (
                "Read the full configuration (including secrets) for a specific superpower by id. "
                "Returns the service URL, credentials, and any other access details needed to use it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The superpower id (filename without .yaml).",
                    }
                },
                "required": ["id"],
            },
        },
    },
    "list_projects": {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": (
                "List all configured projects with their id, name, description, and keywords. "
                "Use this to discover what workflows and executable tasks are available."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    "read_project": {
        "type": "function",
        "function": {
            "name": "read_project",
            "description": (
                "Read the full details of a specific project by id, including its tasks, "
                "related memories, and runnable prompts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The project id (filename without .yaml).",
                    }
                },
                "required": ["id"],
            },
        },
    },
}


def get_tool_schemas(tool_names: list[str]) -> list[dict[str, Any]]:
    """Return Ollama tool schema objects for the given tool names."""
    return [TOOL_SCHEMAS[name] for name in tool_names if name in TOOL_SCHEMAS]
