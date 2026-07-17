"""Bound model-visible widget state to deterministic JSON for MCP context."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Any

MAX_CONTEXT_BYTES = 8_000
MAX_VALUE_BYTES = 2_000
MAX_STRING_CHARS = 1_000
MAX_COLLECTION_ITEMS = 50
MAX_TRAVERSAL_READS = 2_000
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_DEPTH = 6
PREVIEW_CHARS = 240
PREVIEW_ITEMS = 12
MAX_KEY_CHARS = 240


@dataclass
class _ReadBudget:
    remaining: int = MAX_TRAVERSAL_READS


def bounded_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic JSON-safe state within context and traversal limits.

    Oversized, recursive, deep, or truncated values become summaries that
    preserve their type and bounded previews where available.
    """

    budget = _ReadBudget()
    items, has_more, overflow, budget_exhausted = _limited_items(
        mapping.items(), budget
    )
    length = _safe_len(mapping)
    normalized: dict[str, Any] = {}
    for raw_key, value in items:
        key = _available_key(_bounded_key(raw_key), normalized)
        candidate = _json_value(value, depth=0, seen=set(), budget=budget)
        encoded = canonical_json(candidate)
        if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
            candidate = _value_summary(value, candidate, encoded)
        normalized[key] = candidate

    result: dict[str, Any] = {}
    omitted_count = _omitted_count(
        length,
        len(items),
        has_more,
        budget_exhausted,
    )
    omitted_traits: list[str] = []
    if has_more and overflow is not None:
        omitted_traits.append(_bounded_key(overflow[0]))
    for key in sorted(normalized):
        candidate = {**result, key: normalized[key]}
        if len(canonical_json(candidate).encode("utf-8")) <= MAX_CONTEXT_BYTES:
            result[key] = normalized[key]
        else:
            omitted_count += 1
            if len(omitted_traits) < PREVIEW_ITEMS:
                omitted_traits.append(key)

    if omitted_count:
        summary_key = _available_summary_key(result)
        while True:
            summary = {
                "type": "projection",
                "omitted": omitted_count,
                "traits": sorted(omitted_traits)[:PREVIEW_ITEMS],
            }
            candidate = {**result, summary_key: summary}
            if (
                len(canonical_json(candidate).encode("utf-8")) <= MAX_CONTEXT_BYTES
                or not result
            ):
                result[summary_key] = summary
                break
            key, _value = result.popitem()
            omitted_count += 1
            if len(omitted_traits) < PREVIEW_ITEMS:
                omitted_traits.append(key)
    return result


def _json_value(
    value: Any,
    *,
    depth: int,
    seen: set[int],
    budget: _ReadBudget,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            return value
        return {
            "type": "integer",
            "bits": int.bit_length(value),
            "sign": "negative" if value < 0 else "positive",
        }
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "float", "value": str(value)}
    if isinstance(value, str):
        if len(value) <= MAX_STRING_CHARS:
            return value
        return {
            "type": "string",
            "characters": len(value),
            "preview": value[:PREVIEW_CHARS],
        }
    if isinstance(value, memoryview):
        return {"type": "binary", "bytes": value.nbytes}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "binary", "bytes": len(value)}
    if isinstance(value, Enum):
        return _json_value(value.value, depth=depth, seen=seen, budget=budget)

    identity = id(value)
    if identity in seen:
        return {"type": "recursive"}
    if depth >= MAX_DEPTH:
        return {"type": type(value).__name__, "depthLimited": True}

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            items, has_more, _overflow, budget_exhausted = _limited_items(
                value.items(), budget
            )
            length = _safe_len(value)
            result: dict[str, Any] = {}
            for key, item in items:
                bounded_key = _available_key(_bounded_key(key), result)
                result[bounded_key] = _json_value(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
            omitted = _omitted_count(
                length,
                len(items),
                has_more,
                budget_exhausted,
            )
            if omitted:
                summary: dict[str, Any] = {
                    "type": "mapping",
                    "omitted": omitted,
                }
                if length is None:
                    summary["entriesAtLeast"] = len(items) + omitted
                else:
                    summary["entries"] = length
                result[_available_summary_key(result)] = summary
            return result
        finally:
            seen.remove(identity)

    if isinstance(value, Sequence):
        seen.add(identity)
        try:
            items, has_more, _overflow, budget_exhausted = _limited_items(value, budget)
            length = _safe_len(value)
            sequence_result = [
                _json_value(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
                for item in items
            ]
            omitted = _omitted_count(
                length,
                len(items),
                has_more,
                budget_exhausted,
            )
            if not omitted:
                return sequence_result
            summary = {
                "type": "sequence",
                "items": sequence_result,
                "omitted": omitted,
            }
            if length is None:
                summary["lengthAtLeast"] = len(items) + omitted
            else:
                summary["length"] = length
            return summary
        finally:
            seen.remove(identity)

    return {"type": type(value).__name__}


def _value_summary(value: Any, normalized: Any, encoded: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "type": "string",
            "characters": len(value),
            "preview": value[:PREVIEW_CHARS],
        }
    if isinstance(value, Mapping):
        summary: dict[str, Any] = {
            "type": "mapping",
            "jsonBytes": len(encoded.encode("utf-8")),
        }
        length = _safe_len(value)
        if length is not None:
            summary["entries"] = length
        if isinstance(normalized, Mapping):
            summary["keys"] = list(normalized)[:PREVIEW_ITEMS]
        return summary
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        summary = {
            "type": "sequence",
            "jsonBytes": len(encoded.encode("utf-8")),
        }
        length = _safe_len(value)
        if length is not None:
            summary["length"] = length
        normalized_items: list[Any] = []
        source_omitted = 0
        if isinstance(normalized, list):
            normalized_items = normalized
        elif isinstance(normalized, Mapping):
            items = normalized.get("items")
            if isinstance(items, list):
                normalized_items = items
            omitted = normalized.get("omitted")
            if isinstance(omitted, int):
                source_omitted = omitted

        retained: list[Any] = []
        for item in normalized_items:
            candidate_items = [*retained, item]
            candidate = {**summary, "items": candidate_items}
            omitted = (
                max(length - len(candidate_items), 0)
                if length is not None
                else source_omitted + len(normalized_items) - len(candidate_items)
            )
            if omitted:
                candidate["omitted"] = omitted
            if len(canonical_json(candidate).encode("utf-8")) > MAX_VALUE_BYTES:
                break
            retained = candidate_items
        if retained:
            summary["items"] = retained
            omitted = (
                max(length - len(retained), 0)
                if length is not None
                else source_omitted + len(normalized_items) - len(retained)
            )
            if omitted:
                summary["omitted"] = omitted
        return summary
    return {
        "type": type(value).__name__,
        "jsonBytes": len(encoded.encode("utf-8")),
    }


def _limited_items(
    values: Any,
    budget: _ReadBudget,
) -> tuple[list[Any], bool, Any | None, bool]:
    sample_size = min(MAX_COLLECTION_ITEMS + 1, budget.remaining)
    sampled = list(islice(iter(values), sample_size))
    budget.remaining -= len(sampled)
    budget_exhausted = (
        sample_size < MAX_COLLECTION_ITEMS + 1 and len(sampled) == sample_size
    )
    if len(sampled) <= MAX_COLLECTION_ITEMS:
        return sampled, False, None, budget_exhausted
    return (
        sampled[:MAX_COLLECTION_ITEMS],
        True,
        sampled[MAX_COLLECTION_ITEMS],
        budget_exhausted,
    )


def _safe_len(value: Sized) -> int | None:
    try:
        return len(value)
    except (OverflowError, TypeError, ValueError):
        return None


def _omitted_count(
    length: int | None,
    included: int,
    has_more: bool,
    budget_exhausted: bool,
) -> int:
    observed = 1 if has_more or (length is None and budget_exhausted) else 0
    if length is None:
        return observed
    return max(length - included, observed)


def _available_summary_key(mapping: Mapping[str, Any]) -> str:
    return _available_key("_summary", mapping)


def _available_key(key: str, mapping: Mapping[str, Any]) -> str:
    if key not in mapping:
        return key
    base = key
    index = 2
    while key in mapping:
        suffix = f"_{index}"
        key = f"{base[: MAX_KEY_CHARS - len(suffix)]}{suffix}"
        index += 1
    return key


def _bounded_key(value: object) -> str:
    key = str(value)
    if len(key) <= MAX_KEY_CHARS:
        return key
    suffix = f"... [{len(key)} characters]"
    return f"{key[: MAX_KEY_CHARS - len(suffix)]}{suffix}"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
