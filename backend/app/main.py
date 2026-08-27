"""FastAPI application.

The Supabase client is built once here and hung off app.state. If it cannot
be built the app still starts -- /health stays green so Railway's deploy
healthcheck passes, and /health/deep reports the failure honestly. A
misconfigured database should be a visible red light, not a crash loop.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import campaigns, health
from app.core import db as dbmod
from app.core.config import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s"
)
log = logging.getLogger("kirana")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = None
    try:
        app.state.db = await dbmod.create_db_backend()
        log.info("database ready via %s backend", app.state.db.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("database unavailable: %s", exc)
    yield
    if app.state.db is not None:
        await app.state.db.close()
    app.state.db = None


app = FastAPI(
    title="Kirana Agent",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# allow_origin_regex is what makes every Vercel preview deployment work
# without redeploying the backend. Without it only the exact origins match.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.BACKEND_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(campaigns.router)


@app.get("/")
async def root() -> dict:
    return {"service": "kirana-agent", "docs": "/docs", "health": "/health"}
