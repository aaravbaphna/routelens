"""HTTP surface for the dashboard: a static single-page app plus a small JSON API."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .store import Store

STATIC = Path(__file__).parent / "static"
ASSETS = {"app.js": "application/javascript", "style.css": "text/css", "logo.svg": "image/svg+xml"}
WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "14d": 14 * 86400}
BUCKETS = {"1h": 60, "6h": 300, "24h": 1800, "7d": 6 * 3600, "14d": 12 * 3600}


def _tokens() -> "list[str]":
    toks = [os.environ.get("ROUTELENS_TOKEN")]
    try:
        from litellm.proxy import proxy_server
        toks.append(getattr(proxy_server, "master_key", None))
    except Exception:
        pass
    toks.append(os.environ.get("LITELLM_MASTER_KEY"))
    return [t for t in toks if t]


def _supplied(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-routelens-token")


async def require_auth(request: Request) -> None:
    """If the proxy has a master key (or ROUTELENS_TOKEN is set), the API requires it."""
    allowed = _tokens()
    if not allowed:
        return
    given = _supplied(request) or ""
    if not any(secrets.compare_digest(given, t) for t in allowed):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def build_router(store: Store, moderation_enabled: bool = False, moderation_mode: str = "enforce") -> APIRouter:
    r = APIRouter()

    def window(w: str) -> "tuple[float, int, str]":
        w = w if w in WINDOWS else "24h"
        return time.time() - WINDOWS[w], BUCKETS[w], w

    @r.get("/routelens", include_in_schema=False)
    @r.get("/routelens/", include_in_schema=False)
    async def index() -> Any:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @r.get("/routelens/assets/{name}", include_in_schema=False)
    async def asset(name: str) -> Any:
        if name not in ASSETS:
            raise HTTPException(404)
        return FileResponse(STATIC / name, media_type=ASSETS[name])

    @r.get("/routelens/api/meta", dependencies=[Depends(require_auth)])
    async def meta() -> Any:
        return {
            "providers": await asyncio.to_thread(store.providers),
            "auth_required": bool(_tokens()),
            "windows": list(WINDOWS),
            "moderation": {"enabled": moderation_enabled, "mode": moderation_mode},
        }

    @r.get("/routelens/api/overview", dependencies=[Depends(require_auth)])
    async def overview(window_: str = Query("24h", alias="window")) -> Any:
        since, bucket, w = window(window_)
        data = await asyncio.to_thread(store.overview, since, bucket)
        data.update({"window": w, "bucket_s": bucket, "since": since})
        return data

    @r.get("/routelens/api/sessions", dependencies=[Depends(require_auth)])
    async def sessions(window_: str = Query("24h", alias="window"), q: Optional[str] = None, limit: int = 50) -> Any:
        since, _, _ = window(window_)
        return {"sessions": await asyncio.to_thread(store.sessions, since, max(1, min(limit, 200)), q)}

    @r.get("/routelens/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def session(session_id: str) -> Any:
        data = await asyncio.to_thread(store.session, session_id)
        if not data["turns"]:
            raise HTTPException(404, "Unknown session")
        return data

    @r.get("/routelens/api/recent", dependencies=[Depends(require_auth)])
    async def recent(limit: int = 30) -> Any:
        return {"attempts": await asyncio.to_thread(store.recent, max(1, min(limit, 200)))}

    @r.get("/routelens/api/moderation/overview", dependencies=[Depends(require_auth)])
    async def moderation_overview(window_: str = Query("24h", alias="window"),
                                   chain: str = "all") -> Any:
        since, bucket, w = window(window_)
        chain = chain if chain in ("input", "output") else "all"
        data = await asyncio.to_thread(store.moderation_overview, since, bucket, chain)
        data.update({"window": w, "bucket_s": bucket, "since": since, "chain": chain})
        return data

    @r.get("/routelens/api/moderation/recent", dependencies=[Depends(require_auth)])
    async def moderation_recent(chain: str = "all", blocked_only: bool = False, limit: int = 30) -> Any:
        chain = chain if chain in ("input", "output") else "all"
        events = await asyncio.to_thread(store.moderation_recent, max(1, min(limit, 200)), chain, blocked_only)
        return {"events": events}

    return r
