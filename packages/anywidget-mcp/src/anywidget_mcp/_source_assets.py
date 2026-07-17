from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

MAX_ASSETS_PER_REQUEST = 128
ASSET_KINDS = {"_esm": "esm", "_css": "css"}
ASSET_ID_PATTERN = re.compile(r"^(esm|css):sha256:[0-9a-f]{64}$")


def _asset_id(kind: str, source: str) -> str:
    digest = hashlib.sha256(
        f"anywidget-mcp-asset-v1\0{kind}\0{source}".encode("utf-8")
    ).hexdigest()
    return f"{kind}:sha256:{digest}"


class SourceAssets:
    """Own content-addressed widget sources and their replay references."""

    def __init__(self) -> None:
        self._assets: dict[str, tuple[str, int, str]] = {}
        self._model_source_refs: dict[str, dict[str, str]] = {}
        self._latest_asset_ids: set[str] = set()
        self._pinned_asset_refs: dict[str, int] = {}

    def contents(self, asset_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        requested = tuple(dict.fromkeys(asset_ids))
        if len(requested) > MAX_ASSETS_PER_REQUEST:
            raise ValueError(
                f"A widget asset request may contain at most "
                f"{MAX_ASSETS_PER_REQUEST} IDs"
            )
        invalid = next(
            (
                asset_id
                for asset_id in requested
                if not ASSET_ID_PATTERN.fullmatch(asset_id)
            ),
            None,
        )
        if invalid is not None:
            raise ValueError(f"Invalid widget asset ID: {invalid}")

        contents: dict[str, dict[str, Any]] = {}
        for asset_id in requested:
            asset = self._assets.get(asset_id)
            if asset is None:
                raise KeyError(f"Unknown widget asset: {asset_id}")
            kind, byte_length, text = asset
            contents[asset_id] = {
                "kind": kind,
                "byteLength": byte_length,
                "text": text,
            }
        return contents

    def pin(self, asset_ids: Iterable[str]) -> tuple[str, ...]:
        pinned = tuple(dict.fromkeys(asset_ids))
        missing = next(
            (asset_id for asset_id in pinned if asset_id not in self._assets),
            None,
        )
        if missing is not None:
            raise KeyError(f"Unknown widget asset: {missing}")
        for asset_id in pinned:
            self._pinned_asset_refs[asset_id] = (
                self._pinned_asset_refs.get(asset_id, 0) + 1
            )
        return pinned

    def release(self, asset_ids: Iterable[str]) -> None:
        for asset_id in dict.fromkeys(asset_ids):
            count = self._pinned_asset_refs.get(asset_id, 0)
            if count <= 1:
                self._pinned_asset_refs.pop(asset_id, None)
            else:
                self._pinned_asset_refs[asset_id] = count - 1
        self._prune()

    def externalize(
        self,
        models: dict[str, dict[str, Any]],
        messages: list[dict[str, Any]],
        *,
        live_model_ids: set[str],
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        assets = dict(self._assets)
        model_source_refs = {
            model_id: dict(refs)
            for model_id, refs in self._model_source_refs.items()
            if model_id in live_model_ids
        }
        referenced: set[str] = set()
        wire_models: dict[str, dict[str, Any]] = {}
        for model_id, model in models.items():
            state = model.get("state")
            if not isinstance(state, dict):
                wire_models[model_id] = model
                continue
            wire_state, source_refs = self._externalize_state(
                state,
                referenced,
                assets,
            )
            wire_model = {**model, "state": wire_state}
            if source_refs:
                wire_model["sourceRefs"] = source_refs
                current_model_id = model.get("modelId", model_id)
                if (
                    isinstance(current_model_id, str)
                    and current_model_id in live_model_ids
                ):
                    model_source_refs.setdefault(current_model_id, {}).update(
                        source_refs
                    )
            wire_models[model_id] = wire_model

        wire_messages: list[dict[str, Any]] = []
        for message in messages:
            data = message.get("data")
            if not isinstance(data, dict) or data.get("method") not in {
                "update",
                "echo_update",
            }:
                wire_messages.append(message)
                continue
            state = data.get("state")
            if not isinstance(state, dict):
                wire_messages.append(message)
                continue
            wire_state, source_refs = self._externalize_state(
                state,
                referenced,
                assets,
            )
            wire_message = {**message, "data": {**data, "state": wire_state}}
            if source_refs:
                wire_message["sourceRefs"] = source_refs
                model_id = message.get("modelId")
                if isinstance(model_id, str) and model_id in live_model_ids:
                    model_source_refs.setdefault(model_id, {}).update(source_refs)
            wire_messages.append(wire_message)

        manifest = {
            asset_id: {
                "kind": assets[asset_id][0],
                "byteLength": assets[asset_id][1],
            }
            for asset_id in sorted(referenced)
        }
        self._assets = assets
        self._model_source_refs = model_source_refs
        self._latest_asset_ids = referenced
        self._prune()
        return wire_models, wire_messages, manifest

    def clear(self) -> None:
        self._assets.clear()
        self._model_source_refs.clear()
        self._latest_asset_ids.clear()
        self._pinned_asset_refs.clear()

    def forget_models(self, model_ids: Iterable[str]) -> None:
        for model_id in model_ids:
            self._model_source_refs.pop(model_id, None)
        self._prune()

    def _prune(self) -> None:
        retained = self._latest_asset_ids | set(self._pinned_asset_refs)
        retained.update(
            asset_id
            for refs in self._model_source_refs.values()
            for asset_id in refs.values()
        )
        self._assets = {
            asset_id: asset
            for asset_id, asset in self._assets.items()
            if asset_id in retained
        }

    @staticmethod
    def _externalize_state(
        state: dict[str, Any],
        referenced: set[str],
        assets: dict[str, tuple[str, int, str]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        wire_state = dict(state)
        source_refs: dict[str, str] = {}
        for trait_name, kind in ASSET_KINDS.items():
            source = wire_state.get(trait_name)
            if not isinstance(source, str):
                continue
            del wire_state[trait_name]
            asset_id = _asset_id(kind, source)
            byte_length = len(source.encode("utf-8"))
            assets.setdefault(asset_id, (kind, byte_length, source))
            source_refs[trait_name] = asset_id
            referenced.add(asset_id)
        return wire_state, source_refs
