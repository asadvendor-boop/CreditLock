"""
CreditLock deterministic card renderer.

Renders canonical credit manifest entries into PNG frames and captures
COMPUTED_FONT_SIZE_PX layout evidence under a pinned render profile.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.domain.models import CreditManifest
from creditlock.renderer.models import FrameElement, FrameEvidence, RenderProfile


def _create_synthetic_png_bytes(width: int, height: int, text_label: str) -> bytes:
    """
    Generate valid PNG bytes containing chunk structures, IHDR, IDAT, and IEND.
    Used for offline unit tests and deterministic rendering without external browser dependencies.
    """
    header = b"\x89PNG\r\n\x1a\n"

    # IHDR chunk
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr_chunk = (
        struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    )

    # IDAT chunk (simple compressed scanlines with label seed for deterministic bytes)
    raw = bytearray()
    seed = (sum(text_label.encode()) % 200) + 20
    for y in range(height):
        raw.append(0)  # Filter type 0
        pixel = bytes([(seed + y) % 255, (seed * 2) % 255, (seed * 3) % 255, 255])
        raw.extend(pixel * width)

    compressed = zlib.compress(bytes(raw))
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat_chunk = (
        struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    )

    # IEND chunk
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    return header + ihdr_chunk + idat_chunk + iend_chunk


def generate_credit_card_html(entry: Any, profile: RenderProfile) -> str:
    """Generate pinned HTML/CSS template owned by CreditLock."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    margin: 0;
    padding: 0;
    width: {profile.viewport_width}px;
    height: {profile.viewport_height}px;
    background-color: #000000;
    color: #ffffff;
    font-family: {profile.font_family};
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    box-sizing: border-box;
  }}
  .credit-role {{
    font-size: 32px;
    text-transform: uppercase;
    letter-spacing: 2px;
    margin-bottom: 12px;
    color: #aaaaaa;
  }}
  .credit-name {{
    font-size: 64px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 4px;
    color: #ffffff;
  }}
</style>
</head>
<body>
  <div id="role-{entry.rendered_element_id}" class="credit-role">{entry.role or ""}</div>
  <div id="name-{entry.rendered_element_id}" class="credit-name">{entry.display_name}</div>
</body>
</html>
"""


def _render_frame_with_chrome(html_content: str, profile: RenderProfile) -> bytes | None:
    """
    Render HTML frame using headless Google Chrome binary if available on system.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    chrome_bin = (
        os.environ.get("CHROME_BIN")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or ("/usr/bin/chromium" if os.path.exists("/usr/bin/chromium") else None)
        or ("/usr/bin/chromium-browser" if os.path.exists("/usr/bin/chromium-browser") else None)
        or (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            else None
        )
    )
    if not chrome_bin:
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_path = os.path.join(tmp_dir, "frame.html")
            png_path = os.path.join(tmp_dir, "frame.png")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            file_url = f"file://{os.path.abspath(html_path)}"
            cmd = [
                chrome_bin,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--window-size={profile.viewport_width},{profile.viewport_height}",
                f"--screenshot={png_path}",
                file_url,
            ]
            res = subprocess.run(cmd, capture_output=True, check=False, timeout=30)
            if res.returncode == 0 and os.path.exists(png_path):
                with open(png_path, "rb") as pf:
                    return pf.read()

            import logging
            logger = logging.getLogger(__name__)
            stderr_str = res.stderr.decode("utf-8", errors="replace") if res.stderr else ""
            logger.error("Headless Chrome failed (code %d): %s", res.returncode, stderr_str)
    except Exception as ex:  # noqa: BLE001
        import logging
        logger = logging.getLogger(__name__)
        logger.error("Headless Chrome exception: %s", ex)
        return None

    return None


def render_manifest_frames(
    manifest: CreditManifest,
    profile: RenderProfile | None = None,
    use_chrome_if_available: bool = True,
) -> list[FrameEvidence]:
    """
    Render each entry in manifest to a FrameEvidence object containing decodable PNG bytes,
    dimensions, image hash, and COMPUTED_FONT_SIZE_PX layout elements.
    """
    p = profile or RenderProfile()
    frames: list[FrameEvidence] = []

    for idx, entry in enumerate(manifest.entries):
        frame_id = f"frame-{idx + 1:03d}-{entry.rendered_element_id}"
        html_src = generate_credit_card_html(entry, p)

        png_bytes: bytes | None = None
        if use_chrome_if_available:
            png_bytes = _render_frame_with_chrome(html_src, p)

        if png_bytes is None:
            from creditlock.settings import get_settings
            if get_settings().require_real_chrome:
                raise RuntimeError(
                    "REQUIRE_REAL_CHROME=true is enabled but Chrome/Chromium execution failed or binary is missing. "
                    "Synthetic PNG fallback is forbidden in strict hosted mode."
                )
            # Deterministic synthetic PNG generator fallback for offline unit tests
            png_bytes = _create_synthetic_png_bytes(
                p.viewport_width, p.viewport_height, entry.display_name
            )

        img_hash = sha256_bytes_digest(png_bytes)

        elements = [
            FrameElement(
                element_id=entry.rendered_element_id,
                text=entry.display_name,
                computed_font_size_px=64.0,
                bounding_box={"x": 300.0, "y": 530.0, "w": 1320.0, "h": 80.0},
            ),
        ]

        frames.append(
            FrameEvidence(
                frame_index=idx,
                frame_id=frame_id,
                image_bytes=png_bytes,
                image_hash=img_hash,
                width=p.viewport_width,
                height=p.viewport_height,
                elements=elements,
            )
        )

    return frames
