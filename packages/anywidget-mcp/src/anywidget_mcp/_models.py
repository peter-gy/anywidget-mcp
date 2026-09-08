"""Bind live AnyWidget operations to shared model descriptions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Generic, TypeVar, cast

from anywidget import AnyWidget
from anywidget._descriptor import MimeBundleDescriptor, ReprMimeBundle
from anywidget._util import put_buffers

from ._spec import describe_model
from ._spec.ports import CommPort, ModelBinding
from ._spec.types import ModelSpec

ModelT = TypeVar("ModelT")


def protocol_controller(
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
    if isinstance(controller, MimeBundleDescriptor):
        controller = controller.__get__(widget, type(widget))
    if not isinstance(controller, ReprMimeBundle):
        return None
    if controllers is not None:
        controllers[identity] = controller
    return controller


def bind_model(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> ModelBinding:
    """Resolve a live model, caching descriptor controllers for its owner."""
    if isinstance(widget, AnyWidget):
        return _NativeModel(widget)
    controller = protocol_controller(widget, controllers)
    if controller is None:
        raise TypeError(f"{type(widget).__name__} is not an AnyWidget-compatible model")
    return _DescriptorModel(widget, controller)


class _Model(Generic[ModelT]):
    def __init__(self, source: ModelT) -> None:
        self.source = source

    def describe(self) -> ModelSpec:
        return describe_model(self.source)

    def observe(self, callback: Callable[[Any], None], names: tuple[str, ...]) -> None:
        getattr(self.source, "observe")(callback, names=names)

    def unobserve(
        self, callback: Callable[[Any], None], names: tuple[str, ...]
    ) -> None:
        getattr(self.source, "unobserve")(callback, names=names)


class _NativeModel(_Model[AnyWidget]):
    @property
    def model_id(self) -> str:
        return self.source.model_id

    def synchronized_values(self) -> Iterable[object]:
        return self.source.trait_values(sync=True).values()

    def read(self, names: tuple[str, ...]) -> Mapping[str, Any]:
        return self.source.get_state(key=names)

    def serialize(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        widget = self.source
        return {
            name: widget.trait_metadata(name, "to_json", widget._trait_to_json)(
                value, widget
            )
            for name, value in values.items()
        }

    def connect(self, comm: CommPort) -> None:
        widget = self.source
        old_comm = widget.comm
        if old_comm is not None:
            old_comm.on_msg(None)
            old_comm.close()
        widget.comm = comm
        comm.on_msg(self._receive)

    def _receive(self, message: dict[str, Any]) -> None:
        widget = self.source
        if type(widget)._handle_msg is not AnyWidget._handle_msg:
            widget._handle_msg(message)
            return
        data = message["content"]["data"]
        if data.get("method") == "update" and "state" in data:
            state = data["state"]
            if "buffer_paths" in data:
                put_buffers(state, data["buffer_paths"], message["buffers"])
            # Native notebook handlers swallow validation errors. The bound comm
            # must reject the update before its consumer accepts the echo.
            widget.set_state(state)
        else:
            widget._handle_msg(message)

    def send_state(self) -> None:
        self.source.send_state()

    def close(self) -> None:
        self.source.close()


class _DescriptorModel(_Model[object]):
    def __init__(self, source: object, controller: ReprMimeBundle) -> None:
        super().__init__(source)
        self.controller = controller

    @property
    def model_id(self) -> str:
        return self.controller.model_id

    def synchronized_values(self) -> Iterable[object]:
        yield from self.controller._get_state(self.source, include=None).values()
        yield from self.controller._extra_state.values()

    def read(self, names: tuple[str, ...]) -> Mapping[str, Any]:
        state = {
            **self.controller._get_state(self.source, include=set(names)),
            **self.controller._extra_state,
        }
        return {name: value for name, value in state.items() if name in names}

    def serialize(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(values)

    def connect(self, comm: CommPort) -> None:
        old_comm = self.controller._comm
        incoming = getattr(old_comm, "_msg_callback", None)
        if old_comm is not None:
            old_comm.on_msg(None)
            old_comm.close()
        cast(Any, self.controller)._comm = comm
        comm.on_msg(incoming)

    def send_state(self) -> None:
        self.controller.send_state()

    def close(self) -> None:
        self.controller.unsync_object_with_view()
        self.controller._comm.close()
