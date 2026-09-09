"""
Deterministic release checkpoint module.

Constructs a resolution.recorded event from an Authorization and synchronizes
it across Confluent Cloud and Firestore before release export packaging.
"""

from __future__ import annotations

import logging
import time
import uuid

from pydantic import BaseModel

from creditlock.domain.models import Authorization
from creditlock.events.firestore_store import FirestoreProjectionStore
from creditlock.events.models import (
    EVENT_TYPE_RESOLUTION_RECORDED,
    CreditLockEvent,
    ResolutionRecordedPayload,
)
from creditlock.events.transport import ConfluentTransport, EventTransport
from creditlock.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Deterministic namespace for release checkpoint event IDs
_NAMESPACE_RELEASE_CHECKPOINT = uuid.uuid5(uuid.NAMESPACE_DNS, "creditlock.release.checkpoint")


class ReleaseCheckpointUnavailable(Exception):
    """
    Dedicated safe exception for release checkpoint publish/synchronization failure.

    Guarantees no raw underlying exceptions, tokens, or credentials are leaked.
    """

    def __init__(
        self,
        message: str = "Release event synchronization is temporarily unavailable.",
    ) -> None:
        super().__init__(message)
        self.message = message


class ReleaseCheckpointSyncResult(BaseModel):
    """
    Narrow synchronization result containing only safe evidence.

    Never contains credentials, bootstrap servers, payload contents, tokens, or exceptions.
    """

    model_config = {"extra": "forbid"}

    status: str = "SYNCHRONIZED"
    transport: str = "CONFLUENT_CLOUD"
    topic: str
    event_type: str = "resolution.recorded"
    event_id: str
    projection_backend: str = "FIRESTORE"


def build_release_checkpoint_event(
    authorization: Authorization,
    aggregate_version: int = 1,
) -> CreditLockEvent:
    """
    Construct a deterministic resolution.recorded event from an Authorization.

    - Uses authorization.authorized_at for event and payload timestamps.
    - Preserves production ID, issue ID, action/resolution type, actor ID, role,
      reason, and all four bound hashes.
    - Uses proposal_id as correlation_id.
    - Uses the assigned aggregate_version.
    - Generates a stable UUID5 event ID from production_id and authorization_id.
    """
    stable_event_id = str(
        uuid.uuid5(
            _NAMESPACE_RELEASE_CHECKPOINT,
            f"{authorization.production_id}:{authorization.authorization_id}",
        )
    )

    bound_hashes = {
        "manifest_hash": authorization.manifest_hash,
        "obligation_registry_version_hash": authorization.obligation_registry_version_hash,
        "artifact_index_digest": authorization.artifact_index_digest,
        "visual_observations_hash": authorization.visual_observations_hash,
    }

    payload = ResolutionRecordedPayload(
        production_id=authorization.production_id,
        issue_id=authorization.issue_id,
        resolution_type=authorization.action,
        actor_id=authorization.actor_id,
        role=authorization.role,
        bound_hashes=bound_hashes,
        reason=authorization.reason,
        at=authorization.authorized_at,
    )

    return CreditLockEvent(
        event_id=stable_event_id,
        event_type=EVENT_TYPE_RESOLUTION_RECORDED,
        schema_version="1.0",
        production_id=authorization.production_id,
        aggregate_version=aggregate_version,
        actor_id=authorization.actor_id,
        occurred_at=authorization.authorized_at,
        correlation_id=authorization.proposal_id,
        payload=payload,
    )


def synchronize_release_checkpoint(
    authorization: Authorization,
    transport: EventTransport | None = None,
    store: FirestoreProjectionStore | None = None,
    settings: Settings | None = None,
) -> ReleaseCheckpointSyncResult:
    """
    Publish and synchronize a release checkpoint event to Firestore.

    1. Check whether the processed-event document already exists.
    2. If it exists, return synchronized without republishing.
    3. Allocate a durable, monotonic aggregate_version above production last_aggregate_version.
    4. Wait until all prior aggregate versions are projected into Firestore (ordering gate).
    5. Publish the deterministic event once.
    6. Poll Firestore until the exact processed-event document exists or timeout expires.
    7. Raise ReleaseCheckpointUnavailable on publish failure or timeout.
    """
    cfg = settings or get_settings()
    target_store = store or FirestoreProjectionStore.from_settings()

    stable_event_id = str(
        uuid.uuid5(
            _NAMESPACE_RELEASE_CHECKPOINT,
            f"{authorization.production_id}:{authorization.authorization_id}",
        )
    )

    # 1 & 2. Check if already projected
    try:
        if target_store.has_processed_event(authorization.production_id, stable_event_id):
            return ReleaseCheckpointSyncResult(
                status="SYNCHRONIZED",
                transport="CONFLUENT_CLOUD",
                topic=cfg.confluent_topic,
                event_type=EVENT_TYPE_RESOLUTION_RECORDED,
                event_id=stable_event_id,
                projection_backend="FIRESTORE",
            )
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "Initial processed-event check failed with %s: proceeding to allocate & publish",
            type(ex).__name__,
        )

    # 3. Allocate aggregate version dynamically
    try:
        allocated_version = target_store.allocate_aggregate_version(
            authorization.production_id,
            stable_event_id,
        )
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Failed to allocate aggregate version for event %s: %s",
            stable_event_id,
            type(ex).__name__,
        )
        raise ReleaseCheckpointUnavailable(
            "Release event synchronization is temporarily unavailable."
        ) from None

    event = build_release_checkpoint_event(
        authorization,
        aggregate_version=allocated_version,
    )

    poll_interval = cfg.confluent_sync_poll_interval_seconds

    # 4. Durable publication ordering check:
    # A later allocated checkpoint must not overtake an earlier unacknowledged checkpoint.
    # Wait until all prior aggregate versions (up to allocated_version - 1)
    # have been projected into Firestore before publishing.
    if allocated_version > 1:
        order_deadline = time.monotonic() + cfg.confluent_sync_timeout_seconds
        while time.monotonic() < order_deadline:
            try:
                if target_store.has_processed_event(event.production_id, event.event_id):
                    return ReleaseCheckpointSyncResult(
                        status="SYNCHRONIZED",
                        transport="CONFLUENT_CLOUD",
                        topic=cfg.confluent_topic,
                        event_type=event.event_type,
                        event_id=event.event_id,
                        projection_backend="FIRESTORE",
                    )
                if hasattr(target_store, "get_last_aggregate_version"):
                    last_applied = target_store.get_last_aggregate_version(event.production_id)
                    if last_applied >= allocated_version - 1:
                        break
                else:
                    break
            except Exception as ex:  # noqa: BLE001
                logger.warning(
                    "Error checking prior version for event %s: %s",
                    event.event_id,
                    type(ex).__name__,
                )
            time.sleep(poll_interval)
        else:
            is_ready = False
            try:
                if target_store.has_processed_event(event.production_id, event.event_id):
                    return ReleaseCheckpointSyncResult(
                        status="SYNCHRONIZED",
                        transport="CONFLUENT_CLOUD",
                        topic=cfg.confluent_topic,
                        event_type=event.event_type,
                        event_id=event.event_id,
                        projection_backend="FIRESTORE",
                    )
                if hasattr(target_store, "get_last_aggregate_version"):
                    is_ready = (
                        target_store.get_last_aggregate_version(event.production_id)
                        >= allocated_version - 1
                    )
                else:
                    is_ready = True
            except Exception as ex:  # noqa: BLE001
                logger.warning("Final prior-version check failed: %s", type(ex).__name__)

            if not is_ready:
                logger.error(
                    "Release checkpoint publication timed out waiting for prior event versions (needed >= %d)",
                    allocated_version - 1,
                )
                raise ReleaseCheckpointUnavailable(
                    "Release event synchronization timed out."
                )

    # 5. Publish
    target_transport = transport or ConfluentTransport.from_settings()
    try:
        target_transport.publish(event)
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Failed to publish release checkpoint event %s: %s",
            event.event_id,
            type(ex).__name__,
        )
        raise ReleaseCheckpointUnavailable(
            "Release event synchronization is temporarily unavailable."
        ) from None

    # 6. Poll Firestore until projected or timeout
    deadline = time.monotonic() + cfg.confluent_sync_timeout_seconds

    while time.monotonic() < deadline:
        try:
            if target_store.has_processed_event(event.production_id, event.event_id):
                return ReleaseCheckpointSyncResult(
                    status="SYNCHRONIZED",
                    transport="CONFLUENT_CLOUD",
                    topic=cfg.confluent_topic,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    projection_backend="FIRESTORE",
                )
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "Error polling processed-event %s: %s",
                event.event_id,
                type(ex).__name__,
            )
        time.sleep(poll_interval)

    # Final check after deadline
    try:
        if target_store.has_processed_event(event.production_id, event.event_id):
            return ReleaseCheckpointSyncResult(
                status="SYNCHRONIZED",
                transport="CONFLUENT_CLOUD",
                topic=cfg.confluent_topic,
                event_type=event.event_type,
                event_id=event.event_id,
                projection_backend="FIRESTORE",
            )
    except Exception as ex:  # noqa: BLE001
        logger.warning("Final processed-event check failed: %s", type(ex).__name__)

    logger.error(
        "Release checkpoint synchronization timed out after %s seconds for event %s",
        cfg.confluent_sync_timeout_seconds,
        event.event_id,
    )
    raise ReleaseCheckpointUnavailable("Release event synchronization timed out.")
