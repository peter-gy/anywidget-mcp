from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Any, cast

from anywidget import AnyWidget

StateProjector = Callable[[Any], Mapping[str, Any]]
StateSpec = tuple[str, ...] | StateProjector | None


class _DefaultState:
    def __repr__(self) -> str:
        return "<default>"


DEFAULT_STATE = _DefaultState()

_MAX_CONTEXT_BYTES = 8_000
_MAX_VALUE_BYTES = 2_000
_MAX_STRING_CHARS = 1_000
_MAX_COLLECTION_ITEMS = 50
_MAX_TRAVERSAL_READS = 2_000
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_DEPTH = 6
_PREVIEW_CHARS = 240
_PREVIEW_ITEMS = 12
_MAX_KEY_CHARS = 240
_EXCLUDED_DEFAULT_TRAITS = frozenset({"layout", "tabbable", "tooltip"})


@dataclass(frozen=True)
class ProjectionUpdate:
    version: int
    state: dict[str, Any]


class _RefreshStatus(Enum):
    SETTLED = "settled"
    RETRY = "retry"
    BUSY = "busy"
    DEFERRED = "deferred"


@dataclass
class _ReadBudget:
    remaining: int = _MAX_TRAVERSAL_READS


class StateContext:
    def __init__(
        self,
        root: AnyWidget,
        widgets: Sequence[object],
        state: StateSpec | _DefaultState,
    ) -> None:
        self._root = root
        self._lock = threading.RLock()
        self._dirty_generation = 1 if state is not None else 0
        self._projected_generation = 0
        self._last_json: str | None = None
        self._version = 0
        self._pending_update: ProjectionUpdate | None = None
        self._pending_error: Exception | None = None
        self._refreshing = False
        self._refresh_thread_id: int | None = None
        self._mutated_during_refresh = False
        self._closed = False
        self._observers: list[tuple[object, tuple[str, ...]]] = []
        self._notified_values: dict[tuple[int, str], tuple[object, Any]] = {}
        self._tracks_all_widgets = False
        self._trait_names: tuple[str, ...] | None = None

        if state is None:
            self._projector: StateProjector | None = None
            return

        if isinstance(state, _DefaultState):
            names = tuple(
                name
                for name, trait in root.traits().items()
                if trait.metadata.get("sync")
                and not name.startswith("_")
                and name not in _EXCLUDED_DEFAULT_TRAITS
            )
            self._projector = _traits_projector(names)
            self._trait_names = names
            self._observe(root, names)
            return

        if isinstance(state, tuple):
            if not all(isinstance(name, str) for name in state):
                raise TypeError("state trait names must be strings")
            names = tuple(name for name in state if isinstance(name, str))
            _validate_trait_names(root, names)
            self._projector = _traits_projector(names)
            self._trait_names = names
            self._observe(root, names)
            return

        if not callable(state):
            raise TypeError("state must be callable")
        self._projector = state
        self._tracks_all_widgets = True
        self.add_widgets(widgets)

    def add_widgets(self, widgets: Sequence[object]) -> None:
        with self._lock:
            if not self._tracks_all_widgets:
                return
            added = False
            for widget in widgets:
                if any(observed is widget for observed, _names in self._observers):
                    continue
                names = _observable_trait_names(widget)
                if not names:
                    continue
                self._observe(widget, names)
                added = True
            if added:
                self._mark_dirty_locked()

    def remove_widgets(self, widgets: Sequence[object]) -> None:
        with self._lock:
            if not self._tracks_all_widgets:
                return
            targets = {id(widget) for widget in widgets}
            if not targets:
                return
            retained: list[tuple[object, tuple[str, ...]]] = []
            removed = False
            for widget, names in self._observers:
                if id(widget) not in targets:
                    retained.append((widget, names))
                    continue
                _unobserve(widget, self._mark_dirty, names)
                for name in names:
                    self._notified_values.pop((id(widget), name), None)
                removed = True
            self._observers = retained
            if removed:
                self._mark_dirty_locked()

    def invalidate(self) -> None:
        with self._lock:
            if self._closed or not self._tracks_all_widgets:
                return
            self._mark_dirty_locked()

    def take(self) -> ProjectionUpdate | None:
        retries = 0
        while self.refresh() is _RefreshStatus.RETRY:
            retries += 1
            if retries == 8:
                self.reject_unstable()
                break
        return self.take_cached()

    def refresh(self) -> _RefreshStatus:
        with self._lock:
            if (
                self._closed
                or self._projector is None
                or self._dirty_generation == self._projected_generation
            ):
                return _RefreshStatus.SETTLED
            if self._refreshing:
                return _RefreshStatus.BUSY
            generation = self._dirty_generation
            projector = self._projector
            trait_names = self._trait_names
            notified_values = tuple(
                (widget, name, value)
                for (_identity, name), (widget, value) in self._notified_values.items()
            )
            self._refreshing = True
            self._refresh_thread_id = threading.get_ident()
            self._mutated_during_refresh = False

        if trait_names is None and not _matches_notified_values(notified_values):
            with self._lock:
                changed_during_refresh = self._dirty_generation != generation
                self._finish_refresh()
                if self._closed:
                    return _RefreshStatus.SETTLED
                if changed_during_refresh:
                    return _RefreshStatus.RETRY
                return _RefreshStatus.DEFERRED

        state: dict[str, Any] | None = None
        state_json: str | None = None
        error: Exception | None = None
        try:
            if trait_names is None:
                projected = projector(self._root)
            else:
                projected = _project_notified_traits(
                    self._root,
                    trait_names,
                    notified_values,
                )
            if not isinstance(projected, Mapping):
                raise TypeError(
                    "The widget state projection must return a mapping, "
                    f"got {type(projected).__name__}"
                )
            state = _bounded_mapping(projected)
            state_json = _canonical_json(state)
        except Exception as caught:
            error = caught
        except BaseException:
            with self._lock:
                self._finish_refresh()
            raise

        notified_values_match = trait_names is not None or _matches_notified_values(
            notified_values
        )
        with self._lock:
            changed_during_refresh = self._dirty_generation != generation
            mutated_during_refresh = self._mutated_during_refresh
            self._finish_refresh()
            if self._closed:
                return _RefreshStatus.SETTLED
            if mutated_during_refresh:
                self._projected_generation = self._dirty_generation
                self._pending_update = None
                self._pending_error = RuntimeError(
                    "State projection callables must not mutate synchronized "
                    "widget traits"
                )
                return _RefreshStatus.SETTLED
            if changed_during_refresh:
                return _RefreshStatus.RETRY
            if not notified_values_match:
                return _RefreshStatus.DEFERRED

            self._projected_generation = generation
            if error is not None:
                self._pending_update = None
                self._pending_error = error
                return _RefreshStatus.SETTLED

            assert state is not None
            assert state_json is not None
            self._pending_error = None
            if state_json == self._last_json:
                return _RefreshStatus.SETTLED

            self._last_json = state_json
            self._version += 1
            self._pending_update = ProjectionUpdate(
                version=self._version,
                state=state,
            )
            return _RefreshStatus.SETTLED

    def needs_refresh(self) -> bool:
        with self._lock:
            return (
                not self._closed
                and self._projector is not None
                and self._dirty_generation != self._projected_generation
            )

    def deferred_refresh_ready(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            notified_values = tuple(
                (widget, name, value)
                for (_identity, name), (widget, value) in self._notified_values.items()
            )
        return _matches_notified_values(notified_values)

    def reject_unstable(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._projected_generation = self._dirty_generation
            self._pending_update = None
            self._pending_error = RuntimeError(
                "Synchronized widget state did not stabilize during projection"
            )

    def take_cached(self) -> ProjectionUpdate | None:
        with self._lock:
            if self._closed:
                return None
            error = self._pending_error
            self._pending_error = None
            if error is not None:
                raise error
            update = self._pending_update
            self._pending_update = None
            return update

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            observers = self._observers
            self._observers.clear()
            self._notified_values.clear()
            self._pending_update = None
            self._pending_error = None
        for widget, names in observers:
            _unobserve(widget, self._mark_dirty, names)

    def _observe(self, widget: object, names: tuple[str, ...]) -> None:
        if not names:
            return
        for name in names:
            self._notified_values[(id(widget), name)] = (widget, getattr(widget, name))
        observe = getattr(widget, "observe")
        observe(self._mark_dirty, names=names)
        self._observers.append((widget, names))

    def _mark_dirty(self, change: object) -> None:
        with self._lock:
            if not self._closed:
                if isinstance(change, Mapping):
                    owner = change.get("owner")
                    name = change.get("name")
                    if owner is not None and isinstance(name, str):
                        key = (id(owner), name)
                        if key in self._notified_values:
                            self._notified_values[key] = (owner, change.get("new"))
                self._mark_dirty_locked()

    def _mark_dirty_locked(self) -> None:
        self._dirty_generation += 1
        if self._refresh_thread_id == threading.get_ident():
            self._mutated_during_refresh = True

    def _finish_refresh(self) -> None:
        self._refreshing = False
        self._refresh_thread_id = None
        self._mutated_during_refresh = False


def validate_state_spec(state: object) -> None:
    if state is DEFAULT_STATE or state is None or callable(state):
        return
    if isinstance(state, tuple) and all(isinstance(name, str) for name in state):
        return
    raise TypeError(
        "state must be a tuple of trait names, a projection callable, None, or omitted"
    )


def _traits_projector(names: tuple[str, ...]) -> StateProjector:
    def project(widget: AnyWidget) -> Mapping[str, Any]:
        return widget.get_state(key=names)

    return project


def _project_notified_traits(
    widget: AnyWidget,
    names: tuple[str, ...],
    notified_values: tuple[tuple[object, str, Any], ...],
) -> Mapping[str, Any]:
    values = {
        name: value
        for observed, name, value in notified_values
        if observed is widget and name in names
    }
    state: dict[str, Any] = {}
    for name in names:
        to_json = widget.trait_metadata(name, "to_json", widget._trait_to_json)
        state[name] = to_json(values[name], widget)
    return state


def _matches_notified_values(
    notified_values: tuple[tuple[object, str, Any], ...],
) -> bool:
    for widget, name, notified in notified_values:
        current = getattr(widget, name)
        if current is notified:
            continue
        try:
            equal = bool(current == notified)
        except Exception:
            return False
        if equal is not True:
            return False
    return True


def _observable_trait_names(widget: object) -> tuple[str, ...]:
    trait_names = getattr(widget, "trait_names", None)
    observe = getattr(widget, "observe", None)
    unobserve = getattr(widget, "unobserve", None)
    if not callable(trait_names) or not callable(observe) or not callable(unobserve):
        return ()
    return tuple(cast(list[str], trait_names()))


def _unobserve(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    unobserve = getattr(widget, "unobserve")
    unobserve(callback, names=names)


def _validate_trait_names(widget: AnyWidget, names: tuple[str, ...]) -> None:
    missing = sorted(set(names).difference(widget.traits()))
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Unknown state trait for {type(widget).__name__}: {joined}")


def _bounded_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    budget = _ReadBudget()
    items, has_more, overflow, budget_exhausted = _limited_items(
        mapping.items(), budget
    )
    length = _safe_len(mapping)
    normalized: dict[str, Any] = {}
    for raw_key, value in items:
        key = _available_key(_bounded_key(raw_key), normalized)
        candidate = _json_value(value, depth=0, seen=set(), budget=budget)
        encoded = _canonical_json(candidate)
        if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
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
        if len(_canonical_json(candidate).encode("utf-8")) <= _MAX_CONTEXT_BYTES:
            result[key] = normalized[key]
        else:
            omitted_count += 1
            if len(omitted_traits) < _PREVIEW_ITEMS:
                omitted_traits.append(key)

    if omitted_count:
        summary_key = _available_summary_key(result)
        while True:
            summary = {
                "type": "projection",
                "omitted": omitted_count,
                "traits": sorted(omitted_traits)[:_PREVIEW_ITEMS],
            }
            candidate = {**result, summary_key: summary}
            if (
                len(_canonical_json(candidate).encode("utf-8")) <= _MAX_CONTEXT_BYTES
                or not result
            ):
                result[summary_key] = summary
                break
            key, _value = result.popitem()
            omitted_count += 1
            if len(omitted_traits) < _PREVIEW_ITEMS:
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
        if -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            return value
        return {
            "type": "integer",
            "bits": int.bit_length(value),
            "sign": "negative" if value < 0 else "positive",
        }
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "float", "value": str(value)}
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return {
            "type": "string",
            "characters": len(value),
            "preview": value[:_PREVIEW_CHARS],
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
    if depth >= _MAX_DEPTH:
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
            "preview": value[:_PREVIEW_CHARS],
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
            summary["keys"] = list(normalized)[:_PREVIEW_ITEMS]
        return summary
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        summary = {
            "type": "sequence",
            "jsonBytes": len(encoded.encode("utf-8")),
        }
        length = _safe_len(value)
        if length is not None:
            summary["length"] = length
        return summary
    return {
        "type": type(value).__name__,
        "jsonBytes": len(encoded.encode("utf-8")),
    }


def _limited_items(
    values: Any,
    budget: _ReadBudget,
) -> tuple[list[Any], bool, Any | None, bool]:
    sample_size = min(_MAX_COLLECTION_ITEMS + 1, budget.remaining)
    sampled = list(islice(iter(values), sample_size))
    budget.remaining -= len(sampled)
    budget_exhausted = (
        sample_size < _MAX_COLLECTION_ITEMS + 1 and len(sampled) == sample_size
    )
    if len(sampled) <= _MAX_COLLECTION_ITEMS:
        return sampled, False, None, budget_exhausted
    return (
        sampled[:_MAX_COLLECTION_ITEMS],
        True,
        sampled[_MAX_COLLECTION_ITEMS],
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
        key = f"{base[: _MAX_KEY_CHARS - len(suffix)]}{suffix}"
        index += 1
    return key


def _bounded_key(value: object) -> str:
    key = str(value)
    if len(key) <= _MAX_KEY_CHARS:
        return key
    suffix = f"... [{len(key)} characters]"
    return f"{key[: _MAX_KEY_CHARS - len(suffix)]}{suffix}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
