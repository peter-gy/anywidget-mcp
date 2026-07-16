from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Any, Generic, TypeVar, cast

from anywidget import AnyWidget

WidgetT = TypeVar("WidgetT", bound=AnyWidget)
StateProjector = Callable[[Any], Mapping[str, object]]


@dataclass(frozen=True, init=False)
class StateProjection(Generic[WidgetT]):
    """Project model-visible state when selected root traits change.

    Args:
        project: Read-only function that returns a mapping for the root widget.
        watch: Root trait names that invalidate the projection. ``None`` observes
            every trait in the enrolled widget graph. An empty sequence computes
            the projection once during launch.
    """

    project: Callable[[WidgetT], Mapping[str, object]]
    watch: tuple[str, ...] | None

    def __init__(
        self,
        project: Callable[[WidgetT], Mapping[str, object]],
        *,
        watch: str | Sequence[str] | None = None,
    ) -> None:
        if not callable(project):
            raise TypeError("StateProjection project must be callable")
        if watch is None:
            normalized_watch = None
        elif isinstance(watch, str):
            normalized_watch = (watch,)
        elif isinstance(watch, Sequence) and all(
            isinstance(name, str) for name in watch
        ):
            normalized_watch = tuple(watch)
        else:
            raise TypeError("StateProjection watch must contain trait names")
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "watch", normalized_watch)


StateSpec = tuple[str, ...] | StateProjector | StateProjection[Any] | None


class _DefaultState:
    def __repr__(self) -> str:
        return "<default>"


DEFAULT_STATE = _DefaultState()


@dataclass(frozen=True)
class _GroupedState:
    roots: tuple[AnyWidget, ...]
    state: StateSpec | _DefaultState

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("A widget sequence must contain at least one AnyWidget")
        invalid = next(
            (
                (index, root)
                for index, root in enumerate(self.roots)
                if not isinstance(root, AnyWidget)
            ),
            None,
        )
        if invalid is not None:
            index, root = invalid
            raise TypeError(
                f"Widget sequence item {index} must be an AnyWidget, "
                f"got {type(root).__name__}"
            )


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
        state: StateSpec | _DefaultState | _GroupedState,
    ) -> None:
        self._root = root
        if isinstance(state, _GroupedState):
            self._projection_roots = state.roots
            state = state.state
            self._grouped = True
        else:
            self._projection_roots = (root,)
            self._grouped = False
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
        self._projection_guards: list[tuple[object, tuple[str, ...]]] = []
        self._notified_values: dict[tuple[int, str], tuple[object, Any]] = {}
        self._tracks_widget_graph = False
        self._observes_widget_graph = False
        self._guards_projection_mutations = False
        self._tracked_widgets: list[object] = []
        self._invalidates_on_comm = False
        self._dirty_traits: set[tuple[int, str]] | None = set()
        self._trait_selections: tuple[tuple[AnyWidget, tuple[str, ...]], ...] | None = (
            None
        )

        if state is None:
            self._projector: StateProjector | None = None
            return

        if isinstance(state, _DefaultState):
            selections = tuple(
                (current, _default_trait_names(current))
                for current in self._projection_roots
            )
            self._projector = _traits_projector(selections[0][1])
            self._trait_selections = selections
            self._dirty_traits = {
                (id(current), name) for current, names in selections for name in names
            }
            for current, names in _unique_trait_selections(selections):
                self._observe(current, names)
            return

        if isinstance(state, tuple):
            if not all(isinstance(name, str) for name in state):
                raise TypeError("state trait names must be strings")
            names = tuple(name for name in state if isinstance(name, str))
            selections = tuple((current, names) for current in self._projection_roots)
            for index, (current, selected) in enumerate(selections):
                _validate_grouped_trait_names(
                    current,
                    selected,
                    index=index if self._grouped else None,
                )
            self._projector = _traits_projector(names)
            self._trait_selections = selections
            self._dirty_traits = {
                (id(current), name)
                for current, selected in selections
                for name in selected
            }
            for current, selected in _unique_trait_selections(selections):
                self._observe(current, selected)
            return

        if isinstance(state, StateProjection):
            self._projector = state.project
            self._tracks_widget_graph = True
            if state.watch is None:
                self._observes_widget_graph = True
                self._dirty_traits = None
                self._invalidates_on_comm = True
                self.add_widgets(widgets)
            else:
                selections = tuple(
                    (current, state.watch) for current in self._projection_roots
                )
                for index, (current, selected) in enumerate(selections):
                    _validate_grouped_trait_names(
                        current,
                        selected,
                        index=index if self._grouped else None,
                    )
                self._dirty_traits = {
                    (id(current), name)
                    for current, selected in selections
                    for name in selected
                }
                self._guards_projection_mutations = True
                self.add_widgets(widgets)
                for current, selected in _unique_trait_selections(selections):
                    self._observe(current, selected)
            return

        if not callable(state):
            raise TypeError("state must be callable")
        self._projector = state
        self._tracks_widget_graph = True
        self._observes_widget_graph = True
        self._dirty_traits = None
        self._invalidates_on_comm = True
        self.add_widgets(widgets)

    def add_widgets(self, widgets: Sequence[object]) -> None:
        with self._lock:
            if not self._tracks_widget_graph:
                return
            added = False
            tracked: list[object] = []
            try:
                for widget in widgets:
                    if any(current is widget for current in self._tracked_widgets):
                        continue
                    self._tracked_widgets.append(widget)
                    tracked.append(widget)
                    if not self._observes_widget_graph:
                        continue
                    names = _observable_trait_names(widget)
                    if not names:
                        continue
                    self._observe(widget, names)
                    added = True
            except Exception:
                self.remove_widgets(tracked)
                raise
            if added and self._dirty_traits is None:
                self._mark_dirty_locked()

    def remove_widgets(self, widgets: Sequence[object]) -> None:
        with self._lock:
            if not self._tracks_widget_graph:
                return
            targets = {id(widget) for widget in widgets}
            if not targets:
                return
            if not self._observes_widget_graph:
                self._tracked_widgets = [
                    widget
                    for widget in self._tracked_widgets
                    if id(widget) not in targets
                ]
                return
            retained: list[tuple[object, tuple[str, ...]]] = []
            failed_targets: set[int] = set()
            errors: list[Exception] = []
            removed = False
            for widget, names in self._observers:
                if id(widget) not in targets:
                    retained.append((widget, names))
                    continue
                try:
                    _unobserve(widget, self._mark_dirty, names)
                except Exception as error:
                    retained.append((widget, names))
                    failed_targets.add(id(widget))
                    errors.append(error)
                    continue
                for name in names:
                    self._notified_values.pop((id(widget), name), None)
                removed = True
            self._observers = retained
            self._tracked_widgets = [
                widget
                for widget in self._tracked_widgets
                if id(widget) not in targets or id(widget) in failed_targets
            ]
            if removed and self._dirty_traits is None:
                self._mark_dirty_locked()
            if errors:
                raise ExceptionGroup("Failed to remove state observers", errors)

    def invalidate(self) -> None:
        with self._lock:
            if self._closed or not self._invalidates_on_comm:
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
            trait_selections = self._trait_selections
            notified_values = tuple(
                (widget, name, value)
                for (_identity, name), (widget, value) in self._notified_values.items()
            )
            self._refreshing = True
            self._refresh_thread_id = threading.get_ident()
            self._mutated_during_refresh = False

        if trait_selections is None and not _matches_notified_values(notified_values):
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
        projection_guards: list[tuple[object, tuple[str, ...]]] = []
        try:
            try:
                if self._guards_projection_mutations:
                    projection_guards = self._install_projection_guards()
                if trait_selections is None:
                    projected = _project_roots(
                        projector,
                        self._projection_roots,
                        grouped=self._grouped,
                    )
                else:
                    states = [
                        _project_notified_traits(
                            current,
                            names,
                            notified_values,
                        )
                        for current, names in trait_selections
                    ]
                    projected = _aggregate_states(
                        states,
                        grouped=self._grouped,
                    )
                if not isinstance(projected, Mapping):
                    raise TypeError(
                        "The widget state projection must return a mapping, "
                        f"got {type(projected).__name__}"
                    )
                state = _bounded_mapping(projected)
                state_json = _canonical_json(state)
            finally:
                self._remove_projection_guards(projection_guards)
        except Exception as caught:
            error = caught
        except BaseException:
            with self._lock:
                self._finish_refresh()
            raise

        notified_values_match = (
            trait_selections is not None or _matches_notified_values(notified_values)
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
        errors: list[Exception] = []
        with self._lock:
            if self._closed and not self._observers and not self._projection_guards:
                return
            self._closed = True
            self._tracked_widgets.clear()
            self._notified_values.clear()
            self._pending_update = None
            self._pending_error = None
            retained: list[tuple[object, tuple[str, ...]]] = []
            for widget, names in self._observers:
                try:
                    _unobserve(widget, self._mark_dirty, names)
                except Exception as error:
                    retained.append((widget, names))
                    errors.append(error)
            self._observers = retained
            retained_guards: list[tuple[object, tuple[str, ...]]] = []
            for widget, names in self._projection_guards:
                try:
                    _unobserve(widget, self._mark_dirty, names)
                except Exception as error:
                    retained_guards.append((widget, names))
                    errors.append(error)
            self._projection_guards = retained_guards
        if errors:
            raise ExceptionGroup("Failed to close state observers", errors)

    def _observe(self, widget: object, names: tuple[str, ...]) -> None:
        if not names:
            return
        with self._lock:
            observe = getattr(widget, "observe")
            registration = (widget, names)
            self._observers.append(registration)
            try:
                observe(self._mark_dirty, names=names)
                for name in names:
                    self._notified_values[(id(widget), name)] = (
                        widget,
                        getattr(widget, name),
                    )
            except BaseException as error:
                cleanup_error: Exception | None = None
                try:
                    _unobserve(widget, self._mark_dirty, names)
                except Exception as caught:
                    cleanup_error = caught
                else:
                    for index, (current_widget, current_names) in enumerate(
                        self._observers
                    ):
                        if current_widget is widget and current_names == names:
                            self._observers.pop(index)
                            break
                for name in names:
                    self._notified_values.pop((id(widget), name), None)
                if cleanup_error is not None:
                    raise BaseExceptionGroup(
                        "Failed to initialize and remove a state observer",
                        [error, cleanup_error],
                    ) from error
                raise

    def _mark_dirty(self, change: object) -> None:
        with self._lock:
            if self._closed:
                return
            key: tuple[int, str] | None = None
            if isinstance(change, Mapping):
                owner = change.get("owner")
                name = change.get("name")
                if owner is not None and isinstance(name, str):
                    key = (id(owner), name)
                    if key in self._notified_values:
                        self._notified_values[key] = (owner, change.get("new"))
            if self._refresh_thread_id == threading.get_ident():
                self._mutated_during_refresh = True
            if self._dirty_traits is None or key in self._dirty_traits:
                self._mark_dirty_locked()

    def _mark_dirty_locked(self) -> None:
        self._dirty_generation += 1

    def _install_projection_guards(
        self,
    ) -> list[tuple[object, tuple[str, ...]]]:
        with self._lock:
            widgets = tuple(self._tracked_widgets)
            observed: dict[int, set[str]] = {}
            for widget, names in (*self._observers, *self._projection_guards):
                observed.setdefault(id(widget), set()).update(names)
            guards: list[tuple[object, tuple[str, ...]]] = []
            try:
                for widget in widgets:
                    names = tuple(
                        name
                        for name in _observable_trait_names(widget)
                        if name not in observed.get(id(widget), ())
                    )
                    if not names:
                        continue
                    observe = getattr(widget, "observe")
                    observe(self._mark_dirty, names=names)
                    guard = (widget, names)
                    guards.append(guard)
                    self._projection_guards.append(guard)
            except BaseException as error:
                try:
                    self._remove_projection_guards(guards)
                except Exception as cleanup_error:
                    if isinstance(error, Exception):
                        raise ExceptionGroup(
                            "Failed to install and remove projection guards",
                            [error, cleanup_error],
                        ) from error
                    raise
                raise
        return guards

    def _remove_projection_guards(
        self,
        guards: Sequence[tuple[object, tuple[str, ...]]],
    ) -> None:
        errors: list[Exception] = []
        with self._lock:
            for widget, names in guards:
                try:
                    _unobserve(widget, self._mark_dirty, names)
                except Exception as error:
                    errors.append(error)
                    continue
                for index, (current, current_names) in enumerate(
                    self._projection_guards
                ):
                    if current is widget and current_names == names:
                        self._projection_guards.pop(index)
                        break
        if errors:
            raise ExceptionGroup("Failed to remove projection guards", errors)

    def _finish_refresh(self) -> None:
        self._refreshing = False
        self._refresh_thread_id = None
        self._mutated_during_refresh = False


def validate_state_spec(state: object) -> None:
    if (
        state is DEFAULT_STATE
        or state is None
        or isinstance(state, StateProjection)
        or callable(state)
    ):
        return
    if isinstance(state, tuple) and all(isinstance(name, str) for name in state):
        return
    raise TypeError(
        "state must be a tuple of trait names, a StateProjection, "
        "a projection callable, None, or omitted"
    )


def _default_trait_names(widget: AnyWidget) -> tuple[str, ...]:
    return tuple(
        name
        for name, trait in widget.traits().items()
        if trait.metadata.get("sync")
        and not name.startswith("_")
        and name not in _EXCLUDED_DEFAULT_TRAITS
    )


def _traits_projector(names: tuple[str, ...]) -> StateProjector:
    def project(widget: AnyWidget) -> Mapping[str, object]:
        return widget.get_state(key=names)

    return project


def _project_roots(
    projector: StateProjector,
    roots: tuple[AnyWidget, ...],
    *,
    grouped: bool,
) -> Mapping[str, object]:
    states: list[Mapping[str, object]] = []
    for index, root in enumerate(roots):
        projected = projector(root)
        if not isinstance(projected, Mapping):
            if not grouped:
                return cast(Mapping[str, object], projected)
            raise TypeError(
                "The widget state projection for sequence item "
                f"{index} must return a mapping, got {type(projected).__name__}"
            )
        states.append(projected)
    return _aggregate_states(states, grouped=grouped)


def _aggregate_states(
    states: Sequence[Mapping[str, object]],
    *,
    grouped: bool,
) -> Mapping[str, object]:
    if grouped:
        return {"widgets": list(states)}
    return states[0]


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


def _unique_trait_selections(
    selections: Sequence[tuple[AnyWidget, tuple[str, ...]]],
) -> tuple[tuple[AnyWidget, tuple[str, ...]], ...]:
    unique: list[tuple[AnyWidget, tuple[str, ...]]] = []
    seen: set[tuple[int, tuple[str, ...]]] = set()
    for widget, names in selections:
        key = (id(widget), names)
        if key in seen:
            continue
        seen.add(key)
        unique.append((widget, names))
    return tuple(unique)


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


def _validate_grouped_trait_names(
    widget: AnyWidget,
    names: tuple[str, ...],
    *,
    index: int | None,
) -> None:
    try:
        _validate_trait_names(widget, names)
    except ValueError as error:
        if index is None:
            raise
        raise ValueError(
            f"Invalid state selection for widget sequence item {index} "
            f"({type(widget).__name__}): {error}"
        ) from error


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
            if len(_canonical_json(candidate).encode("utf-8")) > _MAX_VALUE_BYTES:
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
