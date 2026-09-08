"""Project synchronized widget state into bounded, versioned model context."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar, cast

from exceptiongroup import BaseExceptionGroup, ExceptionGroup
from anywidget import AnyWidget

from ._projection_json import DEFAULT_MAX_BYTES, bounded_mapping, canonical_json
from ._models import bind_model
from ._spec import describe_model

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
        max_bytes: Maximum compact UTF-8 JSON bytes for the complete projection,
            including every widget in a sequence. Defaults to 8000. ``None``
            preserves trusted finite input in full, subject to JSON conversion
            and Python recursion limits. Integers must be at least 2 bytes,
            the encoded size of an empty JSON mapping.
    """

    project: Callable[[WidgetT], Mapping[str, object]]
    watch: tuple[str, ...] | None
    max_bytes: int | None

    def __init__(
        self,
        project: Callable[[WidgetT], Mapping[str, object]],
        *,
        watch: str | Sequence[str] | None = None,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
    ) -> None:
        if not callable(project):
            raise TypeError("StateProjection project must be callable")
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 2
        ):
            raise ValueError(
                "StateProjection max_bytes must be an integer of at least 2 or None"
            )
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
        object.__setattr__(self, "max_bytes", max_bytes)


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


@dataclass(frozen=True)
class ProjectionUpdate:
    version: int
    state: dict[str, Any]


class _RefreshStatus(Enum):
    SETTLED = "settled"
    RETRY = "retry"
    BUSY = "busy"
    DEFERRED = "deferred"


class StateContext:
    """Track, compute, and version one session's model-visible projection.

    Trait notifications provide committed values for selected projections.
    Callable projections commit only while their observed generation remains
    stable, and they may not mutate synchronized traits.
    """

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
        self._max_bytes = (
            state.max_bytes if isinstance(state, StateProjection) else DEFAULT_MAX_BYTES
        )
        self._lock = threading.RLock()
        self._dirty_generation = 1 if state is not None else 0
        self._projected_generation = 0
        self._last_json: str | None = None
        self._version = 0
        self._pending_update: ProjectionUpdate | None = None
        self._pending_error: Exception | None = None
        self._current_update: ProjectionUpdate | None = None
        self._current_error: Exception | None = None
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
                (current, describe_model(current).default_state_names)
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
                    names = describe_model(widget).capabilities.observe
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

    def commit_values(self, widget: object, names: Sequence[str]) -> None:
        """Capture traits explicitly published through the widget comm."""
        with self._lock:
            if self._closed:
                return
            for name in names:
                key = (id(widget), name)
                if key in self._notified_values:
                    value = _snapshot_containers(getattr(widget, name))
                    if self._refresh_thread_id == threading.get_ident() and not (
                        _values_match(value, self._notified_values[key][1])
                    ):
                        self._mutated_during_refresh = True
                    self._notified_values[key] = (
                        widget,
                        value,
                    )
                    if self._dirty_traits is None or key in self._dirty_traits:
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
        """Compute a dirty projection and report whether its generation settled.

        The projector runs outside the state lock. Its result commits only when
        observed state still matches the captured generation.
        """

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
        container_values: list[tuple[Any, Any]] = []
        try:
            try:
                if self._guards_projection_mutations:
                    projection_guards = self._install_projection_guards()
                if trait_selections is None:
                    with self._lock:
                        widgets = tuple(self._tracked_widgets)
                    container_values = [
                        (value, _snapshot_containers(value))
                        for widget in widgets
                        for name in describe_model(widget).synchronized_names
                        if type(value := getattr(widget, name)) in (dict, list, tuple)
                    ]
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
                state = bounded_mapping(projected, self._max_bytes)
                state_json = canonical_json(state)
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
        containers_mutated = any(
            not _values_match(value, snapshot) for value, snapshot in container_values
        )
        with self._lock:
            changed_during_refresh = self._dirty_generation != generation
            mutated_during_refresh = self._mutated_during_refresh
            self._finish_refresh()
            if self._closed:
                return _RefreshStatus.SETTLED
            if mutated_during_refresh or (
                containers_mutated and not changed_during_refresh
            ):
                self._projected_generation = self._dirty_generation
                self._pending_update = None
                self._pending_error = RuntimeError(
                    "State projection callables must not mutate synchronized "
                    "widget traits"
                )
                self._current_error = self._pending_error
                return _RefreshStatus.SETTLED
            if changed_during_refresh:
                return _RefreshStatus.RETRY
            if not notified_values_match:
                return _RefreshStatus.DEFERRED

            self._projected_generation = generation
            if error is not None:
                self._pending_update = None
                self._pending_error = error
                self._current_error = error
                return _RefreshStatus.SETTLED

            assert state is not None
            assert state_json is not None
            self._pending_error = None
            self._current_error = None
            if state_json == self._last_json:
                return _RefreshStatus.SETTLED

            self._last_json = state_json
            self._version += 1
            update = ProjectionUpdate(
                version=self._version,
                state=state,
            )
            self._pending_update = update
            self._current_update = update
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

    def should_retry_refresh(self, status: _RefreshStatus) -> bool:
        return status is _RefreshStatus.RETRY or (
            self.needs_refresh()
            and (status is not _RefreshStatus.DEFERRED or self.deferred_refresh_ready())
        )

    def reject_unstable(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._projected_generation = self._dirty_generation
            self._pending_update = None
            self._pending_error = RuntimeError(
                "Synchronized widget state did not stabilize during projection"
            )
            self._current_error = self._pending_error

    def current(self) -> ProjectionUpdate | None:
        """Return the latest projection without consuming its pending update."""
        retries = 0
        while self.refresh() is _RefreshStatus.RETRY:
            retries += 1
            if retries == 8:
                self.reject_unstable()
                break
        return self.current_cached()

    def current_cached(self) -> ProjectionUpdate | None:
        """Return the retained projection without running its projector."""
        with self._lock:
            if self._closed:
                return None
            if self._current_error is not None:
                raise self._current_error
            return copy.deepcopy(self._current_update)

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
            self._current_update = None
            self._current_error = None
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
                        _snapshot_containers(getattr(widget, name)),
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
                        self._notified_values[key] = (
                            owner,
                            _snapshot_containers(change.get("new")),
                        )
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
                        for name in describe_model(widget).capabilities.observe
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


def _traits_projector(names: tuple[str, ...]) -> StateProjector:
    def project(widget: AnyWidget) -> Mapping[str, object]:
        return bind_model(widget).read(names)

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
    return bind_model(widget).serialize(values)


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
    return all(
        _values_match(getattr(widget, name), notified)
        for widget, name, notified in notified_values
    )


def _snapshot_containers(value: Any) -> Any:
    # Traitlets publishes container assignment, while nested mutation stays local.
    # Preserve model and opaque leaf identities when retaining the published value.
    memo: dict[int, Any] = {}
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if type(current) is dict:
            pending.extend(current.keys())
            pending.extend(current.values())
        elif type(current) in (list, tuple):
            pending.extend(current)
        else:
            memo[identity] = current
    return copy.deepcopy(value, memo)


def _values_match(current: Any, notified: Any) -> bool:
    pending = [(current, notified)]
    seen: set[tuple[int, int]] = set()
    while pending:
        current, notified = pending.pop()
        pair = (id(current), id(notified))
        if current is notified or pair in seen:
            continue
        seen.add(pair)
        if type(current) in (dict, list, tuple):
            if type(current) is not type(notified) or len(current) != len(notified):
                return False
            if type(current) is dict:
                if current.keys() != notified.keys():
                    return False
                pending.extend((value, notified[key]) for key, value in current.items())
            else:
                pending.extend(zip(current, notified))
            continue
        try:
            equal = bool(current == notified)
        except Exception:
            return False
        if equal is not True:
            return False
    return True


def _unobserve(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    unobserve = getattr(widget, "unobserve")
    unobserve(callback, names=names)


def _validate_trait_names(widget: AnyWidget, names: tuple[str, ...]) -> None:
    missing = sorted(
        set(names).difference(trait.name for trait in describe_model(widget).traits)
    )
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
