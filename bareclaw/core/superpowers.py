"""
Superpowers system — named external service capabilities with secrets, config, and bootstrap.

Each superpower lives in two files:
  superpowers/<id>.yaml  — non-secret config, keywords, bootstrap prompt (safe to commit)
  secrets/<id>.yaml      — flat key/value secrets (always gitignored)

Relevant superpowers are auto-injected into the system prompt based on keyword matching,
and agents can discover/read them explicitly via list_superpowers / read_superpower tools.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPERPOWERS_DIR = Path(__file__).parent.parent.parent / "superpowers"
SECRETS_DIR     = Path(__file__).parent.parent.parent / "secrets"


@dataclass
class Superpower:
    id: str
    name: str
    description: str = ""
    config: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    bootstrap_prompt: str = ""
    bootstrap_agent: str = ""


def _load_secrets(sp_id: str) -> dict[str, str]:
    """Load secrets/<id>.yaml and return a flat str→str dict. Returns {} if missing."""
    path = SECRETS_DIR / f"{sp_id}.yaml"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return {str(k): str(v) for k, v in data.items() if not str(k).startswith("#")}
    except Exception:
        return {}


def _parse(path: Path) -> Superpower | None:
    try:
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        sp_id = data.get("id", path.stem)
        return Superpower(
            id=sp_id,
            name=data.get("name", sp_id),
            description=data.get("description", ""),
            config={str(k): str(v) for k, v in (data.get("config") or {}).items()},
            secrets=_load_secrets(sp_id),
            keywords=[str(k).lower() for k in data.get("keywords", [])],
            bootstrap_prompt=data.get("bootstrap_prompt", ""),
            bootstrap_agent=data.get("bootstrap_agent", ""),
        )
    except Exception:
        return None


def load_all() -> list[Superpower]:
    """Return all superpowers, excluding example.yaml."""
    if not SUPERPOWERS_DIR.exists():
        return []
    superpowers = []
    for p in sorted(SUPERPOWERS_DIR.glob("*.yaml")):
        if p.stem == "example":
            continue
        sp = _parse(p)
        if sp:
            superpowers.append(sp)
    return superpowers


def load_one(sp_id: str) -> Superpower | None:
    """Load a single superpower by id."""
    path = SUPERPOWERS_DIR / f"{sp_id}.yaml"
    if not path.exists():
        return None
    return _parse(path)


def find_relevant(text: str) -> list[Superpower]:
    """
    Return superpowers whose keywords appear as whole words in *text*.
    Case-insensitive. Preserves file order.
    """
    if not text.strip():
        return []
    text_lower = text.lower()
    results = []
    for sp in load_all():
        for kw in sp.keywords:
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, text_lower):
                results.append(sp)
                break
    return results


def interpolate(template: str, sp: Superpower) -> str:
    """
    Replace {key} placeholders in *template* with values from the superpower's
    merged config + secrets dict. Unknown keys are left as-is.
    """
    variables = {**sp.config, **sp.secrets}
    # Use string.Formatter to substitute only known keys, leaving unknown ones intact
    result = []
    formatter = string.Formatter()
    for literal, field_name, _, _ in formatter.parse(template):
        result.append(literal)
        if field_name is not None:
            result.append(str(variables.get(field_name, "{" + field_name + "}")))
    return "".join(result)
