from __future__ import annotations

from collections.abc import Sequence
import sys
import threading
from types import ModuleType
from typing import TypeGuard
import uuid
import weakref

from anywidget import AnyWidget


class _GeneratedModuleLease:
    def __init__(self, name: str, module: ModuleType) -> None:
        self._name = name
        self._module = module
        self._remaining = 0
        self._closed = False
        self._lock = threading.Lock()

    def track(self, widget: AnyWidget) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("The generated AnyWidget module is closed")
            self._remaining += 1
        weakref.finalize(widget, self._release)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._remove_module()

    def _release(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._remaining -= 1
            if self._remaining:
                return
            self._closed = True
            self._remove_module()

    def _remove_module(self) -> None:
        if sys.modules.get(self._name) is self._module:
            sys.modules.pop(self._name, None)


def create_anywidget(
    code: str,
    *,
    classnames: Sequence[str] = (),
) -> AnyWidget | Sequence[AnyWidget]:
    """Execute Python source and instantiate selected AnyWidget classes.

    Pass ``classnames`` to select final top-level bindings from the executed
    module. Names are resolved and instantiated in the provided order. Every
    name must exist and bind an ``AnyWidget`` subclass with a zero-argument
    constructor.

    When ``classnames`` is empty or omitted, the factory instantiates the last
    qualifying binding in the final module namespace. A binding qualifies when
    it refers to a source-defined top-level ``AnyWidget`` class. Pass names
    explicitly when the source defines more than one widget.

    The code runs with the MCP server process permissions. Run this factory in
    a sandbox with scoped filesystem, network, credential, and process access.

    Args:
        code: Complete Python source defining the widget classes.
        classnames: Ordered class binding names to instantiate. An empty or
            omitted value selects the last qualifying final namespace binding.

    Returns:
        One widget for one selected class, otherwise an ordered widget sequence.

    Raises:
        ValueError: If a requested name is missing or no fallback class exists.
        TypeError: If a requested name does not bind an AnyWidget subclass.
        SyntaxError: If the source cannot be compiled.
        Exception: An exception raised by source execution or widget construction.
    """
    module_name = f"_anywidget_mcp_generated_{uuid.uuid4().hex}"
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    lease = _GeneratedModuleLease(module_name, module)

    try:
        exec(compile(code, "<generated-anywidget>", "exec"), module.__dict__)
    except BaseException:
        lease.close()
        raise

    try:
        widget_classes = _resolve_widget_classes(module, classnames)
    except BaseException:
        lease.close()
        raise

    widgets: list[AnyWidget] = []
    try:
        for widget_class in widget_classes:
            widget = widget_class()
            if not isinstance(widget, AnyWidget):
                raise TypeError(
                    f"Generated class {widget_class.__name__} constructed "
                    f"{type(widget).__name__}, expected AnyWidget"
                )
            widgets.append(widget)
        for widget in widgets:
            lease.track(widget)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        for widget in reversed(widgets):
            try:
                widget.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        lease.close()
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Failed to construct and clean up generated AnyWidgets",
                [error, *cleanup_errors],
            ) from error
        raise

    return widgets[0] if len(widgets) == 1 else widgets


def _resolve_widget_classes(
    module: ModuleType,
    classnames: Sequence[str],
) -> tuple[type[AnyWidget], ...]:
    if not classnames:
        candidates = tuple(
            value
            for value in module.__dict__.values()
            if _is_widget_class(value)
            and value.__module__ == module.__name__
            and "." not in value.__qualname__
        )
        if not candidates:
            raise ValueError(
                "code must define at least one top-level AnyWidget subclass "
                "when classnames is omitted"
            )
        return (candidates[-1],)

    resolved: list[type[AnyWidget]] = []
    for index, name in enumerate(classnames):
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"classnames[{index}] must be a valid Python identifier")
        if name not in module.__dict__:
            raise ValueError(f"classnames[{index}] {name!r} was not found")
        value = module.__dict__[name]
        if not _is_widget_class(value):
            raise TypeError(
                f"classnames[{index}] {name!r} must bind an AnyWidget subclass, "
                f"got {type(value).__name__}"
            )
        resolved.append(value)
    return tuple(resolved)


def _is_widget_class(value: object) -> TypeGuard[type[AnyWidget]]:
    return (
        isinstance(value, type)
        and issubclass(value, AnyWidget)
        and value is not AnyWidget
    )
