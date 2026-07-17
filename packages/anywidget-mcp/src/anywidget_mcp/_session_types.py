from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._state import ProjectionUpdate


@dataclass(frozen=True)
class SessionSnapshot:
    messages: list[dict[str, Any]]
    models: dict[str, dict[str, Any]]
    asset_manifest: dict[str, dict[str, Any]]
    removed_model_ids: list[str]
    projection: ProjectionUpdate | None
    projection_error: str | None


def empty_snapshot() -> SessionSnapshot:
    return SessionSnapshot(
        messages=[],
        models={},
        asset_manifest={},
        removed_model_ids=[],
        projection=None,
        projection_error=None,
    )
