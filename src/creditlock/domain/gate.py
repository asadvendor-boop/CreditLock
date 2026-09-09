"""
Deterministic gate fold.

fold_gate takes the current open issue list and a ProjectionStatus and returns
the single highest-precedence GateState. No AI, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from creditlock.domain.models import GATE_PRECEDENCE, ISSUE_GATE_MAP, GateState, Issue


@dataclass
class ProjectionStatus:
    """Readiness flags from the Firestore projection, separate from issue codes."""

    artifact_pending: bool = False
    stale_manifest: bool = False


def fold_gate(issues: list[Issue], projection: ProjectionStatus) -> GateState:
    """Fold all open issues and projection flags into the single highest-precedence gate state."""
    states: list[GateState] = []

    # Contributions from open issues
    for issue in issues:
        states.append(ISSUE_GATE_MAP[issue.code])

    # Contributions from projection status flags
    if projection.artifact_pending:
        states.append(GateState.STALE)
    if projection.stale_manifest:
        states.append(GateState.STALE)

    if not states:
        return GateState.READY_TO_EXPORT

    # Return the highest-precedence state (lowest index in GATE_PRECEDENCE)
    return min(states, key=lambda s: GATE_PRECEDENCE.index(s))
