from __future__ import annotations

import threading
from typing import Any

import anywidget
import traitlets
from anywidget._descriptor import MimeBundleDescriptor
from anywidget.experimental import command


class BinaryWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    payload = traitlets.Bytes(bytes([0, 255])).tag(sync=True)
    size = traitlets.Int(2).tag(sync=True)

    @traitlets.observe("payload")
    def _update_size(self, change: traitlets.Bunch) -> None:
        self.size = len(change.new)


class CommandWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    @command
    def reverse(
        self,
        message: object,
        buffers: list[bytes],
    ) -> tuple[dict[str, object], list[bytes]]:
        return {"seen": message}, [buffer[::-1] for buffer in buffers]


class ChildWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    value = traitlets.Int(7).tag(sync=True)


class ParentWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    child = anywidget.WidgetTrait().tag(sync=True)


class NestedParentWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    payload = traitlets.Any().tag(sync=True)


class ProtocolChild(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    value = traitlets.Int(3).tag(sync=True)


class ProtocolContainer(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    payload = traitlets.Any().tag(sync=True)


class ManualProtocolContainer:
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
        autodetect_observer=False,
    )

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def _get_anywidget_state(self, include: set[str] | None) -> dict[str, object]:
        if include is not None and "payload" not in include:
            return {}
        return {"payload": self.payload}


class HybridProtocolContainer(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    marker = traitlets.Int(0).tag(sync=True)

    def __init__(self, payload: object) -> None:
        super().__init__()
        self.payload = payload

    def _get_anywidget_state(self, include: set[str] | None) -> dict[str, object]:
        state = {"marker": self.marker, "payload": self.payload}
        if include is None:
            return state
        return {name: value for name, value in state.items() if name in include}


class UnhashableWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    # Python uses None to mark a class as unhashable.
    __hash__ = None  # pyright: ignore[reportAssignmentType]


class EqualWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualWidget)

    def __hash__(self) -> int:
        return 1


class PausedNotifyWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    trigger = traitlets.Int(0).tag(sync=True)
    value = traitlets.Int(0).tag(sync=True)

    def __init__(self) -> None:
        super().__init__()
        self.pause_value_notification = False
        self.value_stored = threading.Event()
        self.resume_value_notification = threading.Event()

    def _notify_trait(self, name: str, old_value: Any, new_value: Any) -> None:
        if name == "value" and self.pause_value_notification:
            self.value_stored.set()
            if not self.resume_value_notification.wait(1):
                raise RuntimeError("value notification test did not resume")
        super()._notify_trait(name, old_value, new_value)


class ProjectionWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    a = traitlets.Int(0).tag(sync=True)
    b = traitlets.Int(0).tag(sync=True)


class HotSourceWidget(anywidget.AnyWidget):
    _esm = "export default { render() { return 'initial'; } }"


class SharedSourceChild(anywidget.AnyWidget):
    _esm = "export default { render() { return 'shared'; } }"
