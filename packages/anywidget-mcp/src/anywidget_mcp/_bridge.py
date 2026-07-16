from __future__ import annotations

import base64
import copy
import hashlib
import re
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
    _GroupedState,
    _RefreshStatus,
)

_claimed_widgets: dict[int, tuple[Callable[[], object | None], str, bool]] = {}
_claimed_widgets_lock = threading.RLock()
_CLOSED_MODEL_ID_ATTR = "_anywidget_mcp_closed_model_id"
_NOTIFICATION_WAIT_SECONDS = 3.0
_MAX_PROJECTION_REFRESH_RETRIES = 8
_MAX_ASSETS_PER_REQUEST = 128
PROTOCOL_VERSION = 1
_ASSET_KINDS = {"_esm": "esm", "_css": "css"}
_ASSET_ID_PATTERN = re.compile(r"^(esm|css):sha256:[0-9a-f]{64}$")


def _encode_buffer(buffer: bytes | bytearray | memoryview) -> str:
    return base64.b64encode(memoryview(buffer).tobytes()).decode("ascii")


def _decode_buffer(buffer: str) -> bytes:
    return base64.b64decode(buffer, validate=True)


def _asset_id(kind: str, source: str) -> str:
    digest = hashlib.sha256(
        f"anywidget-mcp-asset-v1\0{kind}\0{source}".encode("utf-8")
    ).hexdigest()
    return f"{kind}:sha256:{digest}"


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
    asset_manifest: dict[str, dict[str, Any]]
    removed_model_ids: list[str]
    projection: ProjectionUpdate | None
    projection_error: str | None


@dataclass
class _DetachedModel:
    widget: object
    comm: BridgeComm | None
    comm_closed: bool = False
    gate_restored: bool = False
    widget_closed: bool = False


def _empty_snapshot() -> SessionSnapshot:
    return SessionSnapshot(
        messages=[],
        models={},
        asset_manifest={},
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


class WidgetSessionInitializationError(ExceptionGroup):
    session: WidgetSession


class WidgetClaimCleanupError(ExceptionGroup):
    widgets: tuple[object, ...]


class WidgetSession:
    def __init__(
        self,
        instance_id: str,
        root: AnyWidget,
        state: StateSpec | _DefaultState | _GroupedState = DEFAULT_STATE,
    ) -> None:
        self.instance_id = instance_id
        self.root = root
        self._lock = threading.RLock()
        self._notification_condition = threading.Condition(self._lock)
        self._notification_depths: dict[int, int] = {}
        self._notification_sources: dict[tuple[int, int], int] = {}
        self._active_notification_changes: dict[
            tuple[int, int],
            list[object],
        ] = {}
        self._notify_change_methods: dict[int, Callable[[Any], Any]] = {}
        self._messages: list[WidgetMessage] = []
        self._protocol_controllers: dict[int, ReprMimeBundle] = {}
        self._widgets: list[object] = [root]
        self._widgets_by_identity: dict[int, object] = {id(root): root}
        self._comms: dict[str, BridgeComm] = {}
        self._graph_observers: dict[int, tuple[str, ...]] = {}
        self._pending_models: dict[str, dict[str, Any]] = {}
        self._pending_removed_model_ids: list[str] = []
        self._pending_detached_models: dict[str, _DetachedModel] = {}
        self._announced_detached_models: dict[str, _DetachedModel] = {}
        self._acknowledged_detached_model_ids: set[str] = set()
        self._assets: dict[str, tuple[str, int, str]] = {}
        self._model_source_refs: dict[str, dict[str, str]] = {}
        self._latest_asset_ids: set[str] = set()
        self._pinned_asset_refs: dict[str, int] = {}
        self._graph_sync_suspended = False
        self._closed = False
        self._closing_widgets: dict[int, object] = {}
        self._widget_close_pending: set[int] = set()
        self._state_context: StateContext | None = None
        self._models: dict[str, dict[str, Any]] = {}
        widgets_claimed = False
        try:
            self._widgets = []
            _collect_widgets(
                root,
                self._protocol_controllers,
                self._widgets,
            )
            self._widgets_by_identity = {id(widget): widget for widget in self._widgets}
            _claim_widgets(self._widgets, self._protocol_controllers)
            widgets_claimed = True
            self._install_notification_gates(self._widgets)
            self._models = self._connect_models(self._widgets)
            self._observe_widget_graph(self._widgets)
            context = StateContext.__new__(StateContext)
            self._state_context = context
            context.__init__(
                root,
                self._widgets,
                state,
            )
        except WidgetClaimCleanupError as error:
            self._widgets = []
            self._widgets_by_identity = {}
            self._retain_unclaimed_cleanup(error.widgets)
            try:
                self.close()
            except Exception as cleanup_error:
                initialization_error = WidgetSessionInitializationError(
                    "Failed to initialize and clean up widget session",
                    [error, cleanup_error],
                )
                initialization_error.session = self
                raise initialization_error from error
            raise
        except WidgetInUseError:
            raise
        except Exception as error:
            if not widgets_claimed:
                self._widgets = [
                    widget
                    for widget in self._widgets
                    if _safe_claim_for(widget, self._protocol_controllers) is None
                ]
                self._widgets_by_identity = {
                    id(widget): widget for widget in self._widgets
                }
            try:
                self.close()
            except Exception as cleanup_error:
                initialization_error = WidgetSessionInitializationError(
                    "Failed to initialize and clean up widget session",
                    [error, cleanup_error],
                )
                initialization_error.session = self
                raise initialization_error from error
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

    def acknowledge_model_removals(self, model_ids: Iterable[str]) -> None:
        """Finalize detached models after the browser applies their removals."""
        acknowledged = tuple(dict.fromkeys(model_ids))
        with self._lock:
            if self._closed:
                raise RuntimeError("The widget session is closed")
            unknown = next(
                (
                    model_id
                    for model_id in acknowledged
                    if model_id not in self._announced_detached_models
                    and model_id not in self._acknowledged_detached_model_ids
                ),
                None,
            )
            if unknown is not None:
                raise KeyError(
                    f"Widget model removal is not awaiting acknowledgment: {unknown}"
                )
            detached = {
                model_id: self._announced_detached_models[model_id]
                for model_id in acknowledged
                if model_id in self._announced_detached_models
            }
            failed, errors = self._finalize_detached_models(detached)
            for model_id in detached:
                if model_id not in failed:
                    self._announced_detached_models.pop(model_id, None)
                    self._acknowledged_detached_model_ids.add(model_id)
                    self._messages = [
                        message
                        for message in self._messages
                        if message.model_id != model_id
                    ]
            if errors:
                raise ExceptionGroup(
                    "Failed to finalize detached widget models",
                    errors,
                )
            self._acknowledged_detached_model_ids.difference_update(acknowledged)

    def take_projection(self) -> ProjectionUpdate | None:
        context = self._state_context
        return context.take() if context is not None else None

    def close(self) -> None:
        errors: list[Exception] = []
        with self._notification_condition:
            cleanup_pending = (
                self._state_context is not None
                or bool(self._graph_observers)
                or bool(self._notify_change_methods)
                or bool(self._widget_close_pending)
                or bool(self._comms)
            )
            if self._closed and not cleanup_pending:
                return
            if not self._closed:
                self._closed = True
                self._notification_condition.notify_all()
                try:
                    self._wait_for_notifications("close")
                except Exception as error:
                    errors.append(error)

                active_widgets = list(self._widgets)
                detached_models = [
                    *self._pending_detached_models.values(),
                    *self._announced_detached_models.values(),
                ]
                all_widgets = [
                    *active_widgets,
                    *(detached.widget for detached in detached_models),
                ]
                self._closing_widgets.update(
                    (id(widget), widget) for widget in all_widgets
                )
                self._widget_close_pending.update(
                    id(widget) for widget in active_widgets
                )
                self._widget_close_pending.update(
                    id(detached.widget)
                    for detached in detached_models
                    if not detached.widget_closed
                )
                self._widgets = []
                self._messages.clear()
                self._pending_models.clear()
                self._pending_removed_model_ids.clear()
                self._pending_detached_models.clear()
                self._announced_detached_models.clear()
                self._acknowledged_detached_model_ids.clear()
                self._assets.clear()
                self._model_source_refs.clear()
                self._latest_asset_ids.clear()
                self._pinned_asset_refs.clear()
                self._models.clear()
                self._widgets_by_identity.clear()

            context = self._state_context
            graph_observers = [
                (identity, widget, names)
                for identity, names in self._graph_observers.items()
                if (widget := self._closing_widgets.get(identity)) is not None
            ]
            widgets = list(reversed(self._closing_widgets.values()))
            comms = list(self._comms.items())

        if context is not None:
            try:
                context.close()
            except Exception as error:
                errors.append(error)
            else:
                with self._lock:
                    if self._state_context is context:
                        self._state_context = None
        for identity, widget, names in graph_observers:
            if not names:
                continue
            try:
                _unobserve(widget, self._sync_widget_graph, names)
            except Exception as error:
                errors.append(error)
            else:
                with self._lock:
                    if self._graph_observers.get(identity) == names:
                        self._graph_observers.pop(identity, None)
        for widget in widgets:
            identity = id(widget)
            try:
                self._restore_notification_gate(widget)
            except Exception as error:
                errors.append(error)
            if identity not in self._widget_close_pending:
                continue
            try:
                _close_widget(widget, self._protocol_controllers)
            except Exception as error:
                errors.append(error)
            else:
                with self._lock:
                    self._widget_close_pending.discard(identity)
        for model_id, comm in comms:
            try:
                comm.close()
            except Exception as error:
                errors.append(error)
            else:
                with self._lock:
                    if self._comms.get(model_id) is comm:
                        self._comms.pop(model_id, None)
        with self._lock:
            pending_identities = (
                set(self._graph_observers)
                | set(self._notify_change_methods)
                | self._widget_close_pending
            )
            if self._state_context is not None:
                pending_identities.update(self._closing_widgets)
            for identity, widget in self._closing_widgets.items():
                if identity not in pending_identities:
                    _finalize_claim(widget, self._protocol_controllers)
                    self._protocol_controllers.pop(identity, None)
            self._closing_widgets = {
                identity: widget
                for identity, widget in self._closing_widgets.items()
                if identity in pending_identities
            }
            if not self._closing_widgets:
                self._protocol_controllers.clear()
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
                    self._active_notification_changes.setdefault(
                        source_key,
                        [],
                    ).append(change)
                try:
                    original(change)
                finally:
                    with self._notification_condition:
                        active_changes = self._active_notification_changes[source_key]
                        active_changes.pop()
                        if not active_changes:
                            self._active_notification_changes.pop(source_key)
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

            self._notify_change_methods[identity] = original
            try:
                setattr(widget, "notify_change", notify_change)
            except (AttributeError, TypeError):
                current = getattr(widget, "notify_change", None)
                same_callable = current is original or (
                    getattr(current, "__self__", None)
                    is getattr(original, "__self__", None)
                    and getattr(current, "__func__", None)
                    is getattr(original, "__func__", None)
                )
                if same_callable:
                    self._notify_change_methods.pop(identity, None)
                if isinstance(widget, AnyWidget):
                    raise
                if same_callable:
                    continue
                raise

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

                models, messages, asset_manifest = self._externalize_sources(
                    models,
                    messages,
                )

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

                snapshot = SessionSnapshot(
                    messages=messages,
                    models=models,
                    asset_manifest=asset_manifest,
                    removed_model_ids=removed_model_ids,
                    projection=projection,
                    projection_error=projection_error,
                )
                if full_models:
                    detached = dict(self._pending_detached_models)
                    failed, errors = self._finalize_detached_models(detached)
                    self._pending_detached_models = failed
                    if errors:
                        raise ExceptionGroup(
                            "Failed to finalize detached widget models",
                            errors,
                        )
                else:
                    for model_id in removed_model_ids:
                        detached_model = self._pending_detached_models.pop(
                            model_id,
                            None,
                        )
                        if detached_model is not None:
                            self._announced_detached_models[model_id] = detached_model
                return snapshot

    def _finalize_detached_models(
        self,
        detached: Mapping[str, _DetachedModel],
    ) -> tuple[
        dict[str, _DetachedModel],
        list[Exception],
    ]:
        failed: dict[str, _DetachedModel] = {}
        errors: list[Exception] = []
        for model_id, detached_model in detached.items():
            widget = detached_model.widget
            comm = detached_model.comm
            model_errors: list[Exception] = []
            if comm is not None and not detached_model.comm_closed:
                try:
                    comm.close()
                    if self._comms.get(model_id) is comm:
                        self._comms.pop(model_id, None)
                except Exception as error:
                    model_errors.append(error)
                else:
                    detached_model.comm_closed = True
            if not detached_model.gate_restored:
                try:
                    self._restore_notification_gate(widget)
                except Exception as error:
                    model_errors.append(error)
                else:
                    detached_model.gate_restored = True
            if not detached_model.widget_closed:
                try:
                    _close_widget(widget, self._protocol_controllers)
                except Exception as error:
                    model_errors.append(error)
                else:
                    detached_model.widget_closed = True
            if model_errors:
                failed[model_id] = detached_model
                errors.extend(model_errors)
            else:
                _finalize_claim(widget, self._protocol_controllers)
                self._protocol_controllers.pop(id(widget), None)
        return failed, errors

    def asset_contents(self, asset_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return session-owned source assets addressed by content digest."""
        requested = tuple(dict.fromkeys(asset_ids))
        if len(requested) > _MAX_ASSETS_PER_REQUEST:
            raise ValueError(
                f"A widget asset request may contain at most {_MAX_ASSETS_PER_REQUEST} IDs"
            )
        invalid = next(
            (
                asset_id
                for asset_id in requested
                if not _ASSET_ID_PATTERN.fullmatch(asset_id)
            ),
            None,
        )
        if invalid is not None:
            raise ValueError(f"Invalid widget asset ID: {invalid}")

        with self._lock:
            if self._closed:
                raise RuntimeError("The widget session is closed")
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

    def pin_assets(self, asset_ids: Iterable[str]) -> tuple[str, ...]:
        """Retain snapshot assets while a protocol response remains replayable."""
        pinned = tuple(dict.fromkeys(asset_ids))
        with self._lock:
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

    def release_assets(self, asset_ids: Iterable[str]) -> None:
        """Release assets after their protocol replay entry expires."""
        with self._lock:
            for asset_id in dict.fromkeys(asset_ids):
                count = self._pinned_asset_refs.get(asset_id, 0)
                if count <= 1:
                    self._pinned_asset_refs.pop(asset_id, None)
                else:
                    self._pinned_asset_refs[asset_id] = count - 1
            self._prune_assets()

    def _externalize_sources(
        self,
        models: dict[str, dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        assets = dict(self._assets)
        model_source_refs = {
            model_id: dict(refs)
            for model_id, refs in self._model_source_refs.items()
            if model_id in self._models
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
                    and current_model_id in self._models
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
                if isinstance(model_id, str) and model_id in self._models:
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
        self._prune_assets()
        return wire_models, wire_messages, manifest

    def _prune_assets(self) -> None:
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

    def _externalize_state(
        self,
        state: dict[str, Any],
        referenced: set[str],
        assets: dict[str, tuple[str, int, str]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        wire_state = dict(state)
        source_refs: dict[str, str] = {}
        for trait_name, kind in _ASSET_KINDS.items():
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

    def _restore_notification_gate(self, widget: object) -> None:
        identity = id(widget)
        original = self._notify_change_methods.get(identity)
        if original is not None:
            setattr(widget, "notify_change", original)
            self._notify_change_methods.pop(identity, None)

    def _capture(self, source: object, message: WidgetMessage) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                data = _replace_widget_refs(
                    message.data,
                    self._protocol_controllers,
                )
            except Exception as error:
                source_key = (threading.get_ident(), id(source))
                changes = self._active_notification_changes.get(source_key, ())
                cleanup_errors: list[Exception] = []
                if changes:
                    try:
                        self._rollback_widget_trait_change(changes[-1], set())
                    except Exception as rollback_error:
                        cleanup_errors.append(rollback_error)
                    self._discard_unenrolled_protocol_controllers(changes[-1])
                else:
                    cleanup_errors.append(
                        RuntimeError(
                            "Widget state serialization failed outside a trait change"
                        )
                    )
                if cleanup_errors:
                    try:
                        self.close()
                    except Exception as close_error:
                        cleanup_errors.append(close_error)
                    raise ExceptionGroup(
                        "Failed to serialize or restore widget state",
                        [error, *cleanup_errors],
                    ) from error
                raise
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
            identity = id(widget)
            self._graph_observers[identity] = names
            try:
                _observe(widget, self._sync_widget_graph, names)
            except BaseException as error:
                try:
                    _unobserve(widget, self._sync_widget_graph, names)
                except Exception as cleanup_error:
                    raise BaseExceptionGroup(
                        "Failed to install and remove a widget graph observer",
                        [error, cleanup_error],
                    ) from error
                self._graph_observers.pop(identity, None)
                raise

    def _sync_widget_graph(self, _change: object) -> None:
        with self._lock:
            if self._closed or self._graph_sync_suspended:
                return

            controller_ids = set(self._protocol_controllers)
            try:
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
                removed_model_ids = {
                    id(widget): _model_id(widget, self._protocol_controllers)
                    for widget in removed
                }
            except Exception as error:
                rollback_errors: list[Exception] = []
                try:
                    self._rollback_widget_trait_change(_change, set())
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                self._discard_unenrolled_protocol_controllers(
                    _change,
                    controller_ids,
                )
                if rollback_errors:
                    try:
                        self.close()
                    except Exception as close_error:
                        rollback_errors.append(close_error)
                    raise ExceptionGroup(
                        "Failed to discover or restore nested widget models",
                        [error, *rollback_errors],
                    ) from error
                raise

            if added:
                claimed = False
                try:
                    _claim_widgets(added, self._protocol_controllers)
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
                    cleanup_errors: list[Exception] = []
                    claim_cleanup_failed = isinstance(
                        error,
                        WidgetClaimCleanupError,
                    )
                    if claim_cleanup_failed:
                        self._retain_unclaimed_cleanup(error.widgets)
                    if claimed:
                        cleanup_errors = self._discard_added_widgets(
                            added,
                            added_model_ids,
                        )
                    try:
                        self._rollback_widget_trait_change(
                            _change,
                            set(added_model_ids.values()),
                        )
                    except Exception as rollback_error:
                        cleanup_errors.append(rollback_error)
                    self._discard_unenrolled_protocol_controllers(
                        _change,
                        controller_ids,
                    )
                    if cleanup_errors or claim_cleanup_failed:
                        try:
                            self.close()
                        except Exception as close_error:
                            cleanup_errors.append(close_error)
                        raise ExceptionGroup(
                            "Failed to enroll or clean up a nested widget model",
                            [error, *cleanup_errors],
                        ) from error
                    raise

            if removed:
                try:
                    self._remove_widgets_from_graph(
                        removed,
                        removed_model_ids,
                        _change,
                    )
                except Exception as error:
                    cleanup_errors = (
                        self._discard_added_widgets(added, added_model_ids)
                        if added
                        else []
                    )
                    if cleanup_errors:
                        try:
                            self.close()
                        except Exception as close_error:
                            cleanup_errors.append(close_error)
                        raise ExceptionGroup(
                            "Failed to restore a nested widget replacement",
                            [error, *cleanup_errors],
                        ) from error
                    raise

            self._widgets = reachable

    def _discard_added_widgets(
        self,
        widgets: list[object],
        model_ids: Mapping[int, str],
    ) -> list[Exception]:
        errors: list[Exception] = []
        state_cleanup_failed = False
        if self._state_context is not None:
            try:
                self._state_context.remove_widgets(widgets)
            except Exception as error:
                state_cleanup_failed = True
                errors.append(error)
        for widget in reversed(widgets):
            identity = id(widget)
            model_id = model_ids[identity]
            self._closing_widgets[identity] = widget
            self._widget_close_pending.add(identity)
            try:
                self._restore_notification_gate(widget)
            except Exception as error:
                errors.append(error)
            names = self._graph_observers.get(identity, ())
            if names:
                try:
                    _unobserve(widget, self._sync_widget_graph, names)
                except Exception as error:
                    errors.append(error)
                else:
                    self._graph_observers.pop(identity, None)
            self._widgets_by_identity.pop(identity, None)
            self._models.pop(model_id, None)
            self._model_source_refs.pop(model_id, None)
            self._pending_models.pop(model_id, None)
            self._messages = [
                message for message in self._messages if message.model_id != model_id
            ]
            try:
                _close_widget(widget, self._protocol_controllers)
            except Exception as error:
                errors.append(error)
            else:
                self._widget_close_pending.discard(identity)
            comm = self._comms.get(model_id)
            if comm is not None:
                try:
                    comm.close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._comms.pop(model_id, None)
            pending = (
                state_cleanup_failed
                or identity in self._graph_observers
                or identity in self._notify_change_methods
                or identity in self._widget_close_pending
                or model_id in self._comms
            )
            if not pending:
                self._closing_widgets.pop(identity, None)
                _finalize_claim(widget, self._protocol_controllers)
                self._protocol_controllers.pop(identity, None)
        return errors

    def _retain_unclaimed_cleanup(self, widgets: Iterable[object]) -> None:
        for widget in widgets:
            identity = id(widget)
            self._closing_widgets[identity] = widget
            self._widget_close_pending.add(identity)

    def _remove_widgets_from_graph(
        self,
        widgets: list[object],
        model_ids: Mapping[int, str],
        change: object,
    ) -> None:
        cleanup_errors: list[Exception] = []
        context = self._state_context
        if context is not None:
            try:
                context.remove_widgets(widgets)
            except Exception as error:
                cleanup_errors.append(error)

        unobserved: list[tuple[object, tuple[str, ...]]] = []
        if not cleanup_errors:
            for widget in widgets:
                names = self._graph_observers.get(id(widget), ())
                if not names:
                    continue
                try:
                    _unobserve(widget, self._sync_widget_graph, names)
                except Exception as error:
                    cleanup_errors.append(error)
                else:
                    unobserved.append((widget, names))

        if cleanup_errors:
            restoration_errors: list[Exception] = []
            for widget, names in unobserved:
                try:
                    _observe(widget, self._sync_widget_graph, names)
                except Exception as error:
                    restoration_errors.append(error)
            if context is not None:
                try:
                    context.add_widgets(widgets)
                except Exception as error:
                    restoration_errors.append(error)
            try:
                self._rollback_widget_trait_change(change, set())
            except Exception as error:
                restoration_errors.append(error)
            if restoration_errors:
                try:
                    self.close()
                except Exception as error:
                    restoration_errors.append(error)
                raise ExceptionGroup(
                    "Failed to remove or restore nested widget models",
                    [*cleanup_errors, *restoration_errors],
                )
            if len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            raise ExceptionGroup(
                "Failed to remove nested widget models",
                cleanup_errors,
            )

        for widget in widgets:
            identity = id(widget)
            self._graph_observers.pop(identity, None)
            self._widgets_by_identity.pop(identity, None)
            model_id = model_ids[identity]
            self._models.pop(model_id, None)
            self._model_source_refs.pop(model_id, None)
            self._pending_detached_models[model_id] = _DetachedModel(
                widget=widget,
                comm=self._comms.get(model_id),
            )
            if model_id not in self._pending_removed_model_ids:
                self._pending_removed_model_ids.append(model_id)

    def _discard_unenrolled_protocol_controllers(
        self,
        change: object,
        existing: set[int] | None = None,
    ) -> None:
        owned_identities = set(self._widgets_by_identity)
        owned_identities.update(
            id(detached.widget) for detached in self._pending_detached_models.values()
        )
        owned_identities.update(
            id(detached.widget) for detached in self._announced_detached_models.values()
        )
        owned_identities.update(self._closing_widgets)
        candidates: list[object] = []
        if isinstance(change, Mapping):
            try:
                _collect_nested_widgets(
                    change.get("new"),
                    candidates,
                    self._protocol_controllers,
                )
            except Exception:
                pass
        for widget in candidates:
            identity = id(widget)
            if identity not in owned_identities:
                self._protocol_controllers.pop(identity, None)
        for identity in set(self._protocol_controllers) - (existing or set()):
            if identity not in owned_identities:
                self._protocol_controllers.pop(identity, None)

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
        missing = object()
        rejected_value: object = missing
        try:
            rejected_value = _replace_widget_refs(
                change.get("new"),
                self._protocol_controllers,
            )
        except Exception:
            pass
        self._messages = [
            message
            for message in self._messages
            if not (
                message.model_id == owner_model_id
                and message.data.get("method") == "update"
                and isinstance(message.data.get("state"), dict)
                and (
                    (
                        rejected_refs
                        and _contains_widget_ref(
                            message.data["state"].get(name),
                            rejected_refs,
                        )
                    )
                    or (
                        rejected_value is not missing
                        and name in message.data["state"]
                        and message.data["state"][name] == rejected_value
                    )
                )
            )
        ]

        self._graph_sync_suspended = True
        try:
            setattr(owner, name, change.get("old"))
        finally:
            self._graph_sync_suspended = False


def _claim_widgets(
    widgets: list[object],
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    with _claimed_widgets_lock:
        reused = next(
            (widget for widget in widgets if _claim_for(widget, controllers)),
            None,
        )
        if reused is not None:
            claim = _claim_for(reused, controllers)
            assert claim is not None
            use_error = WidgetInUseError(
                f"Widget model {claim[1]} was already returned by a widget tool. "
                "Return a fresh root and fresh nested widgets for every tool call."
            )
            failed: list[object] = []
            cleanup_errors: list[Exception] = []
            for widget in reversed(widgets):
                if _claim_for(widget, controllers) is None:
                    try:
                        _close_widget(widget, controllers)
                    except Exception as error:
                        failed.append(widget)
                        cleanup_errors.append(error)
            if cleanup_errors:
                claim_error = WidgetClaimCleanupError(
                    "Failed to reject and clean up reused widget graph",
                    [use_error, *cleanup_errors],
                )
                claim_error.widgets = tuple(failed)
                raise claim_error from use_error
            raise use_error
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
                weak = True
            except TypeError:

                def strong_reference(value: object = widget) -> object:
                    return value

                reference = strong_reference
                weak = False
            _claimed_widgets[identity] = (reference, _model_id(widget), weak)


def _claim_for(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> tuple[Callable[[], object | None], str, bool] | None:
    identity = id(widget)
    claim = _claimed_widgets.get(identity)
    if claim is None:
        controller = _protocol_controller(widget, controllers)
        closed_model_id = (
            getattr(controller, _CLOSED_MODEL_ID_ATTR, None)
            if controller is not None
            else None
        )
        if not isinstance(closed_model_id, str):
            return None
        return (lambda: None, closed_model_id, False)
    if claim[0]() is widget:
        return claim
    _claimed_widgets.pop(identity, None)
    return None


def _finalize_claim(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    identity = id(widget)
    with _claimed_widgets_lock:
        claim = _claimed_widgets.get(identity)
        if claim is None or claim[0]() is not widget or claim[2]:
            return
        _claimed_widgets.pop(identity, None)
        controller = _protocol_controller(widget, controllers)
        if controller is not None:
            setattr(controller, _CLOSED_MODEL_ID_ATTR, claim[1])


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
    collected: list[object] | None = None,
) -> list[object]:
    widgets = collected if collected is not None else []
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


def _close_unclaimed_widget_graphs(roots: Iterable[AnyWidget]) -> None:
    """Close fresh widget graphs while preserving models owned by live sessions."""
    controllers: dict[int, ReprMimeBundle] = {}
    widgets: list[object] = []
    seen: set[int] = set()
    for root in roots:
        for widget in _collect_widgets(root, controllers):
            identity = id(widget)
            if identity in seen:
                continue
            seen.add(identity)
            widgets.append(widget)

    errors: list[Exception] = []
    for widget in reversed(widgets):
        if _safe_claim_for(widget, controllers) is not None:
            continue
        try:
            _close_widget(widget, controllers)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("Failed to close unclaimed widget graphs", errors)


def _safe_claim_for(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> tuple[Callable[[], object | None], str, bool] | None:
    try:
        return _claim_for(widget, controllers)
    except Exception:
        return None


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


def _contains_widget_ref(value: object, references: set[str]) -> bool:
    if isinstance(value, str):
        return value in references
    if isinstance(value, Mapping):
        return any(_contains_widget_ref(item, references) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_widget_ref(item, references) for item in value)
    return False
