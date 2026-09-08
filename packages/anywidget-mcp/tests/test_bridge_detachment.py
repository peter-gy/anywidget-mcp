from __future__ import annotations

import pytest

from exceptiongroup import ExceptionGroup
from anywidget._descriptor import MimeBundleDescriptor

from anywidget_mcp._bridge import WidgetInUseError, WidgetSession
from anywidget_mcp._state import StateContext

from .bridge_test_widgets import NestedParentWidget


def test_state_cleanup_failure_retains_nonweak_protocol_claim() -> None:
    class SlottedProtocolChild:
        __slots__ = ("observers", "unobserve_attempts", "value")

        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self) -> None:
            self.value = 1
            self.observers: list[tuple[object, tuple[str, ...]]] = []
            self.unobserve_attempts = 0

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            return {"value": self.value}

        def trait_names(self) -> list[str]:
            return ["value"]

        def observe(self, callback: object, *, names: tuple[str, ...]) -> None:
            self.observers.append((callback, names))

        def unobserve(self, callback: object, *, names: tuple[str, ...]) -> None:
            if isinstance(getattr(callback, "__self__", None), StateContext):
                self.unobserve_attempts += 1
                if self.unobserve_attempts == 1:
                    raise RuntimeError("state observer cleanup failed")
            self.observers.remove((callback, names))

    child = SlottedProtocolChild()
    first_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        first = WidgetSession("first", first_root, lambda _widget: {})

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        first.close()

    second_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        with pytest.raises(WidgetInUseError, match="fresh nested widgets"):
            WidgetSession("second", second_root, lambda _widget: {})

    first.close()
    third_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        second = WidgetSession("second", third_root, lambda _widget: {})
    second.close()

    assert child.observers == []
