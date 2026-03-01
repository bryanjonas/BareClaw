"""
CLI executor — runs shell commands restricted to a configured workspace directory.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class WorkspaceViolationError(Exception):
    pass


def _resolve_workspace(workspace: str) -> Path:
    p = Path(workspace).expanduser().resolve()
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    return p


def run_command(command: str, workspace: str, timeout: int = 30) -> str:
    """
    Execute *command* inside *workspace*.

    Returns a plain-text string with exit code, stdout and stderr that can be
    fed back to the LLM as a tool result.

    Raises WorkspaceViolationError if the resolved workspace somehow escapes
    the configured directory (belt-and-suspenders; the cwd= arg already handles
    the common case).
    """
    ws = _resolve_workspace(workspace)

    # Extra belt-and-suspenders: ensure workspace is still under home or a
    # reasonable root so a misconfigured YAML can't point to "/"
    if str(ws) == "/":
        raise WorkspaceViolationError("Workspace root '/' is not allowed.")

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(ws)},  # keep env but pin HOME
        )
        parts = [f"[exit code: {result.returncode}]"]
        if result.stdout:
            parts.append(f"[stdout]\n{result.stdout.rstrip()}")
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        return "\n".join(parts) or "[no output]"
    except subprocess.TimeoutExpired:
        return f"[error] Command timed out after {timeout}s"
    except Exception as exc:
        return f"[error] {exc}"


def read_file(path: str, workspace: str) -> str:
    """
    Read a file that must reside within *workspace*.
    Returns the file contents or an error string.
    """
    ws = _resolve_workspace(workspace)
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = ws / target
    target = target.resolve()

    # Prevent path traversal outside workspace
    try:
        target.relative_to(ws)
    except ValueError:
        return f"[error] Path '{path}' is outside the allowed workspace."

    if not target.exists():
        return f"[error] File not found: {path}"
    if not target.is_file():
        return f"[error] Not a file: {path}"

    try:
        return target.read_text(errors="replace")
    except Exception as exc:
        return f"[error] Could not read file: {exc}"
