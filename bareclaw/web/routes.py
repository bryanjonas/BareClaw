"""
FastAPI routes — web UI, WebSocket chat, login, and data API endpoints.
"""
from __future__ import annotations

import json
import logging
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
from bareclaw.web.auth import RequireAuth, _is_valid

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_config: AppConfig | None = None
_clients: LLMClients | None = None

require_auth = RequireAuth()


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

        conversation: list[dict[str, Any]] = []

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

                conversation.append({"role": "user", "content": user_text})

                full_response = ""
                async for chunk in run_agent_stream(agent, _clients, list(conversation)):
                    full_response += chunk
                    await websocket.send_json({"chunk": chunk, "done": False})

                await websocket.send_json({"chunk": "", "done": True})
                conversation.append({"role": "assistant", "content": full_response})

                await db.log_chat("web", agent_id, conversation)

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
        response, _ = await run_agent(agent, _clients, [{"role": "user", "content": prompt}])
        return {"response": response}

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    @router.get("/projects", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def projects_page(request: Request):
        return templates.TemplateResponse(
            "projects.html",
            {
                "request": request,
                "projects": proj_mod.load_all(),
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
            }
            for p in proj_mod.load_all()
        ]

    @router.post(
        "/api/projects/{project_id}/tasks/{task_id}/run",
        dependencies=[Depends(require_auth)],
    )
    async def api_run_task(project_id: str, task_id: str):
        proj = proj_mod.load_one(project_id)
        if not proj:
            return JSONResponse({"error": f"Project '{project_id}' not found"}, status_code=404)
        task = next((t for t in proj.tasks if t.id == task_id), None)
        if not task:
            return JSONResponse({"error": f"Task '{task_id}' not found"}, status_code=404)
        if not task.prompt:
            return JSONResponse({"error": "Task has no prompt defined"}, status_code=400)
        agent_id = task.agent or proj.agent or _config.default_agent
        agent = _config.agents.get(agent_id)
        if not agent:
            return JSONResponse({"error": f"Agent '{agent_id}' not found"}, status_code=404)
        response, _ = await run_agent(agent, _clients, [{"role": "user", "content": task.prompt}])
        return {"response": response}

    return router
