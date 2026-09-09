"""
Renderer data models and render profile definitions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from creditlock.domain.models import LayoutEvidence


class RenderProfile(BaseModel):
    """Pinned render environment profile."""

    model_config = {"extra": "forbid"}

    profile_version: str = Field(default="1.0")
    viewport_width: int = Field(default=1920)
    viewport_height: int = Field(default=1080)
    device_scale_factor: float = Field(default=1.0)
    font_family: str = Field(default="Inter, Roboto, sans-serif")
    size_comparison_basis: str = Field(default="COMPUTED_FONT_SIZE_PX")


class FrameElement(BaseModel):
    """Layout element captured from rendered DOM."""

    model_config = {"extra": "forbid"}

    element_id: str
    text: str
    computed_font_size_px: float
    bounding_box: dict[str, float]


class FrameEvidence(BaseModel):
    """Per-frame layout and render evidence."""

    model_config = {"extra": "forbid"}

    frame_index: int
    frame_id: str
    image_bytes: bytes
    image_hash: str
    width: int
    height: int
    elements: list[FrameElement]


class RenderResult(BaseModel):
    """Complete output of renderer service."""

    model_config = {"extra": "forbid"}

    frames: list[FrameEvidence]
    layout_evidence: LayoutEvidence
    artifact_index: dict[str, Any]
    artifact_index_digest: str
    render_profile: RenderProfile
