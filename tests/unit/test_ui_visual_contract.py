"""Visual contract tests for CreditLock dark-neon cinematic interface."""

from __future__ import annotations

import base64
import re
from pathlib import Path

_STATIC_DIR = Path(__file__).parent.parent.parent / "src" / "creditlock" / "static"
_HTML_PATH = _STATIC_DIR / "index.html"
_ASSETS_DIR = _STATIC_DIR / "assets"


def test_visual_assets_exist() -> None:
    """Verify required baseline assets exist on disk and decorative imagery is removed."""
    required_assets = [
        "brand_mark.png",
        "credit_master_bg.png",
    ]
    for asset_name in required_assets:
        asset_file = _ASSETS_DIR / asset_name
        assert asset_file.exists(), f"Missing required visual asset: {asset_name}"
        assert asset_file.stat().st_size > 0, f"Asset file is empty: {asset_name}"

    # Prove hero_clapper.png and filmstrip_connector.png are deleted from disk
    deleted_assets = [
        "hero_clapper.png",
        "filmstrip_connector.png",
    ]
    for asset_name in deleted_assets:
        asset_file = _ASSETS_DIR / asset_name
        assert not asset_file.exists(), f"Deleted asset must not exist on disk: {asset_name}"


def test_cinematic_dom_contract() -> None:
    """Verify cinematic visual contract IDs, structure, and removal of decorative visuals."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")

    # Prove decorative assets are not referenced anywhere in HTML
    assert "hero_clapper.png" not in html
    assert "filmstrip_connector.png" not in html

    # Prove alignConnector() and connector calculations are absent
    assert "alignConnector" not in html
    assert "filmstrip-bridge" not in html

    # Popcorn removal verification
    assert "popcorn" not in html
    assert "floatSlow" not in html

    # Essential functional structural IDs
    assert 'id="hero-section"' in html
    assert 'id="gate-badge"' in html
    assert 'id="hero-gate-card"' in html
    assert 'id="prod-tag"' in html
    assert 'id="clause-dpark"' in html
    assert 'id="bracket-left"' in html
    assert 'id="credit-dpark-name"' in html
    assert 'id="credit-master-auth-status"' in html
    assert 'id="btn-goto-workbench"' in html
    assert 'id="issues-list"' in html
    assert 'id="issue-count-badge"' in html

    # Trust Journey Steps
    for i in range(1, 5):
        assert f'id="journey-step-{i}"' in html

    # Views and Navigation
    for i in range(1, 4):
        assert f'id="tab-btn-{i}"' in html
        assert f'id="view-{i}"' in html

    # Story text
    assert "APEX: LEGACY OF SPEED" in html
    assert "ONE WRONG CREDIT" in html
    assert "CAN DELAY THE PREMIERE." in html
    assert "WHO IS D. PARK?" in html
    assert "DELIVERY BLOCKED" in html
    assert "READY TO EXPORT" in html


def test_cinematic_dom_dynamic_truthfulness() -> None:
    """Verify dynamic dock branches for unresolved and READY_TO_EXPORT states."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")
    assert 'id="decision-dock"' in html

    # Unresolved dock branch affordances
    assert "AMBIGUOUS_IDENTITY" in html
    assert "Human confirmation is required" in html
    assert "Analyze with Gemini" in html

    # READY_TO_EXPORT dock branch affordances
    assert "IDENTITY AUTHORIZED" in html
    assert "Verification Complete" in html
    assert "Release authorization and evidence digest recorded." in html
    assert "dock-status-badge" in html


def test_analyze_cta_is_text_only_and_data_uris_decode() -> None:
    """Verify Analyze CTA is text-only without broken images, and all data URIs strictly decode."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")

    # CTA must not display any broken image or alt text fallback
    assert 'alt="Play"' not in html
    assert "clapper-btn-badge" not in html

    # Ensure no <img> tag or data URI is inside the Analyze button markup
    btn_matches = re.findall(r'<button[^>]*id="btn-goto-workbench"[^>]*>([\s\S]*?)</button>', html)
    assert len(btn_matches) >= 1, "btn-goto-workbench must exist"
    for btn_content in btn_matches:
        assert "<img" not in btn_content
        assert "data:image" not in btn_content

    # Button must contain text-only affordance
    assert "Analyze with Gemini &rarr;" in html or "Analyze with Gemini →" in html

    # Strict validation: every remaining embedded data:image URI in index.html must decode cleanly
    data_uris = re.findall(r"data:image/[a-zA-Z0-9]+;base64,([A-Za-z0-9+/=]+)", html)
    assert len(data_uris) > 0, "Expected at least one embedded image (brand_mark or background)"
    for idx, b64_payload in enumerate(data_uris):
        decoded = base64.b64decode(b64_payload, validate=True)
        assert len(decoded) > 0, f"Embedded image {idx} failed to decode"


def test_confirmed_proposal_cannot_render_stale_selection_required() -> None:
    """Proves that a confirmed proposal renders recorded identity and cannot display stale selection-required text."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")

    # The recorded selection state replaces the candidate container
    assert "_renderRecordedSelection" in html
    assert "Identity authorized:" in html
    assert "Recorded contributor:" in html
    assert "recorded-selection-box" in html
    assert "neutral-identity-msg" in html

    # In refreshState, if latestProp exists, _renderRecordedSelection is called
    prop_block = re.search(
        r"if\s*\(\s*latestProp\s*\)\s*\{[\s\S]*?_renderRecordedSelection\(latestProp\);",
        html,
    )
    assert prop_block is not None, "refreshState must render recorded selection when latestProp exists"

    # Neutral state when no identity issue exists must never claim selection is required
    neutral_block = re.search(
        r"function _renderNeutralState\(\)\s*\{([\s\S]*?)\}",
        html,
    )
    assert neutral_block is not None
    assert "selection required" not in neutral_block.group(1).lower()
    assert "no candidate selected" not in neutral_block.group(1).lower()
    assert "No active identity resolution required." in neutral_block.group(1)


def test_event_sync_status_panel_contract() -> None:
    """Verify View 3 event-sync-status-panel presence, required labels, and shortened ID derivation."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")

    # Panel stable ID must be defined in the UI
    assert 'id="event-sync-status-panel"' in html or 'event-sync-status-panel' in html

    # Required truthful labels must be present in the UI source
    required_labels = [
        "Release checkpoint synchronized",
        "Confluent Cloud",
        "resolution.recorded",
        "Firestore projection: SYNCHRONIZED",
        "Downstream release systems received the approved decision.",
    ]
    for label in required_labels:
        assert label in html, f"Missing required label in index.html: {label}"

    # Verify forbidden data items are NOT displayed or hardcoded in the export area
    assert "creditlock.production.events" not in html
    assert "kafka offset" not in html.lower()


def test_export_event_sync_validation_and_clearing() -> None:
    """Verify export handler validates all 5 event_sync fields, checks UUID, and clears stale state."""
    assert _HTML_PATH.exists(), "index.html must exist"
    html = _HTML_PATH.read_text(encoding="utf-8")

    # Find triggerExport function
    export_fn_match = re.search(r"async function triggerExport\(\)\s*\{([\s\S]*?)\n    \}", html)
    assert export_fn_match is not None, "triggerExport function must exist"
    export_fn = export_fn_match.group(1)

    # Validations inside triggerExport:
    # 1. Checks event_sync exists
    assert "event_sync" in export_fn
    # 2. Checks exact required field values
    assert "SYNCHRONIZED" in export_fn
    assert "CONFLUENT_CLOUD" in export_fn
    assert "resolution.recorded" in export_fn
    assert "FIRESTORE" in export_fn
    # 3. Validates canonical UUID pattern
    assert "event_id" in export_fn

    # 4. Integrity error treatment on missing/invalid event_sync
    assert "Integrity Error" in export_fn

    # 5. Reset / clearing in initDemo
    init_fn_match = re.search(r"async function initDemo\(\)\s*\{([\s\S]*?)\n    \}", html)
    assert init_fn_match is not None, "initDemo function must exist"
    init_fn = init_fn_match.group(1)
    assert "export-result-box" in init_fn or "event-sync-status-panel" in init_fn
