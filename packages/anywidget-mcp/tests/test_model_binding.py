from __future__ import annotations

from typing import Any

import pytest
from anywidget import AnyWidget
from traitlets import Int, Unicode

from anywidget_mcp._models import bind_model

from .bridge_test_widgets import ChildWidget, ProtocolChild


@pytest.mark.parametrize("widget_type", [ChildWidget, ProtocolChild])
def test_model_binding_reads_live_traits_and_tracks_instance_additions(
    widget_type: type[ChildWidget] | type[ProtocolChild],
) -> None:
    widget = widget_type(value=2)
    binding = bind_model(widget)
    changes: list[int] = []

    def changed(change: Any) -> None:
        changes.append(change["new"])

    try:
        binding.observe(changed, ("value",))
        widget.value = 5
        assert changes == [5]
        assert binding.read(("value",)) == {"value": 5}

        widget.add_traits(label=Unicode("ready").tag(sync=True))
        schema = binding.describe().trait("label").input_schema
        assert schema is not None
        assert schema.to_dict() == {"type": "string"}
        assert binding.read(("label",)) == {"label": "ready"}

        binding.unobserve(changed, ("value",))
        widget.value = 7
        assert changes == [5]
    finally:
        binding.close()


def test_model_binding_serializes_committed_values_with_the_widget_codec() -> None:
    def encode(value: int, widget: AnyWidget) -> dict[str, object]:
        return {"value": value, "unit": getattr(widget, "unit")}

    class Measurement(AnyWidget):
        _esm = "export default { render() {} }"
        value = Int(9).tag(sync=True, to_json=encode)
        unit = Unicode("ms").tag(sync=True)

    widget = Measurement()
    binding = bind_model(widget)
    try:
        assert binding.serialize({"value": 2}) == {"value": {"value": 2, "unit": "ms"}}
        assert binding.read(("value",)) == {"value": {"value": 9, "unit": "ms"}}
        assert binding.describe().trait("value").input_schema is None
    finally:
        binding.close()


def test_model_binding_uses_instance_assigned_native_descriptor() -> None:
    from dataclasses import dataclass

    from anywidget._descriptor import MimeBundleDescriptor

    from anywidget_mcp._spec import scan

    @dataclass
    class Child:
        value: int = 3

    child = Child()
    descriptor = MimeBundleDescriptor(autodetect_observer=False, follow_changes=False)
    setattr(child, "_repr_mimebundle_", descriptor)
    assert scan(child).kind == "instance"
    assert getattr(child, "_repr_mimebundle_") is descriptor
    binding = bind_model(child)
    try:
        assert binding.read(("value",)) == {"value": 3}
        assert bind_model(child).model_id == binding.model_id
        child.value = 5
        assert binding.read(("value",)) == {"value": 5}
    finally:
        binding.close()
