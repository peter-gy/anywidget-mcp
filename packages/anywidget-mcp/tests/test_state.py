from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from typing import Any, cast, overload

import pytest
from anywidget import AnyWidget
from anywidget._descriptor import MimeBundleDescriptor
from traitlets import All, Bytes, Float, Int, List, Unicode, observe
from traitlets.utils.sentinel import Sentinel

from anywidget_mcp import StateProjection
from anywidget_mcp._bridge import WidgetSession, WidgetSessionInitializationError
from anywidget_mcp._state import DEFAULT_STATE, StateContext

from .bridge_test_widgets import NestedParentWidget


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


class ObservationWidget(StateWidget):
    def __init__(self) -> None:
        super().__init__()
        self.state_observers: dict[str, int] = {}

    def observe(
        self,
        handler: Callable[..., Any],
        names: Sentinel | str | Collection[Sentinel | str] = All,
        type: Sentinel | str = "change",
    ) -> None:
        super().observe(handler, names=names, type=type)
        if not hasattr(self, "state_observers") or not isinstance(
            getattr(handler, "__self__", None), StateContext
        ):
            return
        for name in self._names(names):
            self.state_observers[name] = self.state_observers.get(name, 0) + 1

    def unobserve(
        self,
        handler: Callable[..., Any],
        names: Sentinel | str | Collection[Sentinel | str] = All,
        type: Sentinel | str = "change",
    ) -> None:
        super().unobserve(handler, names=names, type=type)
        if not hasattr(self, "state_observers") or not isinstance(
            getattr(handler, "__self__", None), StateContext
        ):
            return
        for name in self._names(names):
            remaining = self.state_observers.get(name, 0) - 1
            if remaining > 0:
                self.state_observers[name] = remaining
            else:
                self.state_observers.pop(name, None)

    @staticmethod
    def _names(names: Any) -> tuple[str, ...]:
        if isinstance(names, str):
            return (names,)
        return tuple(name for name in names if isinstance(name, str))


class RegistrationMutationWidget(StateWidget):
    def __init__(self) -> None:
        super().__init__()
        self.mutate_during_state_observe = True

    def observe(
        self,
        handler: Callable[..., Any],
        names: Sentinel | str | Collection[Sentinel | str] = All,
        type: Sentinel | str = "change",
    ) -> None:
        if getattr(self, "mutate_during_state_observe", False) and isinstance(
            getattr(handler, "__self__", None), StateContext
        ):
            self.mutate_during_state_observe = False
            self.value = 7
        super().observe(handler, names=names, type=type)


class BrokenObservationWidget:
    def trait_names(self) -> list[str]:
        raise RuntimeError("trait discovery failed")

    def observe(self, *_args: object, **_kwargs: object) -> None:
        pass

    def unobserve(self, *_args: object, **_kwargs: object) -> None:
        pass


class UnobserveProbe:
    def __init__(self, *, fail: bool = False) -> None:
        self.value = 1
        self.fail = fail
        self.attempts = 0
        self.observers: list[tuple[Callable[..., Any], tuple[str, ...]]] = []

    def trait_names(self) -> list[str]:
        return ["value"]

    def observe(
        self,
        handler: Callable[..., Any],
        *,
        names: Sequence[str],
    ) -> None:
        self.observers.append((handler, tuple(names)))

    def unobserve(
        self,
        handler: Callable[..., Any],
        *,
        names: Sequence[str],
    ) -> None:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("unobserve failed")
        self.observers.remove((handler, tuple(names)))


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
    assert update.state == {
        "doubled": 2,
        "label": "ready",
        "payload": {"type": "binary", "bytes": 3},
        "ratio": 1.0,
        "value": 1,
        "values": [1, 2],
    }


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


def test_selected_projection_seeds_values_after_observer_registration() -> None:
    widget = RegistrationMutationWidget()
    context = StateContext(widget, [widget], ("value", "doubled"))

    try:
        initial = context.take()
    finally:
        context.close()
        widget.close()

    assert initial is not None
    assert initial.state == {"doubled": 14, "value": 7}


def test_callable_projection_computes_after_observer_registration_change() -> None:
    widget = RegistrationMutationWidget()
    context = StateContext(
        widget,
        [widget],
        lambda current: {"doubled": current.doubled, "value": current.value},
    )

    try:
        initial = context.take()
    finally:
        context.close()
        widget.close()

    assert initial is not None
    assert initial.state == {"doubled": 14, "value": 7}


def test_callable_projection_rolls_back_partial_observer_registration() -> None:
    widget = ObservationWidget()

    try:
        with pytest.raises(RuntimeError, match="trait discovery failed"):
            StateContext(
                widget,
                [widget, BrokenObservationWidget()],
                lambda current: {"value": current.value},
            )
        assert widget.state_observers == {}
    finally:
        widget.close()


def test_callable_projection_summarizes_recursive_and_oversized_values() -> None:
    widget = StateWidget()
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    view = memoryview(bytearray(12)).cast("B", shape=(3, 4))

    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            "recursive": recursive,
            "binary": [b"abc"],
            "memoryview": view,
            "ratio": float("inf"),
            "large": "x" * 20_000,
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
        "truncated": True,
        "preview": "x" * len(update.state["large"]["preview"]),
    }
    assert update.state["memoryview"] == {"type": "binary", "bytes": 12}
    assert update.state["ratio"] == {"type": "float", "value": "inf"}


@pytest.mark.parametrize("collection", [CountingMapping, CountingSequence])
def test_callable_projection_bounds_collection_reads(collection: Any) -> None:
    widget = StateWidget()
    values = collection(10_000)
    context = StateContext(widget, [widget], lambda _widget: {"values": values})

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert 0 < values.reads < values.length
    assert len(json.dumps(update.state, separators=(",", ":")).encode()) <= 8_000
    projected = update.state["values"]
    if isinstance(values, Mapping):
        summary = projected["_summary"]
        assert summary["entries"] == 10_000
        assert summary["omitted"] == 10_000 - (len(projected) - 1)
        assert projected["0"] == 0
    else:
        assert projected["length"] == 10_000
        assert projected["omitted"] == 10_000 - len(projected["items"])
        assert projected["items"][:3] == [0, 1, 2]


def test_projection_summarizes_integers_outside_json_safe_range() -> None:
    widget = StateWidget()
    context = StateContext(
        widget,
        [widget],
        lambda _widget: {
            "max_safe": (1 << 53) - 1,
            "positive": 1 << 53,
            "negative": -(1 << 53),
            "large": 10**10_000,
        },
    )

    try:
        update = context.take()
    finally:
        context.close()
        widget.close()

    assert update is not None
    assert update.state == {
        "large": {"type": "integer", "bits": 33_220, "sign": "positive"},
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
    assert 0 < projection.reads < projection.length
    summaries = [
        value
        for value in update.state.values()
        if isinstance(value, dict) and value.get("type") == "mapping"
    ]
    assert summaries == [
        {
            "type": "mapping",
            "entries": 10_000,
            "omitted": 10_000 - (len(update.state) - 1),
        }
    ]


def test_projection_has_one_aggregate_traversal_budget() -> None:
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
    assert reads[0] + tail.reads <= 8_000
    assert len(json.dumps(update.state, separators=(",", ":")).encode()) <= 8_000


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


def test_remove_widgets_attempts_every_observer_and_retries_failures() -> None:
    root = StateWidget()
    failing = UnobserveProbe(fail=True)
    healthy = UnobserveProbe()
    context = StateContext(root, [root, failing, healthy], lambda _widget: {})

    try:
        with pytest.raises(
            ExceptionGroup,
            match="Failed to remove state observers",
        ) as error_info:
            context.remove_widgets([failing, healthy])

        assert [str(error) for error in error_info.value.exceptions] == [
            "unobserve failed"
        ]
        assert failing.attempts == 1
        assert healthy.attempts == 1
        assert healthy.observers == []

        failing.fail = False
        context.remove_widgets([failing])
        assert failing.attempts == 2
        assert failing.observers == []
    finally:
        context.close()
        root.close()


def test_close_attempts_every_observer_before_raising_cleanup_errors() -> None:
    root = StateWidget()
    failing = UnobserveProbe(fail=True)
    healthy = UnobserveProbe()
    context = StateContext(root, [root, failing, healthy], lambda _widget: {})

    try:
        with pytest.raises(
            ExceptionGroup,
            match="Failed to close state observers",
        ) as error_info:
            context.close()

        assert [str(error) for error in error_info.value.exceptions] == [
            "unobserve failed"
        ]
        assert failing.attempts == 1
        assert healthy.attempts == 1
        assert healthy.observers == []

        failing.fail = False
        context.close()
        assert failing.attempts == 2
        assert failing.observers == []
    finally:
        root.close()


def test_state_projection_recomputes_only_for_watched_root_traits() -> None:
    widget = ObservationWidget()
    child = ObservationWidget()
    calls = 0

    def project(current: StateWidget) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return {"label": current.label, "value": current.value}

    context = StateContext(
        widget,
        [widget, child],
        StateProjection(project, watch="value"),
    )

    try:
        initial = context.take()
        widget.label = "changed outside watch"
        unrelated = context.take()
        widget.value = 4
        changed = context.take()
    finally:
        context.close()
        widget.close()
        child.close()

    assert initial is not None
    assert initial.state == {"label": "ready", "value": 1}
    assert unrelated is None
    assert changed is not None
    assert changed.state == {"label": "changed outside watch", "value": 4}
    assert calls == 2


def test_state_projection_empty_watch_computes_once() -> None:
    widget = ObservationWidget()
    calls = 0

    def project(current: StateWidget) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return {"value": current.value}

    context = StateContext(widget, [widget], StateProjection(project, watch=()))

    try:
        initial = context.take()
        widget.value = 9
        later = context.take()
    finally:
        context.close()
        widget.close()

    assert initial is not None and initial.state == {"value": 1}
    assert later is None
    assert calls == 1


def test_state_projection_rejects_mutation_of_an_unwatched_child() -> None:
    widget = StateWidget()
    child = StateWidget()

    def project(current: StateWidget) -> Mapping[str, object]:
        child.label = "mutated"
        return {"value": current.value}

    context = StateContext(
        widget,
        [widget, child],
        StateProjection(project, watch="value"),
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="must not mutate synchronized widget traits",
        ):
            context.take()
    finally:
        context.close()
        widget.close()
        child.close()


def test_partial_observer_install_is_retained_for_session_cleanup() -> None:
    class PartialObserveWidget(StateWidget):
        def __init__(self) -> None:
            self.installed = 0
            self.cleanup_attempts = 0
            super().__init__()

        def observe(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            super().observe(*args, **kwargs)
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.installed += 1
                raise RuntimeError("observer install failed")

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.cleanup_attempts += 1
                if self.cleanup_attempts < 3:
                    raise RuntimeError("observer cleanup failed")
            super().unobserve(*args, **kwargs)
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.installed -= 1

    widget = PartialObserveWidget()
    with pytest.raises(WidgetSessionInitializationError) as error_info:
        WidgetSession("instance", widget, ("value",))

    assert widget.installed == 1
    error_info.value.session.close()
    assert widget.cleanup_attempts == 3
    assert widget.installed == 0


def test_partial_graph_observer_install_is_retained_for_session_cleanup() -> None:
    class PartialGraphObserveWidget(StateWidget):
        def __init__(self) -> None:
            self.installed = 0
            self.cleanup_attempts = 0
            super().__init__()

        def observe(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            super().observe(*args, **kwargs)
            if getattr(handler, "__name__", None) == "_sync_widget_graph":
                self.installed += 1
                raise RuntimeError("graph observer install failed")

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if getattr(handler, "__name__", None) == "_sync_widget_graph":
                self.cleanup_attempts += 1
                if self.cleanup_attempts < 3:
                    raise RuntimeError("graph observer cleanup failed")
            super().unobserve(*args, **kwargs)
            if getattr(handler, "__name__", None) == "_sync_widget_graph":
                self.installed -= 1

    widget = PartialGraphObserveWidget()
    with pytest.raises(WidgetSessionInitializationError) as error_info:
        WidgetSession("instance", widget, None)

    assert widget.installed == 1
    error_info.value.session.close()
    assert widget.cleanup_attempts == 3
    assert widget.installed == 0


def test_partial_graph_discovery_closes_widgets_collected_before_failure() -> None:
    class TrackedChild(StateWidget):
        close_count = 0

        def close(self) -> None:
            self.close_count += 1
            super().close()

    class BrokenProtocolChild:
        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self) -> None:
            self.reads = 0

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("nested state failed")
            return {"value": 1}

    valid = TrackedChild()
    root = NestedParentWidget(payload=[BrokenProtocolChild(), valid])

    with pytest.raises(RuntimeError, match="nested state failed"):
        WidgetSession("instance", root)

    assert valid.close_count == 1
    assert valid.comm is None


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
    assert len(encoded) <= 8_000
    summaries = [
        value
        for value in update.state.values()
        if isinstance(value, dict) and value.get("type") == "mapping"
    ]
    assert summaries and summaries[0]["omitted"] > 0


def test_state_projection_preserves_complete_values_within_its_byte_budget() -> None:
    projected = {
        "rows": list(range(200)),
        "text": "x" * 4_000,
        "k" * 1_000: "value",
        "entries": {str(index): index for index in range(200)},
    }
    widget = StateWidget()
    session = WidgetSession(
        "instance", widget, StateProjection(lambda _widget: projected)
    )
    try:
        update = session.take_projection()
        assert update is not None
        assert update.state == projected
    finally:
        session.close()


def test_state_projection_preserves_nested_json_values() -> None:
    projected: Any = "leaf"
    for _ in range(20):
        projected = cast(Any, {"child": [projected]})
    widget = StateWidget()
    session = WidgetSession(
        "instance", widget, StateProjection(lambda _widget: projected)
    )
    try:
        update = session.take_projection()
        assert update is not None
        assert update.state == projected
    finally:
        session.close()


@pytest.mark.parametrize("max_bytes", [500, 16_000, None])
def test_state_projection_budget_changes_context_and_preserves_widget_data(
    max_bytes: int | None,
) -> None:
    widget = StateWidget(label="x" * 12_000)
    session = WidgetSession(
        "instance",
        widget,
        StateProjection(lambda current: {"label": current.label}, max_bytes=max_bytes),
    )
    try:
        update = session.take_projection()
        assert update is not None
        if max_bytes == 500:
            assert update.state == {
                "label": {
                    "type": "string",
                    "characters": 12_000,
                    "truncated": True,
                    "preview": "x" * 424,
                }
            }
        else:
            assert update.state == {"label": "x" * 12_000}
        assert widget.label == "x" * 12_000
    finally:
        session.close()


@pytest.mark.parametrize("max_bytes", [2, 20, 80, 200, 800, 8_000])
def test_state_projection_includes_summaries_in_the_encoded_byte_budget(
    max_bytes: int,
) -> None:
    widget = StateWidget()
    session = WidgetSession(
        "instance",
        widget,
        StateProjection(
            lambda _widget: {f"é{index}\n": ["💡" * 100] * 10 for index in range(100)},
            max_bytes=max_bytes,
        ),
    )
    try:
        update = session.take_projection()
        assert update is not None
        encoded = json.dumps(
            update.state, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        assert len(encoded) <= max_bytes
        assert json.loads(encoded) == update.state
    finally:
        session.close()


def test_state_projection_preserves_a_value_that_exactly_fits_its_budget() -> None:
    projected = {"é\n": "💡" * 100, "items": list(range(200))}
    max_bytes = len(
        json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    widget = StateWidget()
    session = WidgetSession(
        "instance",
        widget,
        StateProjection(lambda _widget: projected, max_bytes=max_bytes),
    )
    try:
        update = session.take_projection()
        assert update is not None
        assert update.state == projected
    finally:
        session.close()


@pytest.mark.parametrize("max_bytes", [1, True, 1.5, "8000"])
def test_state_projection_requires_a_byte_budget_that_fits_a_json_mapping(
    max_bytes: Any,
) -> None:
    with pytest.raises(ValueError, match="integer of at least 2 or None"):
        StateProjection(lambda _widget: {}, max_bytes=max_bytes)


@pytest.mark.parametrize("duplicate_keys", [False, True])
def test_state_projection_bounds_unknown_length_mapping_iteration(
    duplicate_keys: bool,
) -> None:
    class InfiniteMapping(Mapping[str, int]):
        reads = 0

        def __iter__(self) -> Iterator[str]:
            while True:
                self.reads += 1
                assert self.reads <= 500
                yield "duplicate" if duplicate_keys else str(self.reads)

        def __getitem__(self, key: str) -> int:
            return 1

        def __len__(self) -> int:
            raise TypeError("length is unavailable")

    projected = InfiniteMapping()
    widget = StateWidget()
    session = WidgetSession(
        "instance", widget, StateProjection(lambda _widget: projected, max_bytes=500)
    )
    try:
        update = session.take_projection()
        assert update is not None
        assert 0 < projected.reads <= 500
        assert update.state["_summary"]["omitted"] > 0
        assert len(json.dumps(update.state, separators=(",", ":")).encode()) <= 500
    finally:
        session.close()


def test_state_projection_reports_the_python_recursion_boundary() -> None:
    import sys

    projected: Any = "leaf"
    for _ in range(sys.getrecursionlimit()):
        projected = cast(Any, {"child": projected})
    widget = StateWidget()
    session = WidgetSession(
        "instance", widget, StateProjection(lambda _widget: projected, max_bytes=None)
    )
    try:
        update = session.take_projection()
        assert update is not None
        assert update.state == {"type": "projection", "recursionLimited": True}
    finally:
        session.close()


@pytest.mark.parametrize("character", ["x", "💡", "\n", '"'])
def test_state_projection_string_preview_uses_the_available_encoded_bytes(
    character: str,
) -> None:
    widget = StateWidget(label=character * 20_000)
    session = WidgetSession(
        "instance",
        widget,
        StateProjection(lambda current: {"label": current.label}),
    )
    try:
        update = session.take_projection()
        assert update is not None
        summary = update.state["label"]
        preview = summary["preview"]
        assert summary == {
            "type": "string",
            "characters": 20_000,
            "truncated": True,
            "preview": character * len(preview),
        }
        assert preview and widget.label.startswith(preview)
        encoded = json.dumps(
            update.state, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        assert len(encoded) <= 8_000
        extended = {"label": {**summary, "preview": preview + character}}
        assert (
            len(
                json.dumps(extended, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            > 8_000
        )
        assert json.loads(encoded)["label"]["preview"] == preview
    finally:
        session.close()
