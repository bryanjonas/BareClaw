"""
FastAPI routes — web UI, WebSocket chat, login, and data API endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from bareclaw import db
from bareclaw.config import AppConfig
from bareclaw.core import memory as mem_mod
from bareclaw.core import projects as proj_mod
from bareclaw.core import superpowers as sp_mod
from bareclaw.core.agent import LLMClients, run_agent, run_agent_stream
from bareclaw.core.task_runner import TaskRunError, run_project_task
from bareclaw.web.auth import RequireAuth, _is_valid

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_config: AppConfig | None = None
_clients: LLMClients | None = None
_codex_flow_task: asyncio.Task | None = None
_codex_flow_result: dict | None = None
_codex_flow_verifier: str | None = None
_codex_flow_state: str | None = None

require_auth = RequireAuth()

ROOT = Path(__file__).parents[2]


def _codex_status() -> dict:
    from bareclaw.config import _parse_dotenv
    f = ROOT / "secrets" / "codex.env"
    if not f.exists():
        return {"connected": False}
    s = _parse_dotenv(f)
    expiry = int(s.get("token_expiry", "0"))
    return {"connected": bool(s.get("access_token")), "expiry": expiry}


def create_router(config: AppConfig, clients: LLMClients) -> APIRouter:
    global _config, _clients
    _config = config
    _clients = clients

    router = APIRouter()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        return templates.TemplateResponse("login.html", {"request": request, "next": next})

    @router.post("/login")
    async def login_post(request: Request):
        form = await request.form()
        key = str(form.get("api_key", ""))
        next_url = str(form.get("next", "/"))
        if _is_valid(key):
            response = RedirectResponse(url=next_url, status_code=303)
            response.set_cookie("bareclaw_session", key, httponly=True, samesite="lax")
            return response
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next_url, "error": "Invalid API key"},
            status_code=401,
        )

    @router.post("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("bareclaw_session")
        return response

    # ------------------------------------------------------------------
    # Main pages
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def index(request: Request):
        token = request.cookies.get("bareclaw_session", "")
        return templates.TemplateResponse(
            "chat.html",
            {
                "request": request,
                "agents": list(_config.agents.values()),
                "default_agent": _config.default_agent,
                "ws_token": token,
            },
        )

    @router.get("/crons", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def crons_page(request: Request):
        runs = await db.fetch_cron_runs(limit=100)
        return templates.TemplateResponse(
            "crons.html",
            {
                "request": request,
                "crons": list(_config.crons.values()),
                "runs": runs,
            },
        )

    @router.get("/webhooks", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def webhooks_page(request: Request):
        runs = await db.fetch_webhook_runs(limit=100)
        return templates.TemplateResponse(
            "webhooks.html",
            {
                "request": request,
                "webhooks": list(_config.webhooks.values()),
                "runs": runs,
            },
        )

    @router.get("/memories", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def memories_page(request: Request):
        memories = mem_mod.load_all()
        return templates.TemplateResponse(
            "memories.html",
            {
                "request": request,
                "memories": memories,
            },
        )

    # ------------------------------------------------------------------
    # WebSocket chat
    # ------------------------------------------------------------------

    @router.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        await websocket.accept()
        # Authenticate via query param (browser can't set headers for WS)
        token = websocket.query_params.get("token", "")
        if not _is_valid(token):
            await websocket.send_json({"error": "Unauthorized"})
            await websocket.close(code=4001)
            return

        try:
            while True:
                data = await websocket.receive_json()
                agent_id = data.get("agent", _config.default_agent)
                user_text = data.get("message", "").strip()
                if not user_text:
                    continue

                agent = _config.agents.get(agent_id)
                if not agent:
                    await websocket.send_json({"error": f"Agent '{agent_id}' not found"})
                    continue

                # Use conversation history from client if provided (for persistence across refreshes)
                # The conversation from the client already includes the user's latest message
                conversation = data.get("conversation", [])

                full_response = ""
                async for chunk in run_agent_stream(agent, _clients, list(conversation), _config.platform_identity, _config):
                    full_response += chunk
                    await websocket.send_json({"chunk": chunk, "done": False})

                await websocket.send_json({"chunk": "", "done": True})
                # Note: Client will add assistant response to its local conversation
                # We log the full conversation including the assistant's response
                full_conversation = conversation + [{"role": "assistant", "content": full_response}]
                await db.log_chat("web", agent_id, full_conversation)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.exception("WebSocket error: %s", exc)
            try:
                await websocket.send_json({"error": str(exc)})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # JSON data API (for AJAX refreshes)
    # ------------------------------------------------------------------

    @router.get("/api/crons", dependencies=[Depends(require_auth)])
    async def api_crons():
        from bareclaw.config import _load_crons
        crons = _load_crons(ROOT / "crons")
        return [
            {
                "id": c.id,
                "schedule": c.schedule,
                "project": c.project,
                "task": c.task,
                "command": c.command,
                "workspace": c.workspace,
                "timeout": c.timeout,
                "notify_telegram": c.notify_telegram,
            }
            for c in crons.values()
        ]

    @router.get("/api/cron-runs", dependencies=[Depends(require_auth)])
    async def api_cron_runs(job_id: str | None = None, limit: int = 50):
        return await db.fetch_cron_runs(job_id=job_id, limit=limit)

    @router.get("/api/webhook-runs", dependencies=[Depends(require_auth)])
    async def api_webhook_runs(webhook_id: str | None = None, limit: int = 50):
        return await db.fetch_webhook_runs(webhook_id=webhook_id, limit=limit)

    @router.get("/api/agents", dependencies=[Depends(require_auth)])
    async def api_agents():
        return [
            {
                "id": a.id,
                "name": a.name,
                "model": a.model,
                "tools": a.tools,
                "workspace": a.workspace,
            }
            for a in _config.agents.values()
        ]

    @router.get("/api/memories", dependencies=[Depends(require_auth)])
    async def api_memories():
        return [
            {"id": m.id, "title": m.title, "keywords": m.keywords, "content": m.content}
            for m in mem_mod.load_all()
        ]

    @router.get("/superpowers", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def superpowers_page(request: Request):
        return templates.TemplateResponse(
            "superpowers.html",
            {
                "request": request,
                "superpowers": sp_mod.load_all(),
            },
        )

    @router.get("/api/superpowers", dependencies=[Depends(require_auth)])
    async def api_superpowers():
        return [
            {
                "id": sp.id,
                "name": sp.name,
                "description": sp.description,
                "keywords": sp.keywords,
                "config": sp.config,
                "secrets": {k: "***" for k in sp.secrets},
                "has_bootstrap": bool(sp.bootstrap_prompt),
                "bootstrap_agent": sp.bootstrap_agent,
            }
            for sp in sp_mod.load_all()
        ]

    @router.post("/api/superpowers/{sp_id}/bootstrap", dependencies=[Depends(require_auth)])
    async def api_bootstrap(sp_id: str):
        sp = sp_mod.load_one(sp_id)
        if not sp:
            return JSONResponse({"error": f"Superpower '{sp_id}' not found"}, status_code=404)
        if not sp.bootstrap_prompt:
            return JSONResponse({"error": "No bootstrap_prompt defined"}, status_code=400)
        agent_id = sp.bootstrap_agent or _config.default_agent
        agent = _config.agents.get(agent_id)
        if not agent:
            return JSONResponse({"error": f"Agent '{agent_id}' not found"}, status_code=404)
        prompt = sp_mod.interpolate(sp.bootstrap_prompt, sp)
        response, _ = await run_agent(agent, _clients, [{"role": "user", "content": prompt}], _config.platform_identity, _config)
        return {"response": response}

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    @router.get("/projects", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def projects_page(request: Request):
        projects = proj_mod.load_all()
        # Enrich projects with runbook existence check
        projects_data = []
        for p in projects:
            # Create a dict with project data + has_runbook flag
            proj_dict = {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "keywords": p.keywords,
                "agent": p.agent,
                "memories": p.memories,
                "tasks": p.tasks,
                "bootstrap_prompt": p.bootstrap_prompt,
                "bootstrap_agent": p.bootstrap_agent,
                "has_runbook": proj_mod.has_runbook(p.id),
            }
            projects_data.append(proj_dict)

        return templates.TemplateResponse(
            "projects.html",
            {
                "request": request,
                "projects": projects_data,
            },
        )

    @router.get("/api/projects", dependencies=[Depends(require_auth)])
    async def api_projects():
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "keywords": p.keywords,
                "agent": p.agent,
                "memories": p.memories,
                "tasks": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "description": t.description,
                        "agent": t.agent,
                    }
                    for t in p.tasks
                ],
                "has_bootstrap": bool(p.bootstrap_prompt) and not proj_mod.has_runbook(p.id),
                "bootstrap_agent": p.bootstrap_agent,
            }
            for p in proj_mod.load_all()
        ]

    @router.post(
        "/api/projects/{project_id}/tasks/{task_id}/run",
        dependencies=[Depends(require_auth)],
    )
    async def api_run_task(project_id: str, task_id: str):
        try:
            response = await run_project_task(project_id, task_id, _config, _clients)
        except TaskRunError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        return {"response": response}

    @router.post("/api/projects/{project_id}/bootstrap", dependencies=[Depends(require_auth)])
    async def api_bootstrap_project(project_id: str):
        proj = proj_mod.load_one(project_id)
        if not proj:
            return JSONResponse({"error": f"Project '{project_id}' not found"}, status_code=404)
        if not proj.bootstrap_prompt:
            return JSONResponse({"error": "No bootstrap_prompt defined"}, status_code=400)
        agent_id = proj.bootstrap_agent or proj.agent or _config.default_agent
        agent = _config.agents.get(agent_id)
        if not agent:
            return JSONResponse({"error": f"Agent '{agent_id}' not found"}, status_code=404)
        prompt = proj_mod.interpolate(proj.bootstrap_prompt, proj)
        response, _ = await run_agent(agent, _clients, [{"role": "user", "content": prompt}], _config.platform_identity, _config)
        return {"response": response}

    # ------------------------------------------------------------------
    # Settings + Codex OAuth
    # ------------------------------------------------------------------

    @router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def settings_page(request: Request):
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "codex": _codex_status()},
        )

    @router.post("/api/auth/codex/start", dependencies=[Depends(require_auth)])
    async def api_codex_start():
        global _codex_flow_task, _codex_flow_result, _codex_flow_verifier, _codex_flow_state
        import secrets as _secrets
        from bareclaw.web.oauth_codex import (
            _pkce_challenge, _pkce_verifier, build_auth_url, run_oauth_flow,
        )
        # Cancel any existing flow
        if _codex_flow_task and not _codex_flow_task.done():
            _codex_flow_task.cancel()
        _codex_flow_result = None

        # Generate PKCE params here so we can return auth_url immediately
        # and pass the same params into the background flow.
        verifier  = _pkce_verifier()
        challenge = _pkce_challenge(verifier)
        state     = _secrets.token_hex(16)
        auth_url  = build_auth_url(challenge, state)

        _codex_flow_verifier = verifier
        _codex_flow_state    = state

        secrets_file = ROOT / "secrets" / "codex.env"

        async def _run():
            global _codex_flow_result
            _codex_flow_result = await run_oauth_flow(secrets_file, verifier=verifier, state=state)

        _codex_flow_task = asyncio.create_task(_run())
        return {"auth_url": auth_url}

    @router.get("/api/auth/codex/status", dependencies=[Depends(require_auth)])
    async def api_codex_status():
        in_progress = bool(_codex_flow_task and not _codex_flow_task.done())
        return {
            **_codex_status(),
            "in_progress": in_progress,
            "flow_result": _codex_flow_result,
        }

    @router.post("/api/auth/codex/token", dependencies=[Depends(require_auth)])
    async def api_codex_token(request: Request):
        body = await request.json()
        token = str(body.get("token", "")).strip()
        if not token:
            return JSONResponse({"error": "token is required"}, status_code=400)
        secrets_file = ROOT / "secrets" / "codex.env"
        secrets_file.parent.mkdir(parents=True, exist_ok=True)
        # Store with no refresh token and a 1-hour expiry; auto-refresh will
        # handle it on next use if a refresh_token is provided separately.
        expiry = int(time.time()) + 3600
        secrets_file.write_text(
            f"access_token={token}\nrefresh_token=\ntoken_expiry={expiry}\n"
        )
        secrets_file.chmod(0o600)
        return {"ok": True}

    @router.post("/api/auth/codex/callback", dependencies=[Depends(require_auth)])
    async def api_codex_callback(request: Request):
        """Accept a pasted callback URL, extract the code, and exchange it for tokens."""
        global _codex_flow_task, _codex_flow_result
        from urllib.parse import parse_qs, urlparse
        from bareclaw.web.oauth_codex import exchange_code

        body = await request.json()
        url  = str(body.get("url", "")).strip()
        if not url:
            return JSONResponse({"error": "url is required"}, status_code=400)
        if not _codex_flow_verifier or not _codex_flow_state:
            return JSONResponse({"error": "No active OAuth flow. Click Connect first."}, status_code=400)

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        code      = (params.get("code")  or [None])[0]
        ret_state = (params.get("state") or [None])[0]

        if not code:
            return JSONResponse({"error": "No code found in URL."}, status_code=400)
        if ret_state != _codex_flow_state:
            return JSONResponse({"error": "State mismatch — start a new flow and try again."}, status_code=400)

        # Cancel the port-1455 server (no longer needed)
        if _codex_flow_task and not _codex_flow_task.done():
            _codex_flow_task.cancel()

        secrets_file = ROOT / "secrets" / "codex.env"
        result = await exchange_code(code, _codex_flow_verifier, secrets_file)
        _codex_flow_result = result
        return result

    @router.post("/api/auth/codex/disconnect", dependencies=[Depends(require_auth)])
    async def api_codex_disconnect():
        global _codex_flow_task, _codex_flow_result
        if _codex_flow_task and not _codex_flow_task.done():
            _codex_flow_task.cancel()
        _codex_flow_task = None
        _codex_flow_result = None
        f = ROOT / "secrets" / "codex.env"
        if f.exists():
            f.unlink()
        return {"ok": True}

    # ------------------------------------------------------------------
    # System Management
    # ------------------------------------------------------------------

    @router.post("/api/system/restart", dependencies=[Depends(require_auth)])
    async def api_system_restart():
        """Restart the Bareclaw service."""
        import os
        import subprocess

        try:
            # Detect runtime environment and execute appropriate restart command
            async def _do_restart():
                # Give the HTTP response a chance to return before restarting
                await asyncio.sleep(0.5)

                # Check if running under systemd (user or system)
                try:
                    result = subprocess.run(
                        ["systemctl", "--user", "is-active", "bareclaw"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0:
                        # Running as systemd user service - exit with special code to trigger restart
                        # Note: service must have Restart=on-failure or Restart=always
                        logger.info("Restarting systemd user service via exit code 42")
                        os._exit(42)
                except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
                    pass

                # Check if running in Docker
                if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
                    logger.info("Running in container - exiting for container runtime to restart")
                    os._exit(42)
                    return

                # Check if docker-compose.yml exists in parent directory (compose deployment)
                compose_file = ROOT / "docker-compose.yml"
                if compose_file.exists():
                    logger.info("Restarting via docker compose")
                    try:
                        subprocess.run(
                            ["docker", "compose", "-f", str(compose_file), "restart"],
                            timeout=10,
                            check=True,
                        )
                        return
                    except subprocess.CalledProcessError as e:
                        logger.error(f"Docker compose restart failed: {e}")
                    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                        logger.warning(f"Docker compose not available: {e}")

                # Fallback: exit with non-zero code to trigger supervisor restart
                logger.info("Restarting via process exit code 42 (supervisor should restart)")
                os._exit(42)

            # Schedule restart in background
            asyncio.create_task(_do_restart())
            return {"ok": True, "message": "Restart initiated"}

        except Exception as e:
            logger.exception("Restart endpoint error")
            return JSONResponse(
                {"ok": False, "error": f"Restart failed: {str(e)}"},
                status_code=500
            )

    return router
