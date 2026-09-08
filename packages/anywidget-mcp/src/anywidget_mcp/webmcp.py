"""Expose live AnyWidgets through their host's native model connection."""

from __future__ import annotations

import copy
import inspect
import json
import uuid
import weakref
from functools import lru_cache
from importlib.resources import files
from typing import Any

from anywidget import AnyWidget
from ipywidgets import Widget
from ipywidgets.widgets import widget as widget_module
from traitlets import Dict, HasTraits

from ._projection_json import bounded_mapping
from ._webmcp_schema import public_traits, trait_schema

__all__ = ["enable", "disable", "is_enabled"]

_active: _Instrumentation | None = None


@lru_cache(maxsize=1)
def _browser_source() -> str:
    return files("anywidget_mcp").joinpath("static/webmcp.js").read_text("utf-8")


class _WidgetExposure:
    def __init__(self, widget: AnyWidget) -> None:
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

    def descriptor(self, widget: AnyWidget) -> dict[str, Any]:
        traits = public_traits(widget)
        properties = {name: trait_schema(trait) or {} for name, trait in traits.items()}
        writable = {
            name: schema
            for name, trait in traits.items()
            if not trait.read_only and (schema := trait_schema(trait)) is not None
        }
        return {
            "id": self.id,
            "title": type(widget).__name__,
            "description": (inspect.getdoc(type(widget)) or "")[:1000],
            "properties": properties,
            "writable": writable,
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
        if not isinstance(message, dict) or message.get("kind") != "anywidget-webmcp":
            return
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return
        response: dict[str, Any] = {"kind": "anywidget-webmcp-result", "id": request_id}
        traits = public_traits(widget)
        try:
            operation = message.get("operation")
            if operation == "update":
                state = message.get("state")
                if not isinstance(state, dict) or not state:
                    raise ValueError("Pass at least one synchronized trait to update")
                for name in state:
                    trait = traits.get(name)
                    if trait is None or trait.read_only or trait_schema(trait) is None:
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
            response["result"] = {
                "state": bounded_mapping(widget.get_state(key=tuple(traits)))
            }
        except Exception as error:
            response["error"] = str(error)
        widget.send(response)

    def close(self) -> None:
        widget = self.widget()
        if widget is None:
            return
        widget.on_msg(self.receive, remove=True)
        restore_source = widget.traits()["_esm"] is self.trait
        if restore_source:
            HasTraits.add_traits(widget, _esm=self.original_trait)
        widget.set_trait("_webmcp", None)
        if restore_source and widget.comm is not None:
            widget.send_state("_esm")


class _Instrumentation:
    def __init__(self) -> None:
        self._widgets: weakref.WeakKeyDictionary[AnyWidget, _WidgetExposure] = (
            weakref.WeakKeyDictionary()
        )
        self._original = Widget._call_widget_constructed
        self._original_add_traits = Widget.add_traits
        self._enabled = True

        def constructed(widget: Widget) -> None:
            if self._enabled and isinstance(widget, AnyWidget):
                self._instrument(widget)
            self._original(widget)

        # Subclasses can add synchronized traits after Widget's constructor hook.
        def add_traits(widget: Widget, **traits: Any) -> None:
            self._original_add_traits(widget, **traits)
            if self._enabled and isinstance(widget, AnyWidget):
                exposure = self._widgets.get(widget)
                if exposure is not None:
                    widget.set_trait("_webmcp", exposure.descriptor(widget))

        self._constructed = constructed
        self._add_traits = add_traits
        # Hosts install their own singleton construction callback. Intercept its
        # dispatcher so both existing and later host registrations are preserved.
        setattr(Widget, "_call_widget_constructed", staticmethod(constructed))
        setattr(Widget, "add_traits", add_traits)
        try:
            for widget in list(widget_module._instances.values()):
                if isinstance(widget, AnyWidget):
                    self._instrument(widget)
                    widget.send_state("_esm")
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "Failed to start and restore WebMCP instrumentation",
                    [error, cleanup_error],
                ) from error
            raise

    def _instrument(self, widget: AnyWidget) -> None:
        if widget not in self._widgets:
            exposure = _WidgetExposure(widget)
            self._widgets[widget] = exposure
            widget.set_trait("_webmcp", exposure.descriptor(widget))

    def close(self) -> None:
        """Restore current widgets and stop instrumenting future instances."""
        if not self._enabled:
            return
        self._enabled = False
        if Widget._call_widget_constructed is self._constructed:
            setattr(Widget, "_call_widget_constructed", staticmethod(self._original))
        if Widget.add_traits is self._add_traits:
            setattr(Widget, "add_traits", self._original_add_traits)
        exposures = list(self._widgets.values())
        self._widgets.clear()
        errors: list[Exception] = []
        for exposure in exposures:
            try:
                exposure.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Failed to restore WebMCP widget sources", errors)


def enable() -> None:
    """Enable WebMCP tools for current and future AnyWidget instances.

    Repeated calls leave the active session unchanged. Rendered widgets expose
    read and update tools through their host's native model connection. Python
    may run locally, remotely, or in Pyodide.

    Public synchronized traits are readable. JSON-schema-compatible writable
    traits accept updates validated by Python. Tag a trait ``webmcp=False`` to
    exclude it. State results use an aggregate 8,000-byte JSON budget.

    Browser tools require WebMCP support and permission to register page tools.
    Failed startup restores widget serializers and host construction hooks.
    """
    global _active
    if _active is None:
        _browser_source()
        _active = _Instrumentation()


def disable() -> None:
    """Withdraw WebMCP tools and restore current widgets, leaving them open.

    Repeated calls are harmless. Cleanup attempts every widget before raising
    an ExceptionGroup for host delivery failures. The session is disabled even
    when cleanup reports an error.
    """
    global _active
    active = _active
    _active = None
    if active is not None:
        active.close()


def is_enabled() -> bool:
    """Return whether WebMCP is enabled in this Python session.

    Browser tool availability also depends on rendering and host permissions.
    """
    return _active is not None
