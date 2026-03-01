"""
Webhook handler — registers dynamic POST routes and dispatches to agents.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from bareclaw import db
from bareclaw.config import AppConfig, WebhookConfig
from bareclaw.core.agent import LLMClients, run_agent

logger = logging.getLogger(__name__)


def _verify_hmac(secret: str, body: bytes, signature_header: str) -> bool:
    """Verify GitHub-style HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _render_template(template: str, body: str) -> str:
    """Simple {{ body }} substitution — keeps things dependency-free."""
    return template.replace("{{ body }}", body).replace("{{body}}", body)


async def _handle_webhook(
    wh: WebhookConfig,
    config: AppConfig,
    clients: LLMClients,
    raw_body: bytes,
) -> None:
    body_str = raw_body.decode(errors="replace")
    try:
        body_display = json.dumps(json.loads(body_str), indent=2)
    except Exception:
        body_display = body_str

    prompt = _render_template(wh.prompt_template, body_display)

    agent = config.agents.get(wh.agent)
    if not agent:
        logger.error("Webhook %s references unknown agent %s", wh.id, wh.agent)
        await db.log_webhook_run(wh.id, body_display, f"[error] Agent '{wh.agent}' not found.")
        return

    try:
        response, _ = await run_agent(agent, clients, [{"role": "user", "content": prompt}])
    except Exception as exc:
        logger.exception("Webhook %s agent run failed: %s", wh.id, exc)
        response = f"[error] {exc}"

    await db.log_webhook_run(wh.id, body_display, response)


def create_webhook_router(config: AppConfig, clients: LLMClients) -> APIRouter:
    router = APIRouter()

    for wh in config.webhooks.values():
        # Capture loop variable in closure
        def make_handler(webhook: WebhookConfig):
            async def handler(request: Request, background_tasks: BackgroundTasks):
                raw_body = await request.body()

                # HMAC verification
                if webhook.secret:
                    sig = request.headers.get("X-Hub-Signature-256", "")
                    if not sig:
                        sig = request.headers.get("X-Signature", "")
                    if not _verify_hmac(webhook.secret, raw_body, sig):
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid webhook signature",
                        )
                else:
                    # Fall back to API key auth
                    api_key = request.headers.get("X-API-Key", "")
                    if api_key != config.api_key:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid API key",
                        )

                # Fire-and-forget — respond immediately
                background_tasks.add_task(
                    _handle_webhook, webhook, config, clients, raw_body
                )
                return {"status": "accepted"}

            return handler

        router.add_api_route(
            path=wh.path,
            endpoint=make_handler(wh),
            methods=["POST"],
            name=f"webhook_{wh.id}",
        )
        logger.info("Registered webhook: POST %s -> agent '%s'", wh.path, wh.agent)

    return router
