"""
Projects system — multi-component workflows that agents have explored and can execute.

Each file in projects/ defines a project with keywords for auto-injection and a list
of named tasks (runnable prompts) that can be triggered from the UI or by agents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECTS_DIR = Path(__file__).parent.parent.parent / "projects"


@dataclass
class ProjectTask:
    id: str
    name: str
    description: str = ""
    prompt: str = ""
    agent: str = ""  # overrides project-level agent when set


@dataclass
class Project:
    id: str
    name: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    agent: str = ""  # default agent for tasks; falls back to config.default_agent
    memories: list[str] = field(default_factory=list)
    tasks: list[ProjectTask] = field(default_factory=list)


def _parse(path: Path) -> Project | None:
    try:
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        tasks = [
            ProjectTask(
                id=str(t.get("id", "")),
                name=str(t.get("name", t.get("id", ""))),
                description=str(t.get("description", "")),
                prompt=str(t.get("prompt", "")),
                agent=str(t.get("agent", "")),
            )
            for t in data.get("tasks", [])
        ]
        return Project(
            id=data.get("id", path.stem),
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            keywords=[str(k).lower() for k in data.get("keywords", [])],
            agent=data.get("agent", ""),
            memories=data.get("memories", []),
            tasks=tasks,
        )
    except Exception:
        return None


def load_all() -> list[Project]:
    """Return all projects, excluding example.yaml."""
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for p in sorted(PROJECTS_DIR.glob("*.yaml")):
        if p.stem == "example":
            continue
        proj = _parse(p)
        if proj:
            projects.append(proj)
    return projects


def load_one(project_id: str) -> Project | None:
    """Load a single project by id."""
    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return None
    return _parse(path)


def find_relevant(text: str) -> list[Project]:
    """
    Return projects whose keywords appear as whole words in *text*.
    Case-insensitive. Preserves file order.
    """
    if not text.strip():
        return []
    text_lower = text.lower()
    results = []
    for proj in load_all():
        for kw in proj.keywords:
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, text_lower):
                results.append(proj)
                break
    return results
