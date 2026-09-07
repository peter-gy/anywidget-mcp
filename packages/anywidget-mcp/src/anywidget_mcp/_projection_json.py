"""Serialize model-visible state within one encoded JSON byte budget."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from typing import Any

DEFAULT_MAX_BYTES = 8_000
MAX_SAFE_INTEGER = (1 << 53) - 1


class _TooLarge(Exception):
    pass


@dataclass
class _Budget:
    remaining: int | None

    def read(self) -> bool:
        # Every iterator attempt costs at least one potential JSON byte, including
        # repeated mapping keys and iterators whose advertised length is wrong.
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def bounded_mapping(
    mapping: Mapping[str, Any], max_bytes: int | None = DEFAULT_MAX_BYTES
) -> dict[str, Any]:
    """Normalize state, preserving complete values that fit compact UTF-8 JSON.

    Truncated collections retain a prefix and an omission summary. ``None``
    traverses trusted finite input in full. Python's recursion limit produces a
    projection summary at the outer boundary.
    """
    if max_bytes is not None and max_bytes < 2:
        raise ValueError("StateProjection max_bytes must fit a JSON mapping (2 bytes)")
    try:
        result, _size = _json_value(mapping, max_bytes, set(), _Budget(max_bytes))
        # The consumer also serializes the result. Catch its recursion boundary
        # here so the session can publish a JSON-safe diagnostic.
        canonical_json(result)
        return result
    except RecursionError:
        return _summary({"type": "projection", "recursionLimited": True}, max_bytes)[0]


def _json_value(
    value: Any, limit: int | None, seen: set[int], budget: _Budget
) -> tuple[Any, int]:
    if value is None or isinstance(value, bool):
        return _encoded(value, limit)
    if isinstance(value, int):
        if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            return _encoded(value, limit)
        return _summary(
            {
                "type": "integer",
                "bits": int.bit_length(value),
                "sign": "negative" if value < 0 else "positive",
            },
            limit,
        )
    if isinstance(value, float):
        if math.isfinite(value):
            return _encoded(value, limit)
        return _summary({"type": "float", "value": str(value)}, limit)
    if isinstance(value, str):
        try:
            return _encoded(value, limit)
        except _TooLarge:
            assert limit is not None
            return _string_summary(value, limit)
    if isinstance(value, (bytes, bytearray, memoryview)):
        size = value.nbytes if isinstance(value, memoryview) else len(value)
        return _summary({"type": "binary", "bytes": size}, limit)
    if isinstance(value, Enum):
        return _json_value(value.value, limit, seen, budget)
    if id(value) in seen:
        return _summary({"type": "recursive"}, limit)
    if not isinstance(value, (Mapping, Sequence)):
        return _summary({"type": type(value).__name__}, limit)

    seen.add(id(value))
    try:
        return _collection(value, limit, seen, budget)
    finally:
        seen.remove(id(value))


def _string_summary(value: str, limit: int) -> tuple[dict[str, Any], int]:
    summary = {"type": "string", "characters": len(value), "truncated": True}
    try:
        _empty, overhead = _encoded({**summary, "preview": ""}, limit)
    except _TooLarge:
        return _summary(summary, limit)
    low, high = 0, min(len(value), limit - overhead)
    while low < high:
        middle = (low + high + 1) // 2
        try:
            _encoded(value[:middle], limit - overhead + 2)
        except _TooLarge:
            high = middle - 1
        else:
            low = middle
    return _encoded({**summary, "preview": value[:low]}, limit)


def _collection(
    value: Mapping[Any, Any] | Sequence[Any],
    limit: int | None,
    seen: set[int],
    budget: _Budget,
) -> tuple[Any, int]:
    is_mapping = isinstance(value, Mapping)
    result: Any = {} if is_mapping else []
    size = 2
    if limit is not None and size > limit:
        raise _TooLarge
    iterator = iter(value.items() if is_mapping else value)
    while budget.read():
        try:
            item = next(iterator)
        except StopIteration:
            return result, size
        overhead = 1 if result else 0
        key = ""
        if is_mapping:
            raw_key, item = item
            key = _available_key(str(raw_key), result)
            try:
                _key, key_size = _encoded(key, _remaining(limit, size + overhead))
            except _TooLarge:
                break
            overhead += key_size + 1
        try:
            normalized, item_size = _json_value(
                item, _remaining(limit, size + overhead), seen, budget
            )
        except _TooLarge:
            break
        if is_mapping:
            result[key] = normalized
        else:
            result.append(normalized)
        size += overhead + item_size
    return _collection_summary(value, result, limit)


def _collection_summary(value: Any, result: Any, limit: int | None) -> tuple[Any, int]:
    is_mapping = isinstance(value, Mapping)
    length = _safe_len(value)
    while True:
        summary: dict[str, Any] = {
            "type": "mapping" if is_mapping else "sequence",
            "omitted": max(1, length - len(result)) if length is not None else 1,
        }
        if length is not None:
            summary["entries" if is_mapping else "length"] = length
        else:
            summary["entriesAtLeast" if is_mapping else "lengthAtLeast"] = (
                len(result) + 1
            )
        if is_mapping:
            candidate = {**result, _available_key("_summary", result): summary}
        else:
            candidate = {**summary, "items": result}
        try:
            return _encoded(candidate, limit)
        except _TooLarge:
            if not result:
                return _summary(summary, limit)
            if is_mapping:
                result.popitem()
            else:
                result.pop()


def _summary(value: dict[str, Any], limit: int | None) -> tuple[dict[str, Any], int]:
    try:
        return _encoded(value, limit)
    except _TooLarge:
        return _encoded({}, limit)


def _encoded(value: Any, limit: int | None) -> tuple[Any, int]:
    if limit is not None and isinstance(value, str) and len(value) + 2 > limit:
        raise _TooLarge
    size = len(canonical_json(value).encode("utf-8"))
    if limit is not None and size > limit:
        raise _TooLarge
    return value, size


def _remaining(limit: int | None, used: int) -> int | None:
    return None if limit is None else limit - used


def _safe_len(value: Sized) -> int | None:
    try:
        return len(value)
    except (OverflowError, TypeError, ValueError):
        return None


def _available_key(key: str, mapping: Mapping[str, Any]) -> str:
    base = key
    index = 2
    while key in mapping:
        key = f"{base}_{index}"
        index += 1
    return key


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
