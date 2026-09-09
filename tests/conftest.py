"""
Pytest global configuration and autouse fixtures for CreditLock tests.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from creditlock.settings import get_settings


@pytest.fixture(autouse=True)
def setup_test_environment() -> Generator[None, None, None]:
    """Inject valid strong test JWT secret and store settings for hermetic test execution."""
    from creditlock.api.export import set_production_store
    from creditlock.domain.store import InMemoryProductionStore

    os.environ["JWT_SECRET"] = "test-secret-must-be-at-least-32-bytes-long-for-security"
    os.environ["STORE_BACKEND"] = "memory_demo"
    os.environ["ALLOW_IN_MEMORY_DEMO"] = "true"
    if os.environ.get("CREDITLOCK_RUN_LIVE_GCS") != "1":
        os.environ["EVIDENCE_BUCKET"] = ""
    get_settings.cache_clear()
    set_production_store(InMemoryProductionStore())
    yield
    os.environ["JWT_SECRET"] = "test-secret-must-be-at-least-32-bytes-long-for-security"
    os.environ["STORE_BACKEND"] = "memory_demo"
    os.environ["ALLOW_IN_MEMORY_DEMO"] = "true"
    if os.environ.get("CREDITLOCK_RUN_LIVE_GCS") != "1":
        os.environ["EVIDENCE_BUCKET"] = ""
    get_settings.cache_clear()
    set_production_store(InMemoryProductionStore())
