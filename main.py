"""
Bareclaw — entry point.

Starts the FastAPI web server, APScheduler cron runner, and (optionally) the
Telegram bot, all running concurrently in a single asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent

# Must be importable before package-level imports resolve relative paths.
sys.path.insert(0, str(ROOT))

from bareclaw import db
from bareclaw.config import load_config
from bareclaw.core.llm import CODEX_SECRETS_FILE, CodexOAuthClient, OllamaClient, OpenAIClient
from bareclaw.scheduler import jobs as scheduler_mod
from bareclaw.telegram import bot as telegram_mod
from bareclaw.web import auth as auth_mod
from bareclaw.web.routes import create_router
from bareclaw.webhooks.handler import create_webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bareclaw")


def build_app(config, clients) -> FastAPI:
    app = FastAPI(title="Bareclaw", docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount(
        "/static",
        StaticFiles(directory=str(ROOT / "bareclaw" / "web" / "static")),
        name="static",
    )
    app.include_router(create_router(config, clients))
    app.include_router(create_webhook_router(config, clients))

    return app


async def main() -> None:
    # ------------------------------------------------------------------ config
    config = load_config(ROOT)
    logger.info("Loaded %d agent(s), %d cron(s), %d webhook(s)",
                len(config.agents), len(config.crons), len(config.webhooks))

    # ------------------------------------------------------------------ DB
    await db.init_db(ROOT / "data" / "bareclaw.db")
    logger.info("Database ready.")

    # ------------------------------------------------------------------ LLM clients
    clients: dict = {}
    for prov in config.providers.values():
        if prov.type == "ollama":
            clients[prov.id] = OllamaClient(base_url=prov.base_url or "http://localhost:11434")
            logger.info("Provider '%s' (ollama) → %s", prov.id, prov.base_url or "http://localhost:11434")
        elif prov.type == "codex":
            from pathlib import Path
            secrets_file = Path(prov.auth_file).expanduser() if prov.auth_file else CODEX_SECRETS_FILE
            clients[prov.id] = CodexOAuthClient(
                secrets_file=secrets_file,
                base_url=prov.base_url or None,
            )
            logger.info("Provider '%s' (codex-oauth) → %s", prov.id, secrets_file)
        else:
            clients[prov.id] = OpenAIClient(
                api_key=prov.api_key or "ignored",
                base_url=prov.base_url or None,
            )
            logger.info("Provider '%s' (openai-compatible)%s", prov.id,
                        f" → {prov.base_url}" if prov.base_url else " → api.openai.com")
    if not clients:
        raise RuntimeError("No providers configured. Add at least one entry under 'providers:' in config.yaml.")

    # ------------------------------------------------------------------ auth
    auth_mod.init_auth(config.api_key)

    # ------------------------------------------------------------------ FastAPI
    app = build_app(config, clients)

    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(server_config)

    # ------------------------------------------------------------------ Crons
    scheduler = scheduler_mod.create_scheduler(config, clients)
    scheduler.start()
    logger.info("Scheduler started with %d job(s).", len(scheduler.get_jobs()))

    # ------------------------------------------------------------------ Telegram
    bot = telegram_mod.create_bot(config, clients)
    if bot:
        # Wire cron notifications to the bot
        scheduler_mod.set_telegram_notifier(telegram_mod.notify)

    # ------------------------------------------------------------------ Run
    tasks = [asyncio.create_task(server.serve())]
    if bot:
        async def run_bot():
            await bot.initialize()
            await bot.start()
            await bot.updater.start_polling(drop_pending_updates=True)
            # Wait until the server task finishes, then stop the bot
            await asyncio.Event().wait()  # runs until cancelled

        tasks.append(asyncio.create_task(run_bot()))

    logger.info("Bareclaw running on http://0.0.0.0:8000")

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        if bot:
            await bot.updater.stop()
            await bot.stop()
            await bot.shutdown()
        logger.info("Bareclaw stopped.")


if __name__ == "__main__":
    asyncio.run(main())
