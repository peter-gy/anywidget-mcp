from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast, overload

import pytest
from anywidget import AnyWidget
from traitlets import Bytes, Float, Int, List, Unicode, observe

from anywidget_mcp._state import DEFAULT_STATE, StateContext


class StateWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(1).tag(sync=True)
    doubled = Int(2).tag(sync=True)
    payload = Bytes(b"abc").tag(sync=True)
    label = Unicode("ready").tag(sync=True)
    values = List(Int(), [1, 2]).tag(sync=True)
    ratio = Float(1.0).tag(sync=True)
    _secret = Unicode("hidden").tag(sync=True)

    @observe("value")
    def _derive_doubled(self, change: dict[str, Any]) -> None:
        self.doubled = change["new"] * 2


class CountingMapping(Mapping[str, int]):
    def __init__(self, length: int) -> None:
        self.length = length
        self.reads = 0

    def __getitem__(self, key: str) -> int:
        return int(key)

    def __iter__(self) -> Iterator[str]:
        for index in range(self.length):
            self.reads += 1
            yield str(index)

    def __len__(self) -> int:
        return self.length


class CountingSequence(Sequence[int]):
    def __init__(self, length: int) -> None:
        self.length = length
        self.reads = 0

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[int]: ...

    def __getitem__(self, index: int | slice) -> int | Sequence[int]:
        if isinstance(index, slice):
            return range(self.length)[index]
        if index >= self.length:
            raise IndexError(index)
        self.reads += 1
        return index

    def __len__(self) -> int:
        return self.length


class CountingTree(Sequence[object]):
    def __init__(self, child: object, reads: list[int]) -> None:
        self.child = child
        self.reads = reads

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        if isinstance(index, slice):
            return [self.child] * 50
        if index >= 50:
            raise IndexError(index)
        self.reads[0] += 1
        return self.child

    def __len__(self) -> int:
        return 50


class UnknownLengthSequence(Sequence[int]):
    def __init__(self, values: Sequence[int]) -> None:
        self.values = values
        self.reads = 0

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[int]: ...

    def __getitem__(self, index: int | slice) -> int | Sequence[int]:
        if isinstance(index, slice):
            return self.values[index]
        if index >= len(self.values):
            raise IndexError(index)
        self.reads += 1
        return self.values[index]

    def __len__(self) -> int:
        raise TypeError("length is unavailable")


def test_default_projection_is_public_json_safe_synced_state() -> None:
    widget = StateWidget()
    context = StateContext(widget, [widget], DEFAULT_STATE)

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.version == 1
    assert update.state["value"] == 1
    assert update.state["payload"] == {"type": "binary", "bytes": 3}
    assert "_secret" not in update.state
    assert "_esm" not in update.state
    assert "layout" not in update.state
    assert "tooltip" not in update.state


def test_selected_projection_tracks_python_authoritative_observer_state() -> None:
    widget = StateWidget()
    context = StateContext(widget, [widget], ("value", "doubled"))

    try:
        initial = context.take()
        widget.value = 7
        changed = context.take()
        duplicate = context.take()
    finally:
        context.close()
        widget.close()

    assert initial is not None and initial.state == {"doubled": 2, "value": 1}
    assert changed is not None
    assert changed.version == 2
    assert changed.state == {"doubled": 14, "value": 7}
    assert duplicate is None


def test_none_disables_model_state() -> None:
    widget = StateWidget()
    context = StateContext(widget, [widget], None)

    try:
        assert context.take() is None
        widget.value = 9
        assert context.take() is None
    finally:
        context.close()
        widget.close()


def test_callable_projection_summarizes_recursive_and_oversized_values() -> None:
    widget = StateWidget()
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            "recursive": recursive,
            "binary": [b"abc"],
            "large": "x" * 20_000,
            "many": list(range(10_000)),
            "ratio": float("inf"),
        },
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.state["recursive"] == {"self": {"type": "recursive"}}
    assert update.state["binary"] == [{"type": "binary", "bytes": 3}]
    assert update.state["large"] == {
        "type": "string",
        "characters": 20_000,
        "preview": "x" * 240,
    }
    many = update.state["many"]
    assert many["type"] == "sequence"
    assert many["length"] == 10_000
    assert many["omitted"] == 9_950
    assert many["items"][:3] == [0, 1, 2]
    assert update.state["ratio"] == {"type": "float", "value": "inf"}


def test_callable_projection_bounds_collection_reads_and_large_integers() -> None:
    widget = StateWidget()
    mapping = CountingMapping(10_000)
    sequence = CountingSequence(10_000)
    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            "integer": 10**10_000,
            "mapping": mapping,
            "sequence": sequence,
        },
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.state["integer"] == {
        "type": "integer",
        "bits": 33_220,
        "sign": "positive",
    }
    assert mapping.reads == 51
    assert sequence.reads == 51
    assert update.state["mapping"]["_summary"] == {
        "type": "mapping",
        "entries": 10_000,
        "omitted": 9_950,
    }
    assert update.state["sequence"]["length"] == 10_000
    assert update.state["sequence"]["omitted"] == 9_950


def test_projection_summarizes_integers_outside_json_safe_range() -> None:
    widget = StateWidget()
    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            "max_safe": (1 << 53) - 1,
            "positive": 1 << 53,
            "negative": -(1 << 53),
        },
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.state == {
        "max_safe": (1 << 53) - 1,
        "negative": {"type": "integer", "bits": 54, "sign": "negative"},
        "positive": {"type": "integer", "bits": 54, "sign": "positive"},
    }


def test_top_level_projection_mapping_reads_are_bounded() -> None:
    widget = StateWidget()
    projection = CountingMapping(10_000)
    context = StateContext(widget, [widget], lambda _widget: projection)

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert projection.reads == 51
    summaries = [
        value
        for value in update.state.values()
        if isinstance(value, dict) and value.get("type") == "projection"
    ]
    assert summaries == [{"type": "projection", "omitted": 9_950, "traits": ["50"]}]


def test_memoryview_summary_uses_total_byte_size() -> None:
    widget = StateWidget()
    view = memoryview(bytearray(12)).cast("B", shape=(3, 4))
    context = StateContext(widget, [widget], lambda _widget: {"payload": view})

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.state["payload"] == {"type": "binary", "bytes": 12}


def test_projection_has_one_aggregate_traversal_budget() -> None:
    widget = StateWidget()
    reads = [0]
    tree: object = 1
    for _depth in range(6):
        tree = CountingTree(tree, reads)
    context = StateContext(widget, [widget], lambda _widget: {"tree": tree})

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert reads[0] <= 2_000
    assert len(json.dumps(update.state).encode()) < 10_000


def test_exhausted_budget_summarizes_unknown_length_sequence() -> None:
    widget = StateWidget()
    reads = [0]
    tree: object = 1
    for _depth in range(6):
        tree = CountingTree(tree, reads)
    tail = UnknownLengthSequence([1, 2, 3])
    context = StateContext(
        widget,
        [widget],
        lambda _widget: {"tree": tree, "tail": tail},
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert reads[0] + tail.reads <= 2_000
    assert update.state["tail"] == {
        "type": "sequence",
        "items": [],
        "omitted": 1,
        "lengthAtLeast": 1,
    }


def test_callable_projection_observes_dynamically_added_widgets() -> None:
    root = StateWidget()
    child = StateWidget()
    context = StateContext(
        root,
        [root],
        lambda _widget: {"root": root.value, "child": child.value},
    )

    try:
        initial = context.take()
        context.add_widgets([child])
        assert context.take() is None
        child.value = 4
        changed = context.take()
        context.remove_widgets([child])
        assert context.take() is None
        child.value = 8
        removed = context.take()
    finally:
        context.close()
        root.close()
        child.close()

    assert initial is not None
    assert changed is not None
    assert changed.state == {"child": 4, "root": 1}
    assert removed is None


def test_unknown_selected_trait_is_actionable() -> None:
    widget = StateWidget()
    try:
        with pytest.raises(ValueError, match="Unknown state trait.*missing"):
            StateContext(widget, [widget], ("missing",))
    finally:
        widget.close()


def test_callable_projection_must_return_a_mapping() -> None:
    widget = StateWidget()
    projector = cast(Any, lambda _widget: ["invalid"])
    context = StateContext(widget, [widget], projector)

    try:
        with pytest.raises(TypeError, match="must return a mapping"):
            context.take()
    finally:
        context.close()
        widget.close()


def test_aggregate_projection_is_bounded_after_omission_summary() -> None:
    widget = StateWidget()
    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            f"trait-{index}-{'x' * 500}": "y" * 1_000 for index in range(100)
        },
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    encoded = json.dumps(update.state, separators=(",", ":")).encode()
    assert len(encoded) < 10_000
    summaries = [
        value
        for value in update.state.values()
        if isinstance(value, dict) and value.get("type") == "projection"
    ]
    assert summaries and summaries[0]["omitted"] > 0
