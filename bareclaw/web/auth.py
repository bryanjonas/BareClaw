"""
Authentication helpers — simple API key checked via header or session cookie.
"""
from __future__ import annotations

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse


_API_KEY: str = "changeme"


def init_auth(api_key: str) -> None:
    global _API_KEY
    _API_KEY = api_key


def _is_valid(key: str) -> bool:
    return key == _API_KEY


class RequireAuth:
    """
    FastAPI dependency that allows requests with a valid:
    - ``Authorization: Bearer <key>`` header, OR
    - ``bareclaw_session`` cookie containing the key
    """

    async def __call__(self, request: Request, bareclaw_session: str | None = Cookie(default=None)):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and _is_valid(auth_header.removeprefix("Bearer ").strip()):
            return
        if bareclaw_session and _is_valid(bareclaw_session):
            return
        # For browser requests redirect to login; for API return 401
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
