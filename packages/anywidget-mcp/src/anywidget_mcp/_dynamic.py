"""Create AnyWidgets from Python source and manage generated module lifetimes."""

from __future__ import annotations

from collections.abc import Sequence
import sys
import threading
from types import ModuleType
from typing import TypeGuard
import uuid
import weakref

from anywidget import AnyWidget

from ._widget_protocol import close_unclaimed_widget_graphs


class WidgetCreationError(ValueError):
    """Report a bounded diagnostic from generated widget source."""


class _GeneratedModuleLease:
    """Keep a generated module registered until its returned widgets close."""

    def __init__(self, name: str, module: ModuleType) -> None:
        self._name = name
        self._module: ModuleType | None = module
        self._remaining = 0
        self._closed = False
        self._lock = threading.Lock()

    def track(self, widget: AnyWidget) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("The generated AnyWidget module is closed")
            self._remaining += 1
        release = weakref.finalize(widget, self._release)
        original_close = widget.close

        # Generated globals can retain the widget, so close must release the
        # module before garbage collection can break that reference cycle.
        def close() -> None:
            original_close()
            release()

        setattr(widget, "close", close)

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
        self._module = None


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

    Compose children with ``anywidget.WidgetTrait().tag(sync=True)``. In an
    async ``render({ model, el, host, signal })``, resolve the child reference
    with ``await host.getWidget(model.get("child"))`` and mount it with
    ``await child.render({ el: childElement, signal })``. Forward the view's
    abort signal to child renders and DOM listeners. Read child state in Python
    as ``self.child.value``. In JavaScript, get its model with
    ``const childModel = await host.getModel(model.get("child"))`` and read
    ``childModel.get("value")``. Send custom events with
    ``model.send(content)`` and receive them in Python with
    ``self.on_msg(callback)``, where callback receives widget, content, buffers.

    The code runs with the current Python process permissions. Execute untrusted
    source in a sandbox with scoped filesystem, network, and credential access.

    The generated module remains in ``sys.modules`` until every returned widget
    is closed or garbage-collected, preserving module lookup for its live classes.

    Args:
        code: Complete Python source defining the widget classes.
        classnames: Ordered class binding names to instantiate. An empty or
            omitted value selects the last qualifying final namespace binding.

    Returns:
        One widget for one selected class, otherwise an ordered widget sequence.

    Raises:
        WidgetCreationError: Compilation, selection, execution, or construction failed.
            Includes the exception type, generated source line when available,
            and a bounded message. Correct the source or classnames and retry.
    """
    try:
        return _create_anywidget(code, classnames)
    except Exception as error:
        raise WidgetCreationError(_generated_error(error)) from error


def _generated_error(error: Exception) -> str:
    cause = error
    while isinstance(cause, ExceptionGroup):
        cause = cause.exceptions[0]
    line = None
    traceback = cause.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename == "<generated-anywidget>":
            line = traceback.tb_lineno
        traceback = traceback.tb_next
    if isinstance(cause, SyntaxError) and cause.filename == "<generated-anywidget>":
        line = cause.lineno
    message = cause.msg if isinstance(cause, SyntaxError) and cause.msg else str(cause)
    if len(message) > 1_000:
        message = message[:1_000] + "..."
    location = f" at line {line}" if line is not None else ""
    summary = f"{type(cause).__name__}{location}: {message}"
    if cause is not error:
        summary = f"{type(error).__name__}: {summary}"
    return summary


def _create_anywidget(
    code: str,
    classnames: Sequence[str],
) -> AnyWidget | Sequence[AnyWidget]:
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
        try:
            close_unclaimed_widget_graphs(widgets)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Failed to construct and clean up generated AnyWidgets",
                [error, cleanup_error],
            ) from error
        finally:
            lease.close()
        raise

    return widgets[0] if len(widgets) == 1 else widgets


def _resolve_widget_classes(
    module: ModuleType,
    classnames: Sequence[str],
) -> tuple[type[AnyWidget], ...]:
    """Resolve explicit bindings or the last source-defined top-level widget."""

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
