"""
SQLite database initialisation and helpers using aiosqlite.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

_DB_PATH: Path | None = None


def set_db_path(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path


def get_db_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("DB path not initialised — call set_db_path() first")
    return _DB_PATH


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS cron_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT    NOT NULL,
    ran_at      TEXT    NOT NULL,
    command_output TEXT,
    llm_response   TEXT,
    status         TEXT DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS webhook_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id   TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    payload      TEXT,
    llm_response TEXT
);

CREATE TABLE IF NOT EXISTS chat_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    messages   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


async def init_db(path: Path) -> None:
    set_db_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def log_cron_run(
    job_id: str,
    command_output: str | None,
    llm_response: str,
    status: str = "ok",
) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO cron_runs (job_id, ran_at, command_output, llm_response, status) VALUES (?,?,?,?,?)",
            (job_id, datetime.utcnow().isoformat(), command_output, llm_response, status),
        )
        await db.commit()


async def log_webhook_run(webhook_id: str, payload: Any, llm_response: str) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO webhook_runs (webhook_id, received_at, payload, llm_response) VALUES (?,?,?,?)",
            (
                webhook_id,
                datetime.utcnow().isoformat(),
                json.dumps(payload) if not isinstance(payload, str) else payload,
                llm_response,
            ),
        )
        await db.commit()


async def log_chat(source: str, agent_id: str, messages: list[dict]) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO chat_logs (source, agent_id, messages, created_at) VALUES (?,?,?,?)",
            (source, agent_id, json.dumps(messages), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def fetch_cron_runs(job_id: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        if job_id:
            cur = await db.execute(
                "SELECT * FROM cron_runs WHERE job_id=? ORDER BY ran_at DESC LIMIT ?",
                (job_id, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM cron_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def fetch_webhook_runs(webhook_id: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        if webhook_id:
            cur = await db.execute(
                "SELECT * FROM webhook_runs WHERE webhook_id=? ORDER BY received_at DESC LIMIT ?",
                (webhook_id, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM webhook_runs ORDER BY received_at DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
