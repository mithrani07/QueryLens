"""
main.py

QueryLens FastAPI application entrypoint.

Responsibilities:
- Initializes the FastAPI app with metadata used in the auto-generated docs.
- Registers CORS middleware using settings from config.py.
- Mounts the /api routers (upload, database, query).
- Serves the vanilla HTML/CSS/JS frontend as static files, with index.html
  available at the root route "/".
- Exposes a lightweight /health endpoint for Render's health checks.

Run locally with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import PROJECT_ROOT, settings
from routes import database as database_routes
from routes import query as query_routes
from routes import upload as upload_routes
from services.file_service import cleanup_old_files

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("querylens.main")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs startup/shutdown logic around the app's lifetime."""
    settings.ensure_upload_dir()
    logger.info(
        "Starting %s v%s in %s mode (LLM provider: %s, model: %s)",
        settings.APP_NAME, settings.APP_VERSION, settings.ENVIRONMENT,
        settings.LLM_PROVIDER, settings.active_model,
    )

    try:
        removed = cleanup_old_files(max_age_hours=24)
        if removed:
            logger.info("Startup cleanup removed %d stale upload(s)", removed)
    except Exception:
        logger.exception("Startup cleanup of old uploads failed (continuing anyway)")

    yield

    logger.info("Shutting down %s", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    description="Talk to your data with SQL - a natural language to SQL generator.",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# ----------------------------------------------------------------------
# CORS
# ----------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# ----------------------------------------------------------------------
# API routers (all under /api via each router's own prefix)
# ----------------------------------------------------------------------
app.include_router(upload_routes.router)
app.include_router(database_routes.router)
app.include_router(query_routes.router)


# ----------------------------------------------------------------------
# Health check (used by render.yaml's healthCheckPath)
# ----------------------------------------------------------------------
@app.get("/health", tags=["meta"], summary="Health check")
async def health_check() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "llm_provider": settings.LLM_PROVIDER,
        }
    )


# ----------------------------------------------------------------------
# Static frontend
# Mounted last so it never shadows /api or /health routes above. Serves
# index.html at "/" and all other frontend assets (style.css, script.js)
# alongside it. html=True makes StaticFiles resolve "/" to index.html
# automatically.
# ----------------------------------------------------------------------
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logger.info("Serving static frontend from %s", FRONTEND_DIR)
else:
    logger.warning(
        "Frontend directory not found at %s - only /api routes will be available.",
        FRONTEND_DIR,
    )

    @app.get("/", tags=["meta"], summary="Frontend not built yet")
    async def frontend_missing() -> JSONResponse:
        return JSONResponse(
            {
                "detail": (
                    "Frontend build not found. Expected static files at "
                    f"{FRONTEND_DIR}. API routes remain available under /api."
                )
            },
            status_code=200,
        )