"""Define the snapshot record transferred from widget sessions to MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._state import ProjectionUpdate


@dataclass(frozen=True)
class SessionSnapshot:
    """One atomic batch of browser effects and model-visible state."""

    messages: list[dict[str, Any]]
    models: dict[str, dict[str, Any]]
    attachment_ids: tuple[str, ...]
    removed_model_ids: list[str]
    projection: ProjectionUpdate | None
    projection_error: str | None


def empty_snapshot() -> SessionSnapshot:
    return SessionSnapshot(
        messages=[],
        models={},
        attachment_ids=(),
        removed_model_ids=[],
        projection=None,
        projection_error=None,
    )
