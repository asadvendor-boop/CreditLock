"""
Event Ingestion Worker application.

Separately deployable Cloud Run service that runs EventIngestionWorker in a background
daemon thread, consuming events from Confluent Cloud and projecting them into Firestore.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from creditlock.events.consumer import EventIngestionWorker
from creditlock.events.firestore_store import FirestoreProjectionStore
from creditlock.events.transport import ConfluentTransport
from creditlock.settings import get_settings

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "creditlock-firestore-projection-v1"

_worker: EventIngestionWorker | None = None
_worker_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _worker, _worker_thread

    settings = get_settings()
    if not settings.confluent_runtime_enabled:
        raise RuntimeError(
            "CONFLUENT_RUNTIME_ENABLED must be True to run the Event Ingestion Worker."
        )

    transport = ConfluentTransport.from_settings(consumer_group=CONSUMER_GROUP)
    store = FirestoreProjectionStore.from_settings()

    worker = EventIngestionWorker(
        transport=transport,
        store=store,
        topic=settings.confluent_topic,
        consumer_group=CONSUMER_GROUP,
    )
    _worker = worker

    worker_thread = threading.Thread(
        target=worker.start,
        name="EventIngestionWorkerThread",
        daemon=True,
    )
    _worker_thread = worker_thread
    worker_thread.start()
    logger.info("Event ingestion worker thread started.")

    yield

    if _worker is not None:
        _worker.stop()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5.0)
    logger.info("Event ingestion worker thread stopped.")


app = FastAPI(
    title="CreditLock Event Ingestion Worker",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    """
    Health check endpoint reporting safe operational metadata.

    Never outputs credentials, bootstrap servers, or sensitive tokens.
    """
    settings = get_settings()
    is_alive = _worker_thread is not None and _worker_thread.is_alive()
    return {
        "status": "healthy" if is_alive else "degraded",
        "worker_alive": is_alive,
        "topic": settings.confluent_topic,
        "consumer_group": CONSUMER_GROUP,
    }
