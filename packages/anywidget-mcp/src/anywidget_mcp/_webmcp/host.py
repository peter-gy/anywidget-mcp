"""Preserve native notebook ownership during synchronous widget callbacks."""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from importlib import import_module
from typing import Any


def is_live(widget: Any) -> bool:
    return widget.comm is not None and not getattr(widget.comm, "_closed", False)


class Host:
    key: int | None = None

    def is_current(self) -> bool:
        return capture_host().key == self.key

    def owns(self, widget: Any) -> bool:
        if widget.comm is None:
            return self.is_current()
        module = sys.modules.get("marimo._plugins.ui._impl.comm")
        marimo_comm = getattr(module, "MarimoComm", None)
        return not isinstance(marimo_comm, type) or not isinstance(
            widget.comm, marimo_comm
        )

    def scope(self) -> AbstractContextManager[None]:
        return nullcontext()

    def bind(self, comm: Any, on_dispose: Callable[[], None]) -> None:
        comm.on_close(lambda _message: on_dispose())


class _MarimoHost(Host):
    def __init__(self, context: Any, cell_id: str | None) -> None:
        self.context = context
        self.cell_id = cell_id
        self.key = id(context)

    def owns(self, widget: Any) -> bool:
        if widget.comm is None:
            return self.is_current()
        return getattr(widget.comm, "_stream", None) is self.context.stream

    def scope(self) -> AbstractContextManager[None]:
        # Marimo's comm callbacks run between cells. Source files and model
        # cleanup must remain owned by the cell that enabled the session.
        if (
            self.cell_id is not None
            and self.is_current()
            and self.context.cell_id is None
        ):
            return self.context.with_cell_id(self.cell_id)
        return nullcontext()

    def bind(self, comm: Any, on_dispose: Callable[[], None]) -> None:
        super().bind(comm, on_dispose)
        if self.cell_id is None:
            return
        lifecycle = import_module("marimo._runtime.cell_lifecycle_item")

        class Cleanup(lifecycle.CellLifecycleItem):
            def create(self, context: Any) -> None:
                pass

            def dispose(self, context: Any, deletion: bool) -> bool:
                on_dispose()
                return True

        with self.scope():
            self.context.cell_lifecycle_registry.add(Cleanup())


def capture_host() -> Host:
    module = sys.modules.get("marimo._runtime.context")
    get_context = getattr(module, "safe_get_context", None)
    if callable(get_context):
        context = get_context()
        if context is not None:
            return _MarimoHost(context, getattr(context, "cell_id", None))
    return Host()
