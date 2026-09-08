"""Publish configured widgets and own browser-requested widget creation."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import anywidget
import traitlets as t
from exceptiongroup import BaseExceptionGroup, ExceptionGroup
from ipywidgets.widgets import widget as widget_module

from .._spec import (
    Identity,
    InputSpec,
    TargetSpec,
    classify,
    compile_inputs,
    describe_model,
    rename,
    scan,
)
from .._models import bind_model
from . import hooks
from .host import Host, is_live
from .instrument import _Instrumentation, _browser_source
from .policy import Exposure, WidgetOptions, validate_name
from .._widget_protocol import collect_widgets, replace_widget_refs

Widgets = Sequence[Any] | Mapping[Any, WidgetOptions | Literal[False]]


def _serialize_widgets(widgets: list[Any], _owner: Any) -> Any:
    return replace_widget_refs(widgets)


@dataclass(frozen=True)
class _Creation:
    definition: TargetSpec
    id: str
    identity: Identity
    inputs: InputSpec


class Session(anywidget.AnyWidget):
    """Display WebMCP tools and the widgets created through them.

    Existing widgets retain their owners. close() closes widgets created by
    this session and withdraws its tools. disable() leaves those widgets open.
    """

    _webmcp_active = t.Bool(False).tag(sync=True)
    _webmcp_catalog = t.List(t.Dict()).tag(sync=True)
    _webmcp_widgets = t.List(anywidget.WidgetTrait()).tag(
        sync=True, to_json=_serialize_widgets
    )
    _webmcp_created = t.List(anywidget.WidgetTrait()).tag(
        sync=True, to_json=_serialize_widgets
    )

    def __init__(self, on_close: Callable[[Session], None], host: Host) -> None:
        self._instrumentation: _Instrumentation | None = None
        self._on_close = on_close
        self._host = host
        self._registrations: dict[Any, dict[str, Any] | Literal[False]] = {}
        self._creations: dict[str, _Creation] = {}
        self._origins: weakref.WeakKeyDictionary[anywidget.AnyWidget, Any] = (
            weakref.WeakKeyDictionary()
        )
        self._discover = True
        self._id = uuid.uuid4().hex
        self._replies: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._pending: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._owned: dict[int, object] = {}
        self._unpublished: weakref.WeakSet[anywidget.AnyWidget] = weakref.WeakSet()
        self._closed = False
        self._closing = False
        self._publishing = False
        self._esm = _browser_source() + "\nexport default session;\n"
        super().__init__()
        self.on_msg(self._receive)
        self._host.bind(self.comm, self._close_from_host)

    def configure(self, widgets: Widgets | None, discover: bool | None) -> None:
        if discover is not None and not isinstance(discover, bool):
            raise TypeError("discover must be a boolean")
        if widgets is None:
            entries: Any = ()
        elif isinstance(widgets, Mapping):
            entries = widgets.items()
        elif isinstance(widgets, Sequence) and not isinstance(widgets, (str, bytes)):
            entries = ((target, {}) for target in widgets)
        else:
            raise TypeError(
                "widgets must be a sequence or a mapping of targets to settings"
            )
        registrations = dict(self._registrations)
        for target, settings in entries:
            kind = classify(target)
            if kind == "instance" and not isinstance(target, anywidget.AnyWidget):
                raise TypeError(
                    "WebMCP requires an AnyWidget with a native notebook comm"
                )
            if isinstance(target, Session):
                raise ValueError("A WebMCP session cannot expose itself as a widget")
            if (
                isinstance(target, anywidget.AnyWidget)
                and settings is not False
                and not is_live(target)
            ):
                raise ValueError("WebMCP requires an open widget comm")
            if isinstance(target, anywidget.AnyWidget) and not self._host.owns(target):
                raise ValueError("The widget belongs to another notebook runtime")
            if settings is False:
                registrations[target] = False
                continue
            if not isinstance(settings, Mapping):
                raise TypeError("Widget settings must be a mapping or False")
            options = Exposure().patch(settings)
            previous = registrations.get(target)
            registrations[target] = {
                **(previous or {}),
                **{key: getattr(options, key) for key in settings},
            }
        creations: dict[str, _Creation] = {}
        names: set[str] = set()
        for target, settings in registrations.items():
            if settings is False or isinstance(target, anywidget.AnyWidget):
                continue
            options = Exposure().patch(settings)
            definition = scan(target)
            metadata = rename(
                definition.identity,
                name=options.name,
                title=options.title,
                description=options.description,
            )
            if options.description is not None and not options.description.strip():
                raise ValueError("Creation tool descriptions must contain text")
            if not metadata.description:
                metadata = rename(metadata, description=f"Create {metadata.title}.")
            validate_name(metadata.name)
            if metadata.name in names:
                raise ValueError(
                    f"Duplicate WebMCP creation tool name: {metadata.name!r}"
                )
            names.add(metadata.name)
            inputs = compile_inputs(definition)
            previous_creation = next(
                (
                    entry
                    for entry in self._creations.values()
                    if entry.definition.source is target
                ),
                None,
            )
            identity = previous_creation.id if previous_creation else uuid.uuid4().hex
            creations[identity] = _Creation(definition, identity, metadata, inputs)
        discovery = self._discover if discover is None else discover
        current = self._live_widgets()
        for widget in current:
            options = self._policy(widget, registrations, discovery)
            if options is not None:
                _Instrumentation.preflight(widget, options)
        previous_configuration = (self._registrations, self._creations, self._discover)
        self._registrations = registrations
        self._creations = creations
        self._discover = discovery
        try:
            if self._instrumentation is None:
                self._instrumentation = _Instrumentation(
                    self._policy, self._publish, self._constructed, self._host
                )
            else:
                for widget in current:
                    self._instrumentation.refresh(widget)
            self._webmcp_active = True
            self._publish()
        except BaseException as error:
            self._registrations, self._creations, self._discover = (
                previous_configuration
            )
            errors: list[BaseException] = []
            if self._instrumentation is not None:
                for widget in current:
                    try:
                        self._instrumentation.refresh(widget)
                    except BaseException as rollback_error:
                        errors.append(rollback_error)
                try:
                    self._publish()
                except BaseException as rollback_error:
                    errors.append(rollback_error)
            if errors:
                instrumentation = self._instrumentation
                self._instrumentation = None
                self._on_close(self)
                if instrumentation is not None:
                    try:
                        instrumentation.close()
                    except BaseException as cleanup_error:
                        errors.append(cleanup_error)
                try:
                    self._webmcp_active = False
                    self._webmcp_widgets = []
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
                raise BaseExceptionGroup(
                    "Failed to configure and restore WebMCP exposure", [error, *errors]
                ) from error
            raise

    def _constructed(self, widget: anywidget.AnyWidget) -> bool:
        invocation = hooks.current_creation()
        if (
            invocation is not None
            and invocation.owner.constructed == self._constructed
            and not isinstance(widget, Session)
        ):
            invocation.widgets.append(widget)
            self._unpublished.add(widget)
            return True
        return False

    def _live_widgets(self) -> list[anywidget.AnyWidget]:
        widgets = [
            widget
            for widget in list(widget_module._instances.values())
            if isinstance(widget, anywidget.AnyWidget)
            and is_live(widget)
            and self._host.owns(widget)
        ]
        for target in self._registrations:
            if (
                isinstance(target, anywidget.AnyWidget)
                and is_live(target)
                and self._host.owns(target)
                and target not in widgets
            ):
                widgets.append(target)
        return widgets

    def _policy(
        self,
        widget: anywidget.AnyWidget,
        registrations: dict[Any, dict[str, Any] | Literal[False]] | None = None,
        discover: bool | None = None,
    ) -> Exposure | None:
        if isinstance(widget, Session) or widget in self._unpublished:
            return None
        registrations = self._registrations if registrations is None else registrations
        discover = self._discover if discover is None else discover
        origin = self._origins.get(widget)
        layers = [
            registrations[cls]
            for cls in reversed(type(widget).__mro__)
            if cls in registrations
        ]
        if (
            origin is not None
            and not inspect.isclass(origin)
            and origin in registrations
        ):
            layers.append(registrations[origin])
        if widget in registrations:
            layers.append(registrations[widget])
        if any(layer is False for layer in layers):
            return None
        if not discover and widget not in registrations and origin is None:
            return None
        options = Exposure()
        for layer in layers:
            if isinstance(layer, Mapping):
                options = options.patch(layer)
        return options

    def _publish(self) -> None:
        if self._instrumentation is None or self._publishing or self._closed:
            return
        self._publishing = True
        try:
            with self.hold_sync():
                self._webmcp_widgets = self._instrumentation.widgets()
                self._webmcp_catalog = [
                    {
                        "id": entry.id,
                        "name": f"anywidget_{self._id}_{entry.identity.name}",
                        "title": entry.identity.title,
                        "description": entry.identity.description,
                        "inputSchema": entry.inputs.schema.to_dict(),
                    }
                    for entry in self._creations.values()
                ]
        finally:
            self._publishing = False

    def _receive(self, _widget: Any, message: Any, _buffers: Any) -> None:
        if not isinstance(message, dict) or message.get("kind") != "anywidget-webmcp":
            return
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return
        response = {"kind": "anywidget-webmcp-result", "id": request_id}
        try:
            if not self._webmcp_active:
                raise ValueError("WebMCP is disabled")
            if message.get("operation") != "create":
                raise ValueError("Session tools accept create operations")
            fingerprint = json.dumps(
                [message.get("target"), message.get("arguments")],
                sort_keys=True,
                allow_nan=False,
            )
            if len(fingerprint.encode()) > 65536:
                raise ValueError("Creation arguments exceed 64 KiB")
            previous = self._replies.get(request_id)
            if previous is not None:
                if fingerprint != previous[0]:
                    raise ValueError(
                        "A creation request ID cannot be reused with different arguments"
                    )
                self.send(previous[1])
                return
            pending = self._pending.get(request_id)
            if pending is not None:
                if fingerprint != pending[0]:
                    raise ValueError(
                        "A creation request ID cannot be reused with different arguments"
                    )
                return
            target = message.get("target")
            if not isinstance(target, str):
                raise ValueError("Creation requests require a tool ID")
            entry = self._creations.get(target)
            if entry is None:
                raise ValueError("This creation tool is no longer registered")
            inputs = message.get("arguments")
            if not isinstance(inputs, dict):
                raise ValueError("Tool arguments must be an object")
            arguments = entry.inputs.validate(inputs)
            if len(self._pending) >= 32:
                raise ValueError("Too many widget creations are pending")
        except Exception as error:
            self.send({**response, "error": str(error)})
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._create(entry, arguments, response, fingerprint))
        else:
            task = loop.create_task(
                self._create(entry, arguments, response, fingerprint)
            )
            self._pending[request_id] = (fingerprint, task)
            task.add_done_callback(lambda _task: self._pending.pop(request_id, None))

    async def _create(
        self,
        entry: _Creation,
        arguments: dict[str, Any],
        response: dict[str, Any],
        fingerprint: str,
    ) -> None:
        result: anywidget.AnyWidget | None = None
        instrumentation = self._instrumentation
        if instrumentation is None:
            return
        with hooks.creation(instrumentation) as invocation:
            constructed = invocation.widgets
            try:
                creation = entry.definition.call
                assert creation is not None
                value = creation.invoke(**arguments)
                if inspect.isawaitable(value):
                    value = await value
                if not isinstance(value, anywidget.AnyWidget) or isinstance(
                    value, Session
                ):
                    raise TypeError("Creation functions must return an AnyWidget")
                if (
                    not any(value is widget for widget in constructed)
                    or value in self._origins
                ):
                    raise ValueError(
                        "Creation functions must return a fresh widget constructed by this call"
                    )
                result = value
                if (
                    self._closed
                    or self._instrumentation is not instrumentation
                    or not self._webmcp_active
                    or entry.id not in self._creations
                ):
                    raise ValueError(
                        "The creation tool was withdrawn before it completed"
                    )
                graph = collect_widgets(result)
                if any(
                    isinstance(child, anywidget.AnyWidget)
                    and not self._host.owns(child)
                    for child in graph
                ):
                    raise ValueError(
                        "The widget graph belongs to another notebook runtime"
                    )
                self._unpublished.discard(result)
                self._origins[result] = entry.definition.source
                options = self._policy(result)
                if options is None:
                    raise ValueError("The created widget is excluded from WebMCP")
                options.validate(describe_model(result))
                assert self._instrumentation is not None
                self._instrumentation.refresh(result)
                exposure = self._instrumentation.exposure(result)
                descriptor = exposure.descriptor(result)
                prefix = f"{descriptor['name']}_{exposure.id}"
                tools = {"read": f"{prefix}_read"}
                if descriptor["writable"]:
                    tools["update"] = f"{prefix}_update"
                state = exposure.state(result)
                for child in graph:
                    if any(child is widget for widget in constructed):
                        self._owned[id(child)] = child
                    if isinstance(child, anywidget.AnyWidget):
                        self._unpublished.discard(child)
                        self._instrumentation.refresh(child)
                cleanup_errors = self._close_constructed(
                    [widget for widget in constructed if id(widget) not in self._owned]
                )
                if cleanup_errors:
                    raise ExceptionGroup(
                        "Failed to close temporary widgets", cleanup_errors
                    )
                self._webmcp_created = [*self._webmcp_created, result]
                response["result"] = {
                    "widget_id": exposure.id,
                    "ref": f"anywidget:{result.model_id}",
                    "state": state,
                    "tools": tools,
                }
            except asyncio.CancelledError:
                cleanup_errors = self._close_constructed(constructed)
                response["error"] = "Widget creation was cancelled"
            except Exception as error:
                if result is not None:
                    self._origins.pop(result, None)
                cleanup_errors = self._close_constructed(constructed)
                response["error"] = str(error)
            if cleanup_errors:
                response["error"] += (
                    f". Could not close {len(cleanup_errors)} widget(s). Close the session to retry cleanup."
                )
        self._pending.pop(response["id"], None)
        if not self._closed:
            self._replies[response["id"]] = (fingerprint, response)
        if self.comm is not None:
            self.send(response)

    def _close_constructed(self, widgets: list[Any]) -> list[Exception]:
        errors: list[Exception] = []
        for widget in reversed(widgets):
            try:
                widget.close()
                self._unpublished.discard(widget)
                self._owned.pop(id(widget), None)
            except Exception as error:
                self._owned[id(widget)] = widget
                errors.append(error)
        return errors

    def disable(self) -> None:
        instrumentation = self._instrumentation
        self._instrumentation = None
        for _, task in tuple(self._pending.values()):
            task.cancel()
        errors: list[Exception] = []
        for name, value in (("_webmcp_active", False), ("_webmcp_widgets", [])):
            try:
                self.set_trait(name, value)
            except Exception as error:
                errors.append(error)
        if instrumentation is not None:
            try:
                instrumentation.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Failed to disable WebMCP session", errors)

    def _close_from_host(self) -> None:
        instrumentation = self._instrumentation
        self._instrumentation = None
        try:
            if instrumentation is not None:
                instrumentation.close(send_source=False)
        finally:
            self.close()

    def close(self) -> None:
        if getattr(self, "_closing", True):
            return
        if self._closed and not self._owned and self.comm is None:
            return
        self._closing = True
        self._closed = True
        errors: list[Exception] = []
        try:
            self._on_close(self)
            try:
                self.disable()
            except Exception as error:
                errors.append(error)
            for identity, widget in reversed(list(self._owned.items())):
                try:
                    bind_model(widget).close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._owned.pop(identity, None)
            self._replies.clear()
            self.on_msg(self._receive, remove=True)
            try:
                super().close()
            except Exception as error:
                errors.append(error)
            for name in ("_webmcp_created", "_webmcp_widgets"):
                try:
                    self.set_trait(name, [])
                except Exception as error:
                    errors.append(error)
        finally:
            self._closing = False
        if errors:
            raise ExceptionGroup("Failed to close WebMCP session", errors)
