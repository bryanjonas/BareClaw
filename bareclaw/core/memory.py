"""
Memory system — persistent YAML files that agents can read, write, and auto-retrieve.

Each file in memories/ is a structured note with keywords used for relevance matching.
Agents always have access to three tools (list_memories, read_memory, write_memory) and
relevant memories are auto-injected into the system prompt based on keyword matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MEMORIES_DIR = Path(__file__).parent.parent.parent / "memories"


@dataclass
class Memory:
    id: str
    title: str
    keywords: list[str] = field(default_factory=list)
    content: str = ""


def _parse(path: Path) -> Memory | None:
    try:
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return Memory(
            id=data.get("id", path.stem),
            title=data.get("title", path.stem),
            keywords=[str(k).lower() for k in data.get("keywords", [])],
            content=data.get("content", ""),
        )
    except Exception:
        return None


def load_all() -> list[Memory]:
    """Return all memories, excluding example.yaml."""
    if not MEMORIES_DIR.exists():
        return []
    memories = []
    for p in sorted(MEMORIES_DIR.glob("*.yaml")):
        if p.stem == "example":
            continue
        m = _parse(p)
        if m:
            memories.append(m)
    return memories


def load_one(memory_id: str) -> Memory | None:
    """Load a single memory by id."""
    path = MEMORIES_DIR / f"{memory_id}.yaml"
    if not path.exists():
        return None
    return _parse(path)


def find_relevant(text: str) -> list[Memory]:
    """
    Return memories whose keywords appear as whole words in *text*.
    Case-insensitive. Preserves file order.
    """
    if not text.strip():
        return []
    text_lower = text.lower()
    results = []
    for m in load_all():
        for kw in m.keywords:
            # Match keyword as a whole word (handles multi-word keywords too)
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, text_lower):
                results.append(m)
                break
    return results


def save(memory_id: str, title: str, keywords: list[str], content: str) -> None:
    """Write or overwrite a memory file."""
    MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": memory_id,
        "title": title,
        "keywords": keywords,
        "content": content,
    }
    path = MEMORIES_DIR / f"{memory_id}.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
