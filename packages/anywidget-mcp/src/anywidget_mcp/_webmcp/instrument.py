"""Expose live AnyWidgets through their host's native model connection."""

from __future__ import annotations

import copy
import json
import uuid
import weakref
from functools import lru_cache
from importlib.resources import files
from typing import Any
from collections.abc import Callable

from .policy import Exposure
from .host import Host, is_live
from . import hooks

from exceptiongroup import BaseExceptionGroup, ExceptionGroup
from anywidget import AnyWidget
from ipywidgets.widgets import widget as widget_module
from traitlets import Dict, HasTraits

from .._projection_json import bounded_mapping
from .._spec import describe_model, rename
from .._models import bind_model


@lru_cache(maxsize=1)
def _browser_source() -> str:
    return files("anywidget_mcp").joinpath("static/webmcp.js").read_text("utf-8")


class _WidgetExposure:
    def __init__(self, widget: AnyWidget, options: Exposure) -> None:
        self.options = options
        self.widget = weakref.ref(widget)
        self.id = uuid.uuid4().hex
        metadata_trait = widget.traits().get("_webmcp")
        if metadata_trait is not None and not metadata_trait.metadata.get(
            "_anywidget_mcp_descriptor"
        ):
            raise ValueError("The _webmcp trait is reserved for WebMCP instrumentation")
        self.original_trait = widget.traits()["_esm"]
        self.trait = copy.copy(self.original_trait)
        self.trait.metadata = dict(self.original_trait.metadata)
        self.trait.tag(to_json=self.serialize)

    def attach(self, widget: AnyWidget) -> None:
        metadata_trait = widget.traits().get("_webmcp")
        # Widget.add_traits also appends keys and sends immediately. Replacing an
        # existing descriptor must preserve the host's first comm-open snapshot.
        HasTraits.add_traits(
            widget,
            _esm=self.trait,
            _webmcp=metadata_trait
            or Dict(default_value=None, allow_none=True, read_only=True).tag(
                sync=True, _anywidget_mcp_descriptor=True
            ),
        )
        if "_webmcp" not in widget.keys:
            widget.keys.append("_webmcp")
        widget.on_msg(self.receive)

    def state(self, widget: AnyWidget) -> dict[str, Any]:
        binding = bind_model(widget)
        traits = self.options.select(binding.describe())
        return bounded_mapping(binding.read(tuple(trait.name for trait in traits)))

    def descriptor(self, widget: AnyWidget) -> dict[str, Any]:
        model = describe_model(widget)
        identity = rename(
            model.identity,
            name=self.options.name,
            title=self.options.title,
            description=self.options.description,
        )
        return {
            "id": self.id,
            "name": self.options.name or "anywidget",
            "title": identity.title,
            "description": identity.description[:1000],
            "properties": {
                trait.name: trait.input_schema.to_dict()
                if trait.input_schema is not None
                else {}
                for trait in self.options.select(model)
            },
            "writable": {
                trait.name: trait.input_schema.to_dict()
                for trait in self.options.writable_traits(model)
                if trait.input_schema is not None
            },
        }

    def serialize(self, source: Any, widget: AnyWidget) -> str:
        serializer = self.original_trait.metadata.get("to_json")
        if serializer is not None:
            source = serializer(source, widget)
        return (
            _browser_source()
            + "\nexport default () => instrument("
            + json.dumps(str(source))
            + ");\n"
        )

    def receive(self, widget: AnyWidget, message: Any, buffers: Any) -> None:
        if getattr(widget, "_webmcp", None) is None:
            return
        if not isinstance(message, dict) or message.get("kind") != "anywidget-webmcp":
            return
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return
        response: dict[str, Any] = {"kind": "anywidget-webmcp-result", "id": request_id}
        binding = bind_model(widget)
        model = binding.describe()
        traits = {trait.name: trait for trait in self.options.select(model)}
        writable = {trait.name for trait in self.options.writable_traits(model)}
        try:
            operation = message.get("operation")
            if operation == "update":
                state = message.get("state")
                if not isinstance(state, dict) or not state:
                    raise ValueError("Pass at least one synchronized trait to update")
                for name in state:
                    if name not in writable:
                        raise ValueError(
                            f"Trait {name!r} is not writable through WebMCP"
                        )
                try:
                    with widget.hold_sync():
                        widget.set_state(state)
                finally:
                    # A tool sends no optimistic frontend edit. Publish canonical
                    # values even when validation rejects or normalizes an input.
                    widget.send_state(tuple(traits))
            elif operation != "read":
                raise ValueError(f"Unknown WebMCP operation: {operation!r}")
            response["result"] = {"state": bounded_mapping(binding.read(tuple(traits)))}
        except Exception as error:
            response["error"] = str(error)
        widget.send(response)

    def close(self, *, send_source: bool = True) -> None:
        widget = self.widget()
        if widget is None:
            return
        widget.on_msg(self.receive, remove=True)
        restore_source = widget.traits()["_esm"] is self.trait
        if restore_source:
            HasTraits.add_traits(widget, _esm=self.original_trait)
        widget.set_trait("_webmcp", None)
        if send_source and restore_source and widget.comm is not None:
            widget.send_state("_esm")


class _Instrumentation:
    @staticmethod
    def preflight(widget: AnyWidget, options: Exposure) -> None:
        options.validate(describe_model(widget))
        exposure = _WidgetExposure(widget, options)
        exposure.descriptor(widget)
        exposure.serialize(getattr(widget, "_esm"), widget)

    def __init__(
        self,
        policy: Callable[[AnyWidget], Exposure | None],
        changed: Callable[[], None],
        on_constructed: Callable[[AnyWidget], bool],
        host: Host,
    ) -> None:
        self.policy = policy
        self.changed = changed
        self.constructed = on_constructed
        self.host = host
        self._pending: weakref.WeakSet[AnyWidget] = weakref.WeakSet()
        self._widgets: weakref.WeakKeyDictionary[AnyWidget, _WidgetExposure] = (
            weakref.WeakKeyDictionary()
        )
        self.enabled = True
        hooks.subscribe(self)
        try:
            for widget in list(widget_module._instances.values()):
                if (
                    isinstance(widget, AnyWidget)
                    and is_live(widget)
                    and self.host.owns(widget)
                ):
                    self.refresh(widget)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "Failed to start and restore WebMCP instrumentation",
                    [error, cleanup_error],
                ) from error
            raise

    def tracks(self, widget: AnyWidget) -> bool:
        return widget in self._widgets

    def prepare(self, widget: AnyWidget) -> None:
        pending = self.constructed(widget)
        if not self.enabled:
            return
        if pending:
            exposure = _WidgetExposure(widget, Exposure())
            exposure.attach(widget)
            self._widgets[widget] = exposure
            self._pending.add(widget)
            widget.observe(self._comm_changed, names="comm")
        else:
            self.refresh(widget)

    def refresh(self, widget: AnyWidget) -> None:
        if not self.enabled:
            return
        with self.host.scope():
            self._refresh(widget)

    def _refresh(self, widget: AnyWidget) -> None:
        options = self.policy(widget)
        exposure = self._widgets.get(widget)
        if options is None:
            if widget in self._pending:
                return
            if exposure is not None:
                del self._widgets[widget]
                widget.unobserve(self._comm_changed, names="comm")
                exposure.close()
                self.changed()
            return
        self._pending.discard(widget)
        fresh = exposure is None
        if exposure is None:
            exposure = _WidgetExposure(widget, options)
            exposure.attach(widget)
            self._widgets[widget] = exposure
            widget.observe(self._comm_changed, names="comm")
        else:
            exposure.options = options
        widget.set_trait("_webmcp", exposure.descriptor(widget))
        if fresh and widget.comm is not None:
            widget.send_state("_esm")
        self.changed()

    def _comm_changed(self, change: Any) -> None:
        self.changed()

    def widgets(self) -> list[AnyWidget]:
        return [
            widget
            for widget in self._widgets
            if is_live(widget) and self.policy(widget) is not None
        ]

    def exposure(self, widget: AnyWidget) -> _WidgetExposure:
        return self._widgets[widget]

    def close(self, *, send_source: bool = True) -> None:
        """Restore current widgets and stop instrumenting future instances."""
        if not self.enabled:
            return
        self.enabled = False
        hooks.unsubscribe(self)
        exposures = list(self._widgets.values())
        self._widgets.clear()
        errors: list[Exception] = []
        for exposure in exposures:
            try:
                widget = exposure.widget()
                if widget is not None:
                    widget.unobserve(self._comm_changed, names="comm")
                with self.host.scope():
                    exposure.close(send_source=send_source)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Failed to restore WebMCP widget sources", errors)
