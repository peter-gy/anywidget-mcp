from __future__ import annotations

import copy
import threading
from collections.abc import Iterable, Mapping
from typing import Any, cast

from anywidget import AnyWidget
from anywidget._descriptor import ReprMimeBundle

from ._comm import BridgeComm, WidgetMessage
from ._detachment import DetachedModels
from ._model_connection import connect_models
from ._notifications import NotificationGate
from ._session_types import SessionSnapshot, empty_snapshot as _empty_snapshot
from ._source_assets import SourceAssets
from ._state import (
    DEFAULT_STATE,
    ProjectionUpdate,
    StateContext,
    StateSpec,
    _DefaultState,
    _GroupedState,
    _RefreshStatus,
)
from ._widget_protocol import (
    WidgetClaimCleanupError,
    WidgetInUseError,
    claim_widgets as _claim_widgets,
    close_widget as _close_widget,
    collect_nested_widgets as _collect_nested_widgets,
    collect_widgets as _collect_widgets,
    contains_widget_ref as _contains_widget_ref,
    finalize_claim as _finalize_claim,
    model_id as _model_id,
    observe as _observe,
    replace_widget_refs as _replace_widget_refs,
    safe_claim_for as _safe_claim_for,
    synced_trait_names as _synced_trait_names,
    unobserve as _unobserve,
)

_MAX_PROJECTION_REFRESH_RETRIES = 8
PROTOCOL_VERSION = 1


class WidgetSessionInitializationError(ExceptionGroup):
    session: WidgetSession


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
        self._notifications = NotificationGate(
            self._lock,
            is_closed=lambda: self._closed,
        )
        self._notification_condition = self._notifications.condition
        self._messages: list[WidgetMessage] = []
        self._protocol_controllers: dict[int, ReprMimeBundle] = {}
        self._widgets: list[object] = [root]
        self._widgets_by_identity: dict[int, object] = {id(root): root}
        self._comms: dict[str, BridgeComm] = {}
        self._graph_observers: dict[int, tuple[str, ...]] = {}
        self._pending_models: dict[str, dict[str, Any]] = {}
        self._pending_removed_model_ids: list[str] = []
        self._detached = DetachedModels()
        self._sources = SourceAssets()
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
        with self._lock:
            if self._closed:
                raise RuntimeError("The widget session is closed")
            self._messages = self._detached.acknowledge(
                model_ids,
                messages=self._messages,
                comms=self._comms,
                controllers=self._protocol_controllers,
                restore_gate=self._restore_notification_gate,
            )

    def take_projection(self) -> ProjectionUpdate | None:
        context = self._state_context
        return context.take() if context is not None else None

    def current_projection(self) -> ProjectionUpdate | None:
        retries = 0
        while True:
            with self._notification_condition:
                if self._closed:
                    return None
                self._wait_for_notifications("read state")
                context = self._state_context
            if context is None:
                return None
            status = context.refresh()
            with self._notification_condition:
                if self._closed:
                    return None
                self._wait_for_notifications("read state")
                if context is not self._state_context:
                    continue
                if context.should_retry_refresh(status):
                    retries += 1
                    if retries < _MAX_PROJECTION_REFRESH_RETRIES:
                        continue
                    context.reject_unstable()
                return context.current_cached()

    def close(self) -> None:
        errors: list[Exception] = []
        with self._notification_condition:
            cleanup_pending = (
                self._state_context is not None
                or bool(self._graph_observers)
                or self._notifications.has_pending_restores
                or bool(self._widget_close_pending)
                or bool(self._comms)
            )
            if self._closed and not cleanup_pending:
                return
            if not self._closed:
                self._closed = True
                self._notifications.notify_all()
                try:
                    self._wait_for_notifications("close")
                except Exception as error:
                    errors.append(error)

                active_widgets = list(self._widgets)
                detached_models = self._detached.values()
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
                self._detached.clear()
                self._sources.clear()
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
                | self._notifications.pending_identities
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
        self._notifications.wait(action)

    def _install_notification_gates(self, widgets: Iterable[object]) -> None:
        self._notifications.install(widgets)

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

                if (
                    context is not None
                    and context is self._state_context
                    and context.should_retry_refresh(status)
                ):
                    retries += 1
                    if retries < _MAX_PROJECTION_REFRESH_RETRIES:
                        continue
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
                    self._detached.finalize_pending(
                        comms=self._comms,
                        controllers=self._protocol_controllers,
                        restore_gate=self._restore_notification_gate,
                    )
                else:
                    self._detached.announce(removed_model_ids)
                return snapshot

    def asset_contents(self, asset_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return session-owned source assets addressed by content digest."""
        with self._lock:
            if self._closed:
                raise RuntimeError("The widget session is closed")
            return self._sources.contents(asset_ids)

    def pin_assets(self, asset_ids: Iterable[str]) -> tuple[str, ...]:
        """Retain snapshot assets while a protocol response remains replayable."""
        with self._lock:
            return self._sources.pin(asset_ids)

    def release_assets(self, asset_ids: Iterable[str]) -> None:
        """Release assets after their protocol replay entry expires."""
        with self._lock:
            self._sources.release(asset_ids)

    def _externalize_sources(
        self,
        models: dict[str, dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        return self._sources.externalize(
            models,
            messages,
            live_model_ids=set(self._models),
        )

    def _restore_notification_gate(self, widget: object) -> None:
        self._notifications.restore(widget)

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
                changes = self._notifications.active_changes(source_key)
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
                self._notifications.source_depth((threading.get_ident(), id(source)))
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
            return connect_models(
                widgets,
                controllers=self._protocol_controllers,
                comms=self._comms,
                messages=self._messages,
                capture=self._capture,
            )
        finally:
            self._graph_sync_suspended = was_suspended

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
            self._sources.forget_models((model_id,))
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
                or self._notifications.has_gate(identity)
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
            self._sources.forget_models((model_id,))
            self._detached.add(model_id, widget, self._comms.get(model_id))
            if model_id not in self._pending_removed_model_ids:
                self._pending_removed_model_ids.append(model_id)

    def _discard_unenrolled_protocol_controllers(
        self,
        change: object,
        existing: set[int] | None = None,
    ) -> None:
        owned_identities = set(self._widgets_by_identity)
        owned_identities.update(self._detached.identities())
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
