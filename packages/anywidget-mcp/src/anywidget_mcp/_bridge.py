from __future__ import annotations

import base64
import copy
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from anywidget import AnyWidget
from anywidget._descriptor import ReprMimeBundle

from ._state import (
    DEFAULT_STATE,
    ProjectionUpdate,
    StateContext,
    StateSpec,
    _DefaultState,
    _RefreshStatus,
)

_claimed_widgets: dict[int, tuple[Callable[[], object | None], str]] = {}
_claimed_widgets_lock = threading.RLock()
_NOTIFICATION_WAIT_SECONDS = 3.0
_MAX_PROJECTION_REFRESH_RETRIES = 8


def _encode_buffer(buffer: bytes | bytearray | memoryview) -> str:
    return base64.b64encode(memoryview(buffer).tobytes()).decode("ascii")


def _decode_buffer(buffer: str) -> bytes:
    return base64.b64decode(buffer, validate=True)


@dataclass(frozen=True)
class WidgetMessage:
    model_id: str
    data: dict[str, Any]
    buffers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "modelId": self.model_id,
            "data": self.data,
            "buffers": list(self.buffers),
        }


@dataclass(frozen=True)
class SessionSnapshot:
    messages: list[dict[str, Any]]
    models: dict[str, dict[str, Any]]
    removed_model_ids: list[str]
    projection: ProjectionUpdate | None
    projection_error: str | None


def _empty_snapshot() -> SessionSnapshot:
    return SessionSnapshot(
        messages=[],
        models={},
        removed_model_ids=[],
        projection=None,
        projection_error=None,
    )


class BridgeComm:
    kernel = True

    def __init__(self, comm_id: str, emit: Callable[[WidgetMessage], None]) -> None:
        self.comm_id = comm_id
        self._emit = emit
        self._on_msg: Callable[[dict[str, Any]], None] | None = None
        self._closed = False

    def on_msg(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_msg = callback

    def send(
        self,
        data: dict[str, Any] | None = None,
        buffers: Iterable[bytes | bytearray | memoryview] | None = None,
        **_: Any,
    ) -> None:
        if self._closed:
            return
        self._emit(
            WidgetMessage(
                model_id=self.comm_id,
                data=data or {},
                buffers=tuple(_encode_buffer(buffer) for buffer in buffers or ()),
            )
        )

    def receive(self, data: dict[str, Any], buffers: Iterable[str] = ()) -> None:
        if self._closed:
            raise RuntimeError("The widget session is closed")
        if self._on_msg is None:
            raise RuntimeError("The widget has no comm message handler")
        self._on_msg(
            {
                "content": {"data": copy.deepcopy(data)},
                "buffers": [_decode_buffer(buffer) for buffer in buffers],
            }
        )

    def close(self, **_: Any) -> None:
        self._closed = True


class WidgetInUseError(RuntimeError):
    pass


class WidgetSession:
    def __init__(
        self,
        instance_id: str,
        root: AnyWidget,
        state: StateSpec | _DefaultState = DEFAULT_STATE,
    ) -> None:
        self.instance_id = instance_id
        self.root = root
        self._lock = threading.RLock()
        self._notification_condition = threading.Condition(self._lock)
        self._notification_depths: dict[int, int] = {}
        self._notification_sources: dict[tuple[int, int], int] = {}
        self._notify_change_methods: dict[int, Callable[[Any], Any]] = {}
        self._messages: list[WidgetMessage] = []
        self._protocol_controllers: dict[int, ReprMimeBundle] = {}
        self._widgets = _collect_widgets(root, self._protocol_controllers)
        self._widgets_by_identity = {id(widget): widget for widget in self._widgets}
        self._comms: dict[str, BridgeComm] = {}
        self._graph_observers: dict[int, tuple[str, ...]] = {}
        self._pending_models: dict[str, dict[str, Any]] = {}
        self._pending_removed_model_ids: list[str] = []
        self._graph_sync_suspended = False
        self._closed = False
        self._state_context: StateContext | None = None
        self._models: dict[str, dict[str, Any]] = {}
        _claim_widgets(self._widgets)
        try:
            self._install_notification_gates(self._widgets)
            self._models = self._connect_models(self._widgets)
            self._observe_widget_graph(self._widgets)
            self._state_context = StateContext(
                root,
                self._widgets,
                state,
            )
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    @property
    def root_model_id(self) -> str:
        return self.root.model_id

    @property
    def models(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._models)

    def receive(
        self,
        model_id: str,
        data: dict[str, Any],
        buffers: Iterable[str] = (),
    ) -> SessionSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeError("The widget session is closed")
            comm = self._comms.get(model_id)
            if comm is None:
                raise KeyError(f"Unknown widget model: {model_id}")
        comm.receive(data, buffers)
        context = self._state_context
        if context is not None:
            context.invalidate()
        return self.snapshot()

    def snapshot(self) -> SessionSnapshot:
        return self._snapshot(full_models=False)

    def launch_snapshot(self) -> SessionSnapshot:
        return self._snapshot(full_models=True)

    def take_projection(self) -> ProjectionUpdate | None:
        context = self._state_context
        return context.take() if context is not None else None

    def close(self) -> None:
        errors: list[Exception] = []
        with self._notification_condition:
            if self._closed:
                return
            self._closed = True
            self._notification_condition.notify_all()
            try:
                self._wait_for_notifications("close")
            except Exception as error:
                errors.append(error)

            context = self._state_context
            self._state_context = None
            graph_observers = [
                (widget, self._graph_observers.get(id(widget), ()))
                for widget in self._widgets
            ]
            self._graph_observers.clear()
            widgets = list(reversed(self._widgets))
            notification_methods = {
                id(widget): self._notify_change_methods.pop(id(widget), None)
                for widget in widgets
            }
            comms = list(self._comms.values())
            self._messages.clear()
            self._pending_models.clear()
            self._pending_removed_model_ids.clear()
            self._comms.clear()
            self._models.clear()
            self._widgets_by_identity.clear()

        if context is not None:
            try:
                context.close()
            except Exception as error:
                errors.append(error)
        for widget, names in graph_observers:
            if not names:
                continue
            try:
                _unobserve(widget, self._sync_widget_graph, names)
            except Exception as error:
                errors.append(error)
        for widget in widgets:
            original = notification_methods[id(widget)]
            if original is not None:
                try:
                    setattr(widget, "notify_change", original)
                except Exception as error:
                    errors.append(error)
            try:
                _close_widget(widget, self._protocol_controllers)
            except Exception as error:
                errors.append(error)
        for comm in comms:
            try:
                comm.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Failed to close widget session", errors)

    def _wait_for_notifications(self, action: str) -> None:
        thread_id = threading.get_ident()
        if self._notification_depths.get(thread_id, 0):
            raise RuntimeError(
                f"Cannot {action} a widget session from inside an active "
                "widget notification"
            )
        deadline = time.monotonic() + _NOTIFICATION_WAIT_SECONDS
        while self._notification_depths:
            if action == "snapshot" and self._closed:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {_NOTIFICATION_WAIT_SECONDS:g} seconds waiting "
                    f"for widget notifications before {action}"
                )
            self._notification_condition.wait(remaining)

    def _install_notification_gates(self, widgets: Iterable[object]) -> None:
        for widget in widgets:
            identity = id(widget)
            if identity in self._notify_change_methods:
                continue
            original = getattr(widget, "notify_change", None)
            if not callable(original):
                continue
            original = cast(Callable[[Any], Any], original)

            def notify_change(
                change: Any,
                original: Callable[[Any], Any] = original,
                identity: int = identity,
            ) -> None:
                thread_id = threading.get_ident()
                source_key = (thread_id, identity)
                with self._notification_condition:
                    if self._closed:
                        return
                    self._notification_depths[thread_id] = (
                        self._notification_depths.get(thread_id, 0) + 1
                    )
                    self._notification_sources[source_key] = (
                        self._notification_sources.get(source_key, 0) + 1
                    )
                try:
                    original(change)
                finally:
                    with self._notification_condition:
                        source_depth = self._notification_sources[source_key] - 1
                        if source_depth:
                            self._notification_sources[source_key] = source_depth
                        else:
                            self._notification_sources.pop(source_key)
                        depth = self._notification_depths[thread_id] - 1
                        if depth:
                            self._notification_depths[thread_id] = depth
                        else:
                            self._notification_depths.pop(thread_id)
                        if not self._notification_depths:
                            self._notification_condition.notify_all()

            try:
                setattr(widget, "notify_change", notify_change)
            except (AttributeError, TypeError):
                if isinstance(widget, AnyWidget):
                    raise
                continue
            self._notify_change_methods[identity] = original

    def _snapshot(self, *, full_models: bool) -> SessionSnapshot:
        retries = 0
        while True:
            with self._notification_condition:
                if self._closed:
                    return _empty_snapshot()
                self._wait_for_notifications("snapshot")
                if self._closed:
                    return _empty_snapshot()
                context = self._state_context

            status = (
                context.refresh() if context is not None else _RefreshStatus.SETTLED
            )

            with self._notification_condition:
                if self._closed:
                    return _empty_snapshot()
                self._wait_for_notifications("snapshot")
                if self._closed:
                    return _empty_snapshot()

                needs_refresh = (
                    context is not None
                    and context is self._state_context
                    and context.needs_refresh()
                )
                deferred_ready = (
                    status is _RefreshStatus.DEFERRED
                    and context is not None
                    and context is self._state_context
                    and context.deferred_refresh_ready()
                )
                if status is _RefreshStatus.RETRY or (
                    needs_refresh
                    and (status is not _RefreshStatus.DEFERRED or deferred_ready)
                ):
                    retries += 1
                    if retries < _MAX_PROJECTION_REFRESH_RETRIES:
                        continue
                    assert context is not None
                    context.reject_unstable()

                if full_models:
                    models = copy.deepcopy(self._models)
                    model_ids = set(models)
                    messages = [
                        message.as_dict()
                        for message in self._messages
                        if message.model_id in model_ids
                    ]
                    removed_model_ids: list[str] = []
                else:
                    models = self._pending_models
                    messages = [message.as_dict() for message in self._messages]
                    removed_model_ids = self._pending_removed_model_ids

                self._messages.clear()
                self._pending_models = {}
                self._pending_removed_model_ids = []

                projection: ProjectionUpdate | None = None
                projection_error: str | None = None
                if context is not None and context is self._state_context:
                    try:
                        projection = context.take_cached()
                    except Exception as error:
                        projection_error = str(error)

                return SessionSnapshot(
                    messages=messages,
                    models=models,
                    removed_model_ids=removed_model_ids,
                    projection=projection,
                    projection_error=projection_error,
                )

    def _restore_notification_gate(self, widget: object) -> None:
        original = self._notify_change_methods.pop(id(widget), None)
        if original is not None:
            setattr(widget, "notify_change", original)

    def _capture(self, source: object, message: WidgetMessage) -> None:
        with self._lock:
            if self._closed:
                return
            data = _replace_widget_refs(
                message.data,
                self._protocol_controllers,
            )
            assert isinstance(data, dict)
            serialized = cast(dict[str, Any], data)
            self._messages.append(
                WidgetMessage(
                    model_id=message.model_id,
                    data=serialized,
                    buffers=message.buffers,
                )
            )
            context = self._state_context
            if context is not None:
                context.invalidate()
            source_is_notifying = bool(
                self._notification_sources.get(
                    (threading.get_ident(), id(source)),
                )
            )
            if not source_is_notifying and not self._graph_sync_suspended:
                self._sync_widget_graph(None)

    def _connect_models(
        self,
        widgets: list[object],
    ) -> dict[str, dict[str, Any]]:
        was_suspended = self._graph_sync_suspended
        self._graph_sync_suspended = True
        try:
            return self._connect_models_suspended(widgets)
        finally:
            self._graph_sync_suspended = was_suspended

    def _connect_models_suspended(
        self,
        widgets: list[object],
    ) -> dict[str, dict[str, Any]]:
        protocol_sync: dict[int, bool] = {}
        for widget in widgets:
            model_id = _model_id(widget, self._protocol_controllers)
            controller = _protocol_controller(
                widget,
                self._protocol_controllers,
            )
            if isinstance(widget, AnyWidget):
                old_comm = widget.comm
            else:
                assert controller is not None
                old_comm = controller._comm
                protocol_sync[id(widget)] = bool(
                    getattr(old_comm, "_msg_callback", None)
                    or controller._disconnectors
                )
                controller.unsync_object_with_view()
            if old_comm is not None:
                old_comm.close()
            comm = BridgeComm(
                model_id,
                lambda message, source=widget: self._capture(source, message),
            )
            if isinstance(widget, AnyWidget):
                widget.comm = comm
            else:
                assert controller is not None
                cast(Any, controller)._comm = comm
            self._comms[model_id] = comm

        models: dict[str, dict[str, Any]] = {}
        for widget in widgets:
            first_message = len(self._messages)
            controller = _protocol_controller(
                widget,
                self._protocol_controllers,
            )
            if isinstance(widget, AnyWidget):
                widget.send_state()
            else:
                assert controller is not None
                if protocol_sync[id(widget)]:
                    controller.sync_object_with_view()
                else:
                    controller.send_state()
            emitted = self._messages[first_message:]
            del self._messages[first_message:]
            model_id = _model_id(widget, self._protocol_controllers)
            initial = next(
                (
                    message
                    for message in reversed(emitted)
                    if message.model_id == model_id
                    and message.data.get("method") == "update"
                    and isinstance(message.data.get("state"), dict)
                ),
                None,
            )
            if initial is None:
                raise RuntimeError("AnyWidget did not emit an initial state update")
            state = initial.data.get("state")
            assert isinstance(state, dict)
            models[model_id] = {
                "modelId": model_id,
                "state": state,
                "bufferPaths": initial.data.get("buffer_paths", []),
                "buffers": list(initial.buffers),
            }
        return models

    def _observe_widget_graph(self, widgets: list[object]) -> None:
        for widget in widgets:
            names = _synced_trait_names(widget)
            if not names:
                continue
            _observe(widget, self._sync_widget_graph, names)
            self._graph_observers[id(widget)] = names

    def _sync_widget_graph(self, _change: object) -> None:
        with self._lock:
            if self._closed or self._graph_sync_suspended:
                return

            reachable = _collect_widgets(self.root, self._protocol_controllers)
            reachable_by_identity = {id(widget): widget for widget in reachable}
            added = [
                widget
                for widget in reachable
                if id(widget) not in self._widgets_by_identity
            ]
            removed = [
                widget
                for widget in self._widgets
                if id(widget) not in reachable_by_identity
            ]

            added_model_ids = {
                id(widget): _model_id(widget, self._protocol_controllers)
                for widget in added
            }
            if added:
                claimed = False
                try:
                    _claim_widgets(added)
                    claimed = True
                    self._install_notification_gates(added)
                    added_models = self._connect_models(added)
                    self._observe_widget_graph(added)
                    if self._state_context is not None:
                        self._state_context.add_widgets(added)
                    self._widgets_by_identity.update(
                        (id(widget), widget) for widget in added
                    )
                    self._models.update(added_models)
                    self._pending_models.update(added_models)
                except Exception as error:
                    if claimed:
                        self._discard_added_widgets(added, added_model_ids)
                    try:
                        self._rollback_widget_trait_change(
                            _change,
                            set(added_model_ids.values()),
                        )
                    except Exception as rollback_error:
                        try:
                            self.close()
                        except Exception:
                            pass
                        raise ExceptionGroup(
                            "Failed to enroll and roll back a nested widget model",
                            [error, rollback_error],
                        ) from error
                    raise

            detached: list[tuple[object, BridgeComm | None]] = []
            if removed:
                if self._state_context is not None:
                    self._state_context.remove_widgets(removed)
                for widget in removed:
                    identity = id(widget)
                    names = self._graph_observers.pop(identity, ())
                    if names:
                        _unobserve(widget, self._sync_widget_graph, names)
                    self._widgets_by_identity.pop(identity, None)
                    model_id = _model_id(widget, self._protocol_controllers)
                    self._models.pop(model_id, None)
                    comm = self._comms.pop(model_id, None)
                    detached.append((widget, comm))
                    if model_id not in self._pending_removed_model_ids:
                        self._pending_removed_model_ids.append(model_id)

            self._widgets = reachable
            for widget, comm in detached:
                self._restore_notification_gate(widget)
                try:
                    _close_widget(widget, self._protocol_controllers)
                except Exception:
                    pass
                if comm is not None:
                    comm.close()

    def _discard_added_widgets(
        self,
        widgets: list[object],
        model_ids: Mapping[int, str],
    ) -> None:
        if self._state_context is not None:
            self._state_context.remove_widgets(widgets)
        for widget in reversed(widgets):
            identity = id(widget)
            self._restore_notification_gate(widget)
            names = self._graph_observers.pop(identity, ())
            if names:
                try:
                    _unobserve(widget, self._sync_widget_graph, names)
                except Exception:
                    pass
            self._widgets_by_identity.pop(identity, None)
            model_id = model_ids[identity]
            self._models.pop(model_id, None)
            self._pending_models.pop(model_id, None)
            self._comms.pop(model_id, None)
            try:
                _close_widget(widget, self._protocol_controllers)
            except Exception:
                pass

    def _rollback_widget_trait_change(
        self,
        change: object,
        rejected_model_ids: set[str],
    ) -> None:
        if not isinstance(change, Mapping):
            raise RuntimeError("Nested widget change metadata is unavailable")
        owner = change.get("owner")
        name = change.get("name")
        if (
            not isinstance(name, str)
            or self._widgets_by_identity.get(id(owner)) is not owner
        ):
            raise RuntimeError("Nested widget change metadata is invalid")

        rejected_refs = {f"anywidget:{model_id}" for model_id in rejected_model_ids}
        owner_model_id = _model_id(owner, self._protocol_controllers)
        self._messages = [
            message
            for message in self._messages
            if not (
                message.model_id == owner_model_id
                and message.data.get("method") == "update"
                and isinstance(message.data.get("state"), dict)
                and _contains_widget_ref(
                    message.data["state"].get(name),
                    rejected_refs,
                )
            )
        ]

        self._graph_sync_suspended = True
        try:
            setattr(owner, name, change.get("old"))
        finally:
            self._graph_sync_suspended = False


def _claim_widgets(widgets: list[object]) -> None:
    with _claimed_widgets_lock:
        reused = next((widget for widget in widgets if _claim_for(widget)), None)
        if reused is not None:
            for widget in reversed(widgets):
                if _claim_for(widget) is None:
                    _close_unclaimed_widget(widget)
            claim = _claim_for(reused)
            assert claim is not None
            raise WidgetInUseError(
                f"Widget model {claim[1]} was already returned by a widget tool. "
                "Return a fresh root and fresh nested widgets for every tool call."
            )
        for widget in widgets:
            identity = id(widget)
            try:
                reference: Callable[[], object | None] = weakref.ref(
                    widget,
                    lambda expired, identity=identity: _remove_claim(
                        identity,
                        expired,
                    ),
                )
            except TypeError:

                def strong_reference(value: object = widget) -> object:
                    return value

                reference = strong_reference
            _claimed_widgets[identity] = (reference, _model_id(widget))


def _claim_for(
    widget: object,
) -> tuple[Callable[[], object | None], str] | None:
    identity = id(widget)
    claim = _claimed_widgets.get(identity)
    if claim is None:
        return None
    if claim[0]() is widget:
        return claim
    _claimed_widgets.pop(identity, None)
    return None


def _remove_claim(
    identity: int,
    reference: Callable[[], object | None],
) -> None:
    with _claimed_widgets_lock:
        claim = _claimed_widgets.get(identity)
        if claim is not None and claim[0] is reference:
            _claimed_widgets.pop(identity, None)


def _collect_widgets(
    root: AnyWidget,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> list[object]:
    widgets: list[object] = []
    pending: list[object] = [root]
    seen: set[int] = set()
    while pending:
        widget = pending.pop()
        identity = id(widget)
        if identity in seen:
            continue
        seen.add(identity)
        widgets.append(widget)
        for value in _synchronized_values(widget, controllers):
            _collect_nested_widgets(value, pending, controllers)
    return widgets


def _protocol_controller(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> ReprMimeBundle | None:
    if isinstance(widget, AnyWidget):
        return None
    identity = id(widget)
    if controllers is not None:
        cached = controllers.get(identity)
        if cached is not None:
            return cached
    controller = getattr(widget, "_repr_mimebundle_", None)
    if not isinstance(controller, ReprMimeBundle):
        return None
    if controllers is not None:
        controllers[identity] = controller
    return controller


def _model_id(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> str:
    if isinstance(widget, AnyWidget):
        return widget.model_id
    controller = _protocol_controller(widget, controllers)
    if controller is None:
        raise TypeError(f"{type(widget).__name__} is not an AnyWidget-compatible model")
    return controller.model_id


def _synchronized_values(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None,
) -> Iterable[object]:
    if isinstance(widget, AnyWidget):
        for name, trait in widget.traits().items():
            if trait.metadata.get("sync"):
                yield getattr(widget, name)
        return

    controller = _protocol_controller(widget, controllers)
    if controller is None:
        return
    state = controller._get_state(widget, include=None)
    yield from state.values()
    yield from controller._extra_state.values()


def _collect_nested_widgets(
    value: object,
    pending: list[object],
    controllers: dict[int, ReprMimeBundle] | None,
    seen: set[int] | None = None,
) -> None:
    if isinstance(value, AnyWidget) or _protocol_controller(value, controllers):
        pending.append(value)
        return
    if not isinstance(value, (Mapping, list, tuple)):
        return
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    nested = value.values() if isinstance(value, Mapping) else value
    for item in nested:
        _collect_nested_widgets(item, pending, controllers, seen)


def _replace_widget_refs(
    value: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
    seen: set[int] | None = None,
) -> object:
    if isinstance(value, AnyWidget) or _protocol_controller(value, controllers):
        return f"anywidget:{_model_id(value, controllers)}"
    if isinstance(value, Mapping):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError(
                "Synchronized widget state cannot contain recursive mappings"
            )
        seen.add(identity)
        try:
            return {
                key: _replace_widget_refs(item, controllers, seen)
                for key, item in value.items()
            }
        finally:
            seen.remove(identity)
    if isinstance(value, list):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError("Synchronized widget state cannot contain recursive lists")
        seen.add(identity)
        try:
            return [_replace_widget_refs(item, controllers, seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, tuple):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError(
                "Synchronized widget state cannot contain recursive tuples"
            )
        seen.add(identity)
        try:
            return tuple(
                _replace_widget_refs(item, controllers, seen) for item in value
            )
        finally:
            seen.remove(identity)
    return value


def _synced_trait_names(widget: object) -> tuple[str, ...]:
    traits = getattr(widget, "traits", None)
    observe = getattr(widget, "observe", None)
    unobserve = getattr(widget, "unobserve", None)
    if not callable(traits) or not callable(observe) or not callable(unobserve):
        return ()
    return tuple(cast(dict[str, Any], traits(sync=True)))


def _observe(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    observe = getattr(widget, "observe")
    observe(callback, names=names)


def _unobserve(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    unobserve = getattr(widget, "unobserve")
    unobserve(callback, names=names)


def _close_widget(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    if isinstance(widget, AnyWidget):
        widget.close()
        return
    controller = _protocol_controller(widget, controllers)
    if controller is None:
        return
    controller.unsync_object_with_view()
    controller._comm.close()


def _close_unclaimed_widget(widget: object) -> None:
    try:
        _close_widget(widget)
    except Exception:
        pass


def _contains_widget_ref(value: object, references: set[str]) -> bool:
    if isinstance(value, str):
        return value in references
    if isinstance(value, Mapping):
        return any(_contains_widget_ref(item, references) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_widget_ref(item, references) for item in value)
    return False
