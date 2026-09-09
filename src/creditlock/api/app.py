"""
FastAPI application for CreditLock.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from creditlock.agents.app import agent_router
from creditlock.api.demo import router as demo_router
from creditlock.api.export import router as export_router
from creditlock.api.resolutions import router as resolutions_router
from creditlock.settings import get_settings

logger = logging.getLogger("creditlock")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from creditlock.api.export import UninitializedProductionStore, set_production_store
    from creditlock.domain.store import FirestoreProductionStore, InMemoryProductionStore

    settings = get_settings()
    if settings.store_backend == "memory_demo":
        logger.warning(
            "WARNING: CreditLock starting with in-memory production store (local-demo mode). "
            "State is ephemeral and non-production!"
        )
        set_production_store(InMemoryProductionStore())
    elif settings.store_backend == "firestore":
        set_production_store(UninitializedProductionStore())
        from google.cloud import firestore

        try:
            if not settings.google_cloud_project:
                raise ValueError("GOOGLE_CLOUD_PROJECT must be configured when STORE_BACKEND='firestore'.")
            client = firestore.Client(
                project=settings.google_cloud_project,
                database=settings.firestore_database,
            )
            store = FirestoreProductionStore(client=client)
            set_production_store(store)
            logger.info(f"CreditLock initialized with FirestoreProductionStore (project: {settings.google_cloud_project})")
        except Exception as e:
            logger.error(f"Failed to initialize Firestore client/store: {e}")
            raise RuntimeError(f"Firestore initialization failed: {e}") from e
    else:
        raise ValueError(f"Unrecognized or unsupported STORE_BACKEND '{settings.store_backend}'")
    try:
        yield
    finally:
        set_production_store(UninitializedProductionStore())


app = FastAPI(
    title="CreditLock",
    description="Production credit-obligation control plane",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(export_router, tags=["export"])
app.include_router(resolutions_router, tags=["authorizations"])
app.include_router(agent_router)
app.include_router(demo_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False, response_model=None)
async def serve_favicon() -> HTMLResponse:
    # Serve an empty 204 to prevent 404 noise in browser console
    from fastapi.responses import Response
    return Response(content=b"", status_code=204)  # type: ignore[return-value]


@app.get("/", response_class=HTMLResponse, response_model=None)
@app.get("/demo", response_class=HTMLResponse, response_model=None)
async def serve_judge_ui() -> FileResponse | HTMLResponse:
    ui_path = Path("src/creditlock/static/index.html")
    if not ui_path.exists():
        return HTMLResponse("<h1>CreditLock Control Plane</h1><p>UI file missing.</p>")
    return FileResponse(ui_path, media_type="text/html")
