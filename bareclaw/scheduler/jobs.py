"""
Cron scheduler — loads cron YAML definitions and dispatches agent runs on schedule.
"""
from __future__ import annotations

import logging
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bareclaw import db
from bareclaw.config import AppConfig, CronConfig
from bareclaw.core.agent import LLMClients, run_agent
from bareclaw.executor.cli import run_command

logger = logging.getLogger(__name__)

# Telegram notification callback — set by main.py when bot is available
_telegram_notify: Callable[[str], None] | None = None


def set_telegram_notifier(fn: Callable[[str], None]) -> None:
    global _telegram_notify
    _telegram_notify = fn


async def _run_cron_job(job: CronConfig, config: AppConfig, clients: LLMClients) -> None:
    logger.info("Running cron job: %s", job.id)
    command_output: str | None = None

    # Step 1: optionally run the shell command
    if job.command:
        agent = config.agents.get(job.agent)
        workspace = agent.workspace if agent else "~"
        command_output = run_command(job.command, workspace)
        logger.debug("Cron %s command output: %s", job.id, command_output[:200])

    # Step 2: build prompt
    prompt = job.prompt
    if command_output:
        prompt = f"Command output:\n```\n{command_output}\n```\n\n{prompt}"

    # Step 3: run agent
    agent = config.agents.get(job.agent)
    if not agent:
        logger.error("Cron job %s references unknown agent %s", job.id, job.agent)
        await db.log_cron_run(
            job.id, command_output,
            f"[error] Agent '{job.agent}' not found.", "error"
        )
        return

    try:
        response, _ = await run_agent(agent, clients, [{"role": "user", "content": prompt}])
        status = "ok"
    except Exception as exc:
        logger.exception("Cron job %s failed: %s", job.id, exc)
        response = f"[error] {exc}"
        status = "error"

    # Step 4: persist
    await db.log_cron_run(job.id, command_output, response, status)

    # Step 5: optional Telegram notification
    if job.notify_telegram and _telegram_notify:
        summary = f"[cron: {job.id}]\n{response[:1000]}"
        try:
            await _telegram_notify(summary)
        except Exception as exc:
            logger.warning("Telegram notify failed: %s", exc)


def _parse_cron_expression(expr: str) -> CronTrigger:
    """Parse a standard 5-field cron expression into an APScheduler trigger."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: '{expr}' — expected 5 fields")
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


def create_scheduler(config: AppConfig, clients: LLMClients) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    for job in config.crons.values():
        if not job.schedule:
            logger.warning("Cron job %s has no schedule — skipping", job.id)
            continue
        try:
            trigger = _parse_cron_expression(job.schedule)
        except ValueError as exc:
            logger.error("Cron job %s: %s", job.id, exc)
            continue

        scheduler.add_job(
            _run_cron_job,
            trigger=trigger,
            id=job.id,
            name=job.id,
            kwargs={"job": job, "config": config, "clients": clients},
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info("Scheduled cron job '%s' with expression: %s", job.id, job.schedule)

    return scheduler
