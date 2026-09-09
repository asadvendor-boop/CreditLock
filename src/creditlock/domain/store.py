"""
Production store abstraction for API and projection access.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from creditlock.evidence.storage import EvidenceStorage

from creditlock.domain.gate import ProjectionStatus
from creditlock.domain.models import (
    Authorization,
    CreditManifest,
    LayoutEvidence,
    Obligation,
    VisualObservations,
)


class ProductionStore(ABC):
    """Abstract interface for production state storage."""

    @abstractmethod
    def get(self, production_id: str) -> dict[str, Any] | None:
        """Return production state dict for production_id, or None if missing."""

    @abstractmethod
    def register(
        self,
        production_id: str,
        obligations: list[Obligation],
        manifest: CreditManifest,
        layout_evidence: LayoutEvidence | None = None,
        visual_observations: VisualObservations | None = None,
        contributor_registry: list[dict[str, Any]] | None = None,
        authorizations: list[Authorization] | None = None,
        projection: Any | None = None,
        frames: list[Any] | None = None,
        artifact_index: Any | None = None,
        render_profile_version: str | None = None,
    ) -> None:
        """Seed or overwrite a production entry."""

    @abstractmethod
    def clear(self) -> None:
        """Reset all in-memory production state."""

    @abstractmethod
    def save_proposal(self, proposal: Any) -> None:
        """Store or update a resolution Proposal for a production."""

    @abstractmethod
    def get_proposals(self, production_id: str) -> list[Any]:
        """Return all proposals for a production."""

    @abstractmethod
    def confirm_proposal(
        self,
        production_id: str,
        proposal_id: str,
        approver_id: str,
        current_hashes: tuple[str, str, str, str],
    ) -> Authorization:
        """Atomically confirm a proposal, creating and storing an Authorization."""

    @abstractmethod
    def get_authorizations(self, production_id: str) -> list[Authorization]:
        """Return all authorizations for a production."""

    @abstractmethod
    def save_export_result(
        self,
        production_id: str,
        release_digest: str,
        delivery_package_path: str,
    ) -> None:
        """Durably record the export result for a production."""


class InMemoryProductionStore(ProductionStore):
    """In-memory production store implementation for tests and dev."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: dict[str, dict[str, Any]] = {}
        self._audit_log: list[dict[str, Any]] = []

    def get(self, production_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._store.get(production_id)

    def register(
        self,
        production_id: str,
        obligations: list[Obligation],
        manifest: CreditManifest,
        layout_evidence: LayoutEvidence | None = None,
        visual_observations: VisualObservations | None = None,
        contributor_registry: list[dict[str, Any]] | None = None,
        authorizations: list[Authorization] | None = None,
        projection: Any | None = None,
        frames: list[Any] | None = None,
        artifact_index: Any | None = None,
        render_profile_version: str | None = None,
    ) -> None:
        with self._lock:
            from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
            from creditlock.domain.gate import ProjectionStatus

            cur_frames = frames
            cur_layout = layout_evidence
            cur_art_index = artifact_index
            cur_profile_ver = render_profile_version or "1.0"

            art_digest = None
            if cur_art_index is not None:
                art_index_dict = (
                    cur_art_index.model_dump()
                    if hasattr(cur_art_index, "model_dump")
                    else cur_art_index
                )
                art_digest = sha256_bytes_digest(canonical_json_bytes(art_index_dict))

            existing = self._store.get(production_id, {})
            self._store[production_id] = {
                "obligations": obligations,
                "manifest": manifest,
                "layout_evidence": cur_layout,
                "visual_observations": visual_observations,
                "contributor_registry": contributor_registry or [],
                "proposals": existing.get("proposals", []),
                "authorizations": authorizations or existing.get("authorizations", []),
                "projection": projection or ProjectionStatus(),
                "frames": cur_frames or [],
                "artifact_index": cur_art_index,
                "artifact_index_digest": art_digest,
                "render_profile_version": cur_profile_ver,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._audit_log.clear()

    def save_proposal(self, proposal: Any) -> None:
        with self._lock:
            prod = self._store.get(proposal.production_id)
            if prod is None:
                raise KeyError(f"Production '{proposal.production_id}' not registered in store.")
            props = prod.setdefault("proposals", [])
            idx = next(
                (
                    i
                    for i, p in enumerate(props)
                    if getattr(p, "proposal_id", None) == proposal.proposal_id
                ),
                None,
            )
            if idx is not None:
                props[idx] = proposal
            else:
                props.append(proposal)
            self._audit_log.append({"event": "PROPOSAL_SAVED", "proposal_id": proposal.proposal_id})

    def get_proposals(self, production_id: str) -> list[Any]:
        with self._lock:
            prod = self._store.get(production_id)
            if prod is None:
                return []
            return list(prod.get("proposals", []))

    def confirm_proposal(
        self,
        production_id: str,
        proposal_id: str,
        approver_id: str,
        current_hashes: tuple[str, str, str, str],
    ) -> Authorization:
        with self._lock:
            prod = self._store.get(production_id)
            if prod is None:
                raise KeyError(f"Production '{production_id}' not found.")
            props: list[Any] = prod.get("proposals", [])
            prop = next((p for p in props if getattr(p, "proposal_id", None) == proposal_id), None)
            if prop is None or getattr(prop, "status", None) != "PENDING":
                raise ValueError(f"Proposal '{proposal_id}' not found or is not pending.")
            if approver_id == prop.proposer_id:
                raise ValueError("Proposer and approver identities must be distinct.")

            m_hash, orv_hash, art_digest, vis_hash = current_hashes
            if (
                prop.manifest_hash != m_hash
                or prop.obligation_registry_version_hash != orv_hash
                or prop.artifact_index_digest != art_digest
                or prop.visual_observations_hash != vis_hash
            ):
                raise ValueError("Stale proposal: production state hashes have changed.")

            import uuid
            from datetime import UTC, datetime

            now_iso = datetime.now(UTC).isoformat()
            auth = Authorization(
                authorization_id=f"auth-{uuid.uuid4().hex[:12]}",
                production_id=production_id,
                proposal_id=proposal_id,
                issue_id=prop.issue_id,
                proposer_id=prop.proposer_id,
                actor_id=approver_id,
                role="RELEASE_APPROVER",
                action=prop.action,
                reason=prop.reason,
                selected_contributor_id=prop.selected_contributor_id,
                manifest_hash=prop.manifest_hash,
                obligation_registry_version_hash=prop.obligation_registry_version_hash,
                artifact_index_digest=prop.artifact_index_digest,
                visual_observations_hash=prop.visual_observations_hash,
                authorized_at=now_iso,
            )

            old_status = prop.status
            auths: list[Authorization] = prod.setdefault("authorizations", [])
            initial_auth_len = len(auths)
            initial_audit_len = len(self._audit_log)

            try:
                auths.append(auth)
                prop.status = "CONFIRMED"
                self._audit_log.append(
                    {
                        "event": "PROPOSAL_CONFIRMED",
                        "proposal_id": proposal_id,
                        "auth_id": auth.authorization_id,
                    }
                )
                return auth
            except Exception:
                prop.status = old_status
                if len(auths) > initial_auth_len:
                    auths.pop()
                if len(self._audit_log) > initial_audit_len:
                    self._audit_log.pop()
                raise

    def get_authorizations(self, production_id: str) -> list[Authorization]:
        with self._lock:
            prod = self._store.get(production_id)
            if prod is None:
                return []
            return list(prod.get("authorizations", []))

    def save_export_result(
        self,
        production_id: str,
        release_digest: str,
        delivery_package_path: str,
    ) -> None:
        if (
            not isinstance(release_digest, str)
            or len(release_digest) != 64
            or not all(c in "0123456789abcdef" for c in release_digest)
        ):
            raise ValueError("release_digest must be a 64-character lowercase hex SHA-256 digest.")
        if not isinstance(delivery_package_path, str) or not delivery_package_path.strip():
            raise ValueError("delivery_package_path must be a non-empty string.")

        with self._lock:
            prod = self._store.get(production_id)
            if prod is None:
                raise KeyError(f"Production '{production_id}' not found.")
            existing_digest = prod.get("release_digest")
            existing_path = prod.get("delivery_package_path")
            if existing_digest is not None or existing_path is not None:
                if existing_digest == release_digest and existing_path == delivery_package_path:
                    return
                raise ValueError(
                    f"Production '{production_id}' already has conflicting export metadata: "
                    f"existing=({existing_digest!r}, {existing_path!r}), "
                    f"new=({release_digest!r}, {delivery_package_path!r})"
                )
            prod["release_digest"] = release_digest
            prod["delivery_package_path"] = delivery_package_path

    @property
    def audit_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._audit_log)


def _validate_and_normalize_projection(projection: Any) -> ProjectionStatus:
    if projection is None:
        return ProjectionStatus(artifact_pending=False, stale_manifest=False)
    if isinstance(projection, ProjectionStatus):
        if type(projection.artifact_pending) is not bool or type(projection.stale_manifest) is not bool:
            raise ValueError("projection attributes must be exact bool values")
        return projection
    if isinstance(projection, dict):
        if set(projection.keys()) != {"artifact_pending", "stale_manifest"}:
            raise ValueError("projection dict must contain exactly artifact_pending and stale_manifest keys")
        if type(projection["artifact_pending"]) is not bool or type(projection["stale_manifest"]) is not bool:
            raise ValueError("projection fields must be exact bool values")
        return ProjectionStatus(
            artifact_pending=projection["artifact_pending"],
            stale_manifest=projection["stale_manifest"],
        )
    raise ValueError("projection must be None, ProjectionStatus, or dict with exact bool fields")


class FirestoreProductionStore(ProductionStore):
    """
    Firestore-backed production store for production execution.

    Namespaces:
    - production_states/{production_id}
    - production_states/{production_id}/proposals/{proposal_id}
    - production_states/{production_id}/authorizations/{authorization_id}
    """

    def __init__(
        self,
        client: Any = None,
        transaction_runner: Callable[[Any, Callable[[Any], Any]], Any] | None = None,
        evidence_storage: EvidenceStorage | None = None,
    ) -> None:
        from creditlock.events.firestore_store import _default_transaction_runner

        self._client = client
        self._transaction_runner = transaction_runner or _default_transaction_runner
        self._evidence_storage = evidence_storage

    @property
    def evidence_storage(self) -> EvidenceStorage | None:
        if self._evidence_storage is None:
            try:
                from creditlock.evidence.storage import GCSStore
                from creditlock.settings import get_settings

                s = get_settings()
                if s.evidence_bucket:
                    self._evidence_storage = GCSStore(bucket_name=s.evidence_bucket)
            except Exception:  # noqa: BLE001
                return None
        return self._evidence_storage

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            from creditlock.settings import get_settings

            s = get_settings()
            if not s.google_cloud_project:
                raise ValueError("GOOGLE_CLOUD_PROJECT must be configured when STORE_BACKEND='firestore'.")
            self._client = firestore.Client(
                project=s.google_cloud_project,
                database=s.firestore_database,
            )
        return self._client

    def register(
        self,
        production_id: str,
        obligations: list[Obligation],
        manifest: CreditManifest,
        layout_evidence: LayoutEvidence | None = None,
        visual_observations: VisualObservations | None = None,
        contributor_registry: list[dict[str, Any]] | None = None,
        authorizations: list[Authorization] | None = None,
        projection: Any | None = None,
        frames: list[Any] | None = None,
        artifact_index: Any | None = None,
        render_profile_version: str | None = None,
    ) -> None:
        from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
        from creditlock.evidence.models import ArtifactIndex
        from creditlock.evidence.storage import validate_storage_path
        from creditlock.renderer.audit import audit_png_bytes
        from creditlock.renderer.models import FrameElement, FrameEvidence

        # 1. Validate contributor_registry
        if contributor_registry is not None and (
            not isinstance(contributor_registry, list)
            or any(not isinstance(c, dict) for c in contributor_registry)
        ):
            raise ValueError("contributor_registry must be a list of dictionaries.")

        # 2. Validate render_profile_version
        rp_ver = render_profile_version if render_profile_version is not None else "v1"
        if not isinstance(rp_ver, str) or not rp_ver.strip():
            raise ValueError("render_profile_version must be a non-empty string.")

        # 3. Validate projection
        validated_proj = _validate_and_normalize_projection(projection)
        proj_dict = {
            "artifact_pending": validated_proj.artifact_pending,
            "stale_manifest": validated_proj.stale_manifest,
        }

        # 4. Validate ArtifactIndex
        art_index_dict: dict[str, Any] | None = None
        art_digest: str | None = None
        validated_idx: ArtifactIndex | None = None
        if artifact_index is not None:
            if isinstance(artifact_index, ArtifactIndex):
                validated_idx = artifact_index
            elif isinstance(artifact_index, dict):
                try:
                    validated_idx = ArtifactIndex.model_validate(artifact_index)
                except Exception as e:
                    raise ValueError(f"invalid ArtifactIndex: {e}") from e
            else:
                raise ValueError(
                    "invalid ArtifactIndex: expected ArtifactIndex model or dictionary."
                )

            art_index_dict = validated_idx.model_dump()
            art_digest = sha256_bytes_digest(canonical_json_bytes(art_index_dict))

        # 5. Validate and persist frame evidence if present
        persisted_frames: list[dict[str, Any]] = []
        if frames:
            storage = self.evidence_storage
            if storage is None:
                raise ValueError(
                    "Frame evidence storage requires configured EvidenceStorage / GCS bucket."
                )
            if not isinstance(frames, list):
                raise ValueError("frames must be a list.")
            if validated_idx is not None and len(validated_idx.frames) != len(frames):
                raise ValueError(
                    f"Frame count mismatch: artifact_index has {len(validated_idx.frames)} frames, "
                    f"but frames list has {len(frames)}."
                )

            seen_frame_ids: set[str] = set()
            for idx, frame in enumerate(frames):
                if isinstance(frame, FrameEvidence):
                    fe = frame
                elif isinstance(frame, dict):
                    try:
                        fe = FrameEvidence.model_validate(frame)
                    except Exception as e:
                        raise ValueError(f"Invalid FrameEvidence at index {idx}: {e}") from e
                else:
                    raise TypeError(
                        f"Frame at index {idx} must be a FrameEvidence instance or dict."
                    )

                if fe.frame_index != idx:
                    raise ValueError(
                        f"Non-contiguous frame_index at index {idx}: "
                        f"expected {idx}, got {fe.frame_index}."
                    )
                if not fe.frame_id or not isinstance(fe.frame_id, str):
                    raise ValueError(f"Frame at index {idx} has invalid frame_id.")
                if fe.frame_id in seen_frame_ids:
                    raise ValueError(f"Duplicate frame_id '{fe.frame_id}' at index {idx}.")
                seen_frame_ids.add(fe.frame_id)

                if not isinstance(fe.image_bytes, bytes) or len(fe.image_bytes) == 0:
                    raise ValueError(f"Missing PNG image bytes for frame {idx}.")

                computed_hash = sha256_bytes_digest(fe.image_bytes)
                if computed_hash != fe.image_hash:
                    raise ValueError(
                        f"Frame {idx} ({fe.frame_id}) image_hash mismatch: "
                        f"expected {computed_hash}, got {fe.image_hash}."
                    )

                valid, reason, audited_w, audited_h = audit_png_bytes(
                    fe.image_bytes,
                    expected_width=fe.width,
                    expected_height=fe.height,
                )
                if not valid:
                    raise ValueError(f"Frame {idx} ({fe.frame_id}) failed PNG audit: {reason}")
                if audited_w != fe.width or audited_h != fe.height:
                    raise ValueError(
                        f"Frame {idx} ({fe.frame_id}) dimensions mismatch: "
                        f"audited ({audited_w}x{audited_h}), expected ({fe.width}x{fe.height})."
                    )

                if validated_idx is not None:
                    idx_frame = validated_idx.frames[idx]
                    idx_fid = idx_frame.get("frame_id")
                    idx_sha = idx_frame.get("sha256")
                    if idx_fid != fe.frame_id:
                        raise ValueError(
                            f"artifact_index frame_id mismatch at index {idx}: "
                            f"expected '{idx_fid}', got '{fe.frame_id}'."
                        )
                    if idx_sha != fe.image_hash:
                        raise ValueError(
                            f"artifact_index sha256 mismatch at index {idx}: "
                            f"expected '{idx_sha}', got '{fe.image_hash}'."
                        )

                storage_path = f"productions/{production_id}/frames/{fe.frame_id}/{computed_hash}.png"
                validate_storage_path(storage_path)

                storage.put_verified(
                    path=storage_path,
                    data=fe.image_bytes,
                    expected_sha256=computed_hash,
                    if_generation_match=0,
                )

                elements_data: list[dict[str, Any]] = []
                for el in fe.elements:
                    if isinstance(el, FrameElement):
                        elements_data.append(el.model_dump())
                    elif isinstance(el, dict):
                        elements_data.append(FrameElement.model_validate(el).model_dump())
                    else:
                        raise TypeError(f"Invalid frame element in frame {idx}: {el}")

                persisted_frames.append(
                    {
                        "frame_index": fe.frame_index,
                        "frame_id": fe.frame_id,
                        "image_hash": fe.image_hash,
                        "width": fe.width,
                        "height": fe.height,
                        "elements": elements_data,
                        "storage_path": storage_path,
                    }
                )

        prod_data = {
            "obligations": [o.model_dump() for o in obligations],
            "manifest": manifest.model_dump(),
            "layout_evidence": layout_evidence.model_dump() if layout_evidence else None,
            "visual_observations": visual_observations.model_dump()
            if visual_observations
            else None,
            "contributor_registry": contributor_registry or [],
            "projection": proj_dict,
            "frames": persisted_frames,
            "artifact_index": art_index_dict,
            "artifact_index_digest": art_digest,
            "render_profile_version": rp_ver,
        }

        ref = self.client.collection("production_states").document(production_id)
        ref.set(prod_data)

        if authorizations:
            auths_col = ref.collection("authorizations")
            for auth in authorizations:
                auth_dict = auth.model_dump() if hasattr(auth, "model_dump") else auth
                if isinstance(auth_dict, dict):
                    auth_id = auth_dict.get("authorization_id")
                    if auth_id:
                        auths_col.document(auth_id).set(auth_dict)

    def get(self, production_id: str) -> dict[str, Any] | None:
        from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
        from creditlock.domain.models import Authorization, Proposal
        from creditlock.evidence.models import ArtifactIndex

        ref = self.client.collection("production_states").document(production_id)
        doc = ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if data is None or not isinstance(data, dict):
            raise ValueError(
                f"Malformed production state in Firestore for production '{production_id}': document body is not a valid dictionary."
            )

        # 9 Required keys in persisted production_states document
        required_keys = {
            "obligations",
            "manifest",
            "layout_evidence",
            "visual_observations",
            "contributor_registry",
            "projection",
            "artifact_index",
            "artifact_index_digest",
            "render_profile_version",
        }
        missing_keys = required_keys - set(data.keys())
        if missing_keys:
            raise ValueError(
                f"Malformed production state in Firestore for production '{production_id}': missing required key(s) {sorted(missing_keys)}"
            )

        # Load proposals subcollection strictly
        props_col = ref.collection("proposals")
        prop_docs = props_col.stream() if hasattr(props_col, "stream") else props_col.get()
        proposals: list[Proposal] = []
        raw_props: list[dict[str, Any]] = []
        for pdoc in prop_docs:
            pd = pdoc.to_dict() if hasattr(pdoc, "to_dict") else pdoc
            if pd is None or not isinstance(pd, dict) or not pd:
                raise ValueError(
                    f"Malformed production state in Firestore for production '{production_id}': proposal document is not a non-empty dict"
                )
            raw_props.append(pd)
        raw_props.sort(key=lambda x: (x.get("created_at", ""), x.get("proposal_id", "")))
        for pd in raw_props:
            try:
                proposals.append(Proposal.model_validate(pd))
            except Exception as e:
                raise ValueError(
                    f"Malformed production state in Firestore for production '{production_id}': invalid proposal: {e}"
                ) from e

        # Load authorizations subcollection strictly
        auths_col = ref.collection("authorizations")
        auth_docs = auths_col.stream() if hasattr(auths_col, "stream") else auths_col.get()
        authorizations: list[Authorization] = []
        raw_auths: list[dict[str, Any]] = []
        for adoc in auth_docs:
            ad = adoc.to_dict() if hasattr(adoc, "to_dict") else adoc
            if ad is None or not isinstance(ad, dict) or not ad:
                raise ValueError(
                    f"Malformed production state in Firestore for production '{production_id}': authorization document is not a non-empty dict"
                )
            raw_auths.append(ad)
        raw_auths.sort(key=lambda x: (x.get("authorized_at", ""), x.get("authorization_id", "")))
        for ad in raw_auths:
            try:
                authorizations.append(Authorization.model_validate(ad))
            except Exception as e:
                raise ValueError(
                    f"Malformed production state in Firestore for production '{production_id}': invalid authorization: {e}"
                ) from e

        try:
            # 1. obligations
            raw_obls = data["obligations"]
            if not isinstance(raw_obls, list):
                raise TypeError("obligations field must be a list")
            obligations = [Obligation.model_validate(o) for o in raw_obls]

            # 2. manifest
            raw_manifest = data["manifest"]
            if not isinstance(raw_manifest, dict):
                raise TypeError("manifest field must be a dict")
            manifest = CreditManifest.model_validate(raw_manifest)

            # 3. layout_evidence (optional, must be None or valid model)
            raw_le = data["layout_evidence"]
            layout_evidence = (
                LayoutEvidence.model_validate(raw_le) if raw_le is not None else None
            )

            # 4. visual_observations (optional, must be None or valid model)
            raw_vo = data["visual_observations"]
            visual_observations = (
                VisualObservations.model_validate(raw_vo) if raw_vo is not None else None
            )

            # 5. contributor_registry
            contributor_registry = data["contributor_registry"]
            if not isinstance(contributor_registry, list) or any(
                not isinstance(c, dict) for c in contributor_registry
            ):
                raise ValueError("contributor_registry must be a list of dicts")

            # 6. render_profile_version
            render_profile_version = data["render_profile_version"]
            if not isinstance(render_profile_version, str) or not render_profile_version.strip():
                raise ValueError("render_profile_version must be a non-empty, non-whitespace string")

            # 7. projection
            raw_proj = data["projection"]
            projection = _validate_and_normalize_projection(raw_proj)

            # 8 & 9. artifact_index and artifact_index_digest
            raw_art_idx = data["artifact_index"]
            raw_art_digest = data["artifact_index_digest"]
            artifact_index: ArtifactIndex | None = None
            artifact_index_digest: str | None = None

            if raw_art_idx is not None:
                if isinstance(raw_art_idx, ArtifactIndex):
                    artifact_index = raw_art_idx
                elif isinstance(raw_art_idx, dict):
                    artifact_index = ArtifactIndex.model_validate(raw_art_idx)
                else:
                    raise ValueError("artifact_index must be an ArtifactIndex model or dict")

                if (
                    not isinstance(raw_art_digest, str)
                    or len(raw_art_digest) != 64
                    or not all(c in "0123456789abcdef" for c in raw_art_digest)
                ):
                    raise ValueError("artifact_index_digest must be a 64-character lowercase hex string")

                expected_digest = sha256_bytes_digest(canonical_json_bytes(artifact_index.model_dump(mode="json")))
                if raw_art_digest != expected_digest:
                    raise ValueError("artifact_index_digest mismatch")
                artifact_index_digest = raw_art_digest
            else:
                if raw_art_digest is not None:
                    raise ValueError("artifact_index_digest must be None when artifact_index is None")

            # 10. frames
            raw_frames = data.get("frames")
            rehydrated_frames: list[FrameEvidence] = []
            if raw_frames is not None:
                if not isinstance(raw_frames, list):
                    raise ValueError("frames field must be a list")
                if raw_frames:
                    storage = self.evidence_storage
                    if storage is None:
                        raise ValueError(
                            "cannot rehydrate frames without configured evidence storage backend"
                        )
                    if artifact_index is not None and len(artifact_index.frames) != len(raw_frames):
                        raise ValueError(
                            f"Frame count mismatch: artifact_index has {len(artifact_index.frames)} frames, "
                            f"but stored state has {len(raw_frames)} frames."
                        )

                    from creditlock.evidence.storage import validate_storage_path
                    from creditlock.renderer.audit import audit_png_bytes
                    from creditlock.renderer.models import FrameElement, FrameEvidence

                    seen_frame_ids: set[str] = set()
                    for idx, f_meta in enumerate(raw_frames):
                        if not isinstance(f_meta, dict):
                            raise TypeError(f"Frame metadata at index {idx} must be a dict.")

                        frame_idx = f_meta.get("frame_index")
                        if frame_idx != idx:
                            raise ValueError(
                                f"Non-contiguous frame_index at index {idx}: "
                                f"expected {idx}, got {frame_idx}."
                            )

                        frame_id = f_meta.get("frame_id")
                        if not isinstance(frame_id, str) or not frame_id:
                            raise ValueError(f"Invalid or missing frame_id at index {idx}.")
                        if frame_id in seen_frame_ids:
                            raise ValueError(f"Duplicate frame_id '{frame_id}' at index {idx}.")
                        seen_frame_ids.add(frame_id)

                        image_hash = f_meta.get("image_hash")
                        if (
                            not isinstance(image_hash, str)
                            or len(image_hash) != 64
                            or not all(c in "0123456789abcdef" for c in image_hash)
                        ):
                            raise ValueError(f"Invalid image_hash for frame {idx} ({frame_id}).")

                        width = f_meta.get("width")
                        height = f_meta.get("height")
                        if (
                            not isinstance(width, int)
                            or width <= 0
                            or not isinstance(height, int)
                            or height <= 0
                        ):
                            raise ValueError(
                                f"Invalid dimensions ({width}x{height}) for frame {idx} ({frame_id})."
                            )

                        storage_path = f_meta.get("storage_path")
                        if not isinstance(storage_path, str) or not storage_path:
                            raise ValueError(f"Missing storage_path for frame {idx} ({frame_id}).")
                        validate_storage_path(storage_path)

                        if artifact_index is not None:
                            idx_frame = artifact_index.frames[idx]
                            idx_frame_id = idx_frame.get("frame_id")
                            idx_hash = idx_frame.get("sha256")
                            if idx_frame_id != frame_id:
                                raise ValueError(
                                    f"Stale artifact_index: frame_id mismatch at index {idx} "
                                    f"('{idx_frame_id}' != '{frame_id}')."
                                )
                            if idx_hash != image_hash:
                                raise ValueError(
                                    f"Stale artifact_index: hash mismatch at index {idx} "
                                    f"('{idx_hash}' != '{image_hash}')."
                                )

                        png_bytes = storage.get(storage_path)
                        if png_bytes is None:
                            raise ValueError(
                                f"Corrupt or missing frame evidence at '{storage_path}'."
                            )

                        computed_hash = sha256_bytes_digest(png_bytes)
                        if computed_hash != image_hash:
                            raise ValueError(
                                f"Corrupt frame evidence: content digest mismatch for "
                                f"frame {idx} ({frame_id})."
                            )

                        valid, reason, audited_w, audited_h = audit_png_bytes(
                            png_bytes,
                            expected_width=width,
                            expected_height=height,
                        )
                        if not valid:
                            raise ValueError(f"Frame {idx} ({frame_id}) failed PNG audit: {reason}")
                        if audited_w != width or audited_h != height:
                            raise ValueError(
                                f"Frame {idx} ({frame_id}) dimension mismatch: "
                                f"audited ({audited_w}x{audited_h}) != stored ({width}x{height})"
                            )

                        raw_elements = f_meta.get("elements", [])
                        if not isinstance(raw_elements, list):
                            raise TypeError(
                                f"Elements for frame {idx} ({frame_id}) must be a list."
                            )

                        elements = [
                            el if isinstance(el, FrameElement) else FrameElement.model_validate(el)
                            for el in raw_elements
                        ]

                        rehydrated_frames.append(
                            FrameEvidence(
                                frame_index=frame_idx,
                                frame_id=frame_id,
                                image_bytes=png_bytes,
                                image_hash=image_hash,
                                width=width,
                                height=height,
                                elements=elements,
                            )
                        )

            # 11 & 12. release_digest and delivery_package_path (optional before export)
            raw_release_digest = data.get("release_digest")
            raw_delivery_path = data.get("delivery_package_path")
            validated_release_digest: str | None = None
            validated_delivery_path: str | None = None

            if (raw_release_digest is None) != (raw_delivery_path is None):
                raise ValueError(
                    "release_digest and delivery_package_path must both be present or both absent."
                )
            if raw_release_digest is not None:
                if (
                    not isinstance(raw_release_digest, str)
                    or len(raw_release_digest) != 64
                    or not all(c in "0123456789abcdef" for c in raw_release_digest)
                ):
                    raise ValueError("release_digest must be a 64-character lowercase hex string")
                if not isinstance(raw_delivery_path, str) or not raw_delivery_path.strip():
                    raise ValueError("delivery_package_path must be a non-empty string")
                validated_release_digest = raw_release_digest
                validated_delivery_path = raw_delivery_path

            result_dict: dict[str, Any] = {
                "obligations": obligations,
                "manifest": manifest,
                "layout_evidence": layout_evidence,
                "visual_observations": visual_observations,
                "contributor_registry": contributor_registry,
                "proposals": proposals,
                "authorizations": authorizations,
                "projection": projection,
                "frames": rehydrated_frames,
                "artifact_index": artifact_index,
                "artifact_index_digest": artifact_index_digest,
                "render_profile_version": render_profile_version,
            }
            if validated_release_digest is not None:
                result_dict["release_digest"] = validated_release_digest
            if validated_delivery_path is not None:
                result_dict["delivery_package_path"] = validated_delivery_path

            return result_dict
        except Exception as e:
            raise ValueError(
                f"Malformed production state in Firestore for production '{production_id}': {e}"
            ) from e

    def clear(self) -> None:
        """Global Firestore clearing is disabled to prevent accidental data destruction."""
        raise RuntimeError("Global Firestore clearing is disabled.")

    def save_proposal(self, proposal: Any) -> None:
        prod_ref = self.client.collection("production_states").document(proposal.production_id)
        prop_id = proposal.proposal_id
        prop_ref = prod_ref.collection("proposals").document(prop_id)
        p_dump = proposal.model_dump() if hasattr(proposal, "model_dump") else proposal

        def _txn_save(txn: Any) -> None:
            prod_snap = prod_ref.get(transaction=txn)
            if not prod_snap.exists:
                raise KeyError(f"Production '{proposal.production_id}' is not registered.")

            prop_snap = prop_ref.get(transaction=txn)
            if prop_snap.exists:
                existing = prop_snap.to_dict() or {}
                if existing == p_dump:
                    return
                raise ValueError(f"Proposal '{prop_id}' already exists with different data.")

            txn.set(prop_ref, p_dump)

        self._transaction_runner(self.client, _txn_save)

    def get_proposals(self, production_id: str) -> list[Any]:
        from creditlock.domain.models import Proposal

        prod_ref = self.client.collection("production_states").document(production_id)
        if not prod_ref.get().exists:
            return []

        props_col = prod_ref.collection("proposals")
        docs = props_col.stream() if hasattr(props_col, "stream") else props_col.get()
        raw_props: list[dict[str, Any]] = []
        for doc in docs:
            d = doc.to_dict() if hasattr(doc, "to_dict") else doc
            if d and isinstance(d, dict):
                raw_props.append(d)
        raw_props.sort(key=lambda x: (x.get("created_at", ""), x.get("proposal_id", "")))
        return [Proposal.model_validate(p) for p in raw_props]

    def confirm_proposal(
        self,
        production_id: str,
        proposal_id: str,
        approver_id: str,
        current_hashes: tuple[str, str, str, str],
    ) -> Authorization:
        prod_ref = self.client.collection("production_states").document(production_id)
        prop_ref = prod_ref.collection("proposals").document(proposal_id)

        def _txn_confirm(txn: Any) -> Authorization:
            prod_snap = prod_ref.get(transaction=txn)
            if not prod_snap.exists:
                raise KeyError(f"Production '{production_id}' not found in Firestore.")

            prop_snap = prop_ref.get(transaction=txn)
            if not prop_snap.exists:
                raise ValueError(f"Proposal '{proposal_id}' not found.")

            prop_dict = prop_snap.to_dict() or {}
            if prop_dict.get("status") != "PENDING":
                raise ValueError(f"Proposal '{proposal_id}' is not pending (status: {prop_dict.get('status')}).")

            if approver_id == prop_dict.get("proposer_id"):
                raise ValueError("Proposer and approver identities must be distinct.")

            m_hash, orv_hash, art_digest, vis_hash = current_hashes
            if (
                prop_dict.get("manifest_hash") != m_hash
                or prop_dict.get("obligation_registry_version_hash") != orv_hash
                or prop_dict.get("artifact_index_digest") != art_digest
                or prop_dict.get("visual_observations_hash") != vis_hash
            ):
                raise ValueError("Stale proposal: production state hashes have changed.")

            import uuid
            from datetime import UTC, datetime

            now_iso = datetime.now(UTC).isoformat()
            auth_id = f"auth-{uuid.uuid4().hex[:12]}"
            auth = Authorization(
                authorization_id=auth_id,
                production_id=production_id,
                proposal_id=proposal_id,
                issue_id=prop_dict["issue_id"],
                proposer_id=prop_dict["proposer_id"],
                actor_id=approver_id,
                role="RELEASE_APPROVER",
                action=prop_dict["action"],
                reason=prop_dict["reason"],
                selected_contributor_id=prop_dict.get("selected_contributor_id"),
                manifest_hash=prop_dict["manifest_hash"],
                obligation_registry_version_hash=prop_dict["obligation_registry_version_hash"],
                artifact_index_digest=prop_dict["artifact_index_digest"],
                visual_observations_hash=prop_dict["visual_observations_hash"],
                authorized_at=now_iso,
            )

            updated_prop = dict(prop_dict)
            updated_prop["status"] = "CONFIRMED"
            txn.set(prop_ref, updated_prop)

            auth_ref = prod_ref.collection("authorizations").document(auth_id)
            txn.set(auth_ref, auth.model_dump())

            return auth

        try:
            res: Authorization = self._transaction_runner(self.client, _txn_confirm)
            return res
        except ValueError as err:
            normalized_status: str | None = None
            try:
                latest_snap = prop_ref.get()
                if latest_snap.exists:
                    latest_dict = latest_snap.to_dict()
                    if isinstance(latest_dict, dict) and latest_dict:
                        from creditlock.domain.models import Proposal

                        validated_prop = Proposal.model_validate(latest_dict)
                        if validated_prop.status in ("CONFIRMED", "REJECTED"):
                            normalized_status = str(validated_prop.status)
            except Exception:  # noqa: BLE001, S110
                pass

            if normalized_status is not None:
                raise ValueError(
                    f"Proposal '{proposal_id}' is not pending (status: {normalized_status})."
                ) from err
            raise

    def get_authorizations(self, production_id: str) -> list[Authorization]:
        prod_ref = self.client.collection("production_states").document(production_id)
        if not prod_ref.get().exists:
            return []

        auths_col = prod_ref.collection("authorizations")
        docs = auths_col.stream() if hasattr(auths_col, "stream") else auths_col.get()
        raw_auths: list[dict[str, Any]] = []
        for doc in docs:
            d = doc.to_dict() if hasattr(doc, "to_dict") else doc
            if d and isinstance(d, dict):
                raw_auths.append(d)
        raw_auths.sort(key=lambda x: (x.get("authorized_at", ""), x.get("authorization_id", "")))
        return [Authorization.model_validate(a) for a in raw_auths]

    def save_export_result(
        self,
        production_id: str,
        release_digest: str,
        delivery_package_path: str,
    ) -> None:
        if (
            not isinstance(release_digest, str)
            or len(release_digest) != 64
            or not all(c in "0123456789abcdef" for c in release_digest)
        ):
            raise ValueError("release_digest must be a 64-character lowercase hex SHA-256 digest.")
        if not isinstance(delivery_package_path, str) or not delivery_package_path.strip():
            raise ValueError("delivery_package_path must be a non-empty string.")

        prod_ref = self.client.collection("production_states").document(production_id)

        def _txn_save_export(txn: Any) -> None:
            prod_snap = prod_ref.get(transaction=txn)
            if not prod_snap.exists:
                raise KeyError(f"Production '{production_id}' not found in Firestore.")

            data = prod_snap.to_dict() or {}
            existing_digest = data.get("release_digest")
            existing_path = data.get("delivery_package_path")

            if existing_digest is not None or existing_path is not None:
                if existing_digest == release_digest and existing_path == delivery_package_path:
                    return
                raise ValueError(
                    f"Production '{production_id}' already has conflicting export metadata: "
                    f"existing=({existing_digest!r}, {existing_path!r}), "
                    f"new=({release_digest!r}, {delivery_package_path!r})"
                )

            txn.update(
                prod_ref,
                {
                    "release_digest": release_digest,
                    "delivery_package_path": delivery_package_path,
                },
            )

        self._transaction_runner(self.client, _txn_save_export)
