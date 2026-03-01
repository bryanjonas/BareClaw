"""
Telegram bot — bridges Telegram chats to the agent system.
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bareclaw import db
from bareclaw.config import AppConfig
from bareclaw.core.agent import LLMClients, run_agent

logger = logging.getLogger(__name__)

# Per-chat state: maps chat_id -> { agent_id, conversation }
_sessions: dict[int, dict[str, Any]] = {}
_config: AppConfig | None = None
_clients: LLMClients | None = None
_app: Application | None = None


def _allowed(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    allowed = _config.telegram.allowed_user_ids
    return not allowed or user_id in allowed


def _session(chat_id: int) -> dict[str, Any]:
    if chat_id not in _sessions:
        _sessions[chat_id] = {
            "agent_id": _config.default_agent,
            "conversation": [],
        }
    return _sessions[chat_id]


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text(
        "bareclaw\n\nCommands:\n"
        "/agents — list agents\n"
        "/agent <id> — switch agent\n"
        "/crons — list scheduled jobs\n"
        "/clear — clear conversation history\n\n"
        "Any other message is sent to the active agent."
    )


async def _cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    lines = []
    for a in _config.agents.values():
        tools = ", ".join(a.tools) if a.tools else "none"
        lines.append(f"• {a.id} — {a.name} ({a.model}) tools: {tools}")
    await update.message.reply_text("\n".join(lines) or "No agents configured.")


async def _cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    args = ctx.args
    if not args:
        sess = _session(update.effective_chat.id)
        await update.message.reply_text(f"Current agent: {sess['agent_id']}")
        return
    agent_id = args[0]
    if agent_id not in _config.agents:
        await update.message.reply_text(f"Unknown agent: {agent_id}")
        return
    sess = _session(update.effective_chat.id)
    sess["agent_id"] = agent_id
    sess["conversation"] = []
    await update.message.reply_text(f"Switched to agent: {agent_id}")


async def _cmd_crons(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    lines = []
    for c in _config.crons.values():
        lines.append(f"• {c.id}: {c.schedule} (agent: {c.agent})")
    await update.message.reply_text("\n".join(lines) or "No cron jobs configured.")


async def _cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    _session(update.effective_chat.id)["conversation"] = []
    await update.message.reply_text("Conversation cleared.")


async def _handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    text = update.message.text or ""
    if not text:
        return

    chat_id = update.effective_chat.id
    sess = _session(chat_id)
    agent = _config.agents.get(sess["agent_id"])
    if not agent:
        await update.message.reply_text(f"Agent '{sess['agent_id']}' not found.")
        return

    sess["conversation"].append({"role": "user", "content": text})

    # Show typing indicator
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        response, full_msgs = await run_agent(agent, _clients, list(sess["conversation"]))
    except Exception as exc:
        logger.exception("Telegram agent run failed: %s", exc)
        await update.message.reply_text(f"Error: {exc}")
        return

    sess["conversation"].append({"role": "assistant", "content": response})
    await db.log_chat("telegram", agent.id, sess["conversation"])

    # Telegram max message length is 4096 chars
    for i in range(0, max(1, len(response)), 4096):
        await update.message.reply_text(response[i:i+4096] or "(empty response)")


async def notify(text: str) -> None:
    """Send a notification to all allowed users. Called by the cron scheduler."""
    if not _app or not _config.telegram.allowed_user_ids:
        return
    for uid in _config.telegram.allowed_user_ids:
        try:
            await _app.bot.send_message(chat_id=uid, text=text[:4096])
        except Exception as exc:
            logger.warning("Could not notify user %d: %s", uid, exc)


def create_bot(config: AppConfig, clients: LLMClients) -> Application:
    global _config, _clients, _app

    if not config.telegram.token:
        logger.warning("No Telegram token configured — bot disabled.")
        return None

    _config = config
    _clients = clients

    app = (
        Application.builder()
        .token(config.telegram.token)
        .build()
    )

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_start))
    app.add_handler(CommandHandler("agents", _cmd_agents))
    app.add_handler(CommandHandler("agent", _cmd_agent))
    app.add_handler(CommandHandler("crons", _cmd_crons))
    app.add_handler(CommandHandler("clear", _cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    _app = app
    return app
