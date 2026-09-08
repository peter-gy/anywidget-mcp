from __future__ import annotations

import json
from typing import Any

import anywidget
import pytest
import traitlets

from anywidget_mcp import StateProjection
from anywidget_mcp._bridge import WidgetSession
from anywidget_mcp._group import _WidgetGroup, _group_state
from anywidget_mcp._state import DEFAULT_STATE


class GroupStateWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    selected = traitlets.Int().tag(sync=True)
    detail = traitlets.Unicode().tag(sync=True)


class OtherGroupWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    value = traitlets.Int().tag(sync=True)


def test_group_trait_selection_applies_to_every_widget() -> None:
    first = GroupStateWidget(selected=1, detail="first")
    second = GroupStateWidget(selected=2, detail="second")
    group = _WidgetGroup((first, second))
    session = WidgetSession(
        "instance",
        group,
        _group_state(("selected",), (first, second)),
    )

    try:
        initial = session.take_projection()
        assert initial is not None
        assert initial.state == {"widgets": [{"selected": 1}, {"selected": 2}]}

        first.detail = "ignored"
        assert session.snapshot().projection is None
    finally:
        session.close()


def test_group_trait_selection_identifies_the_invalid_item() -> None:
    first = GroupStateWidget()
    second = OtherGroupWidget()
    group = _WidgetGroup((first, second))

    with pytest.raises(
        ValueError,
        match=(
            r"widget sequence item 1 \(OtherGroupWidget\).*"
            r"Unknown state trait.*selected"
        ),
    ):
        WidgetSession(
            "instance",
            group,
            _group_state(("selected",), (first, second)),
        )

    assert group.comm is None
    assert first.comm is None
    assert second.comm is None


def test_group_callable_projection_runs_for_each_logical_widget() -> None:
    first = GroupStateWidget(selected=1)
    second = GroupStateWidget(selected=2)
    group = _WidgetGroup((first, second))

    def project(widget: GroupStateWidget) -> dict[str, int]:
        return {"answer": widget.selected * 2}

    session = WidgetSession(
        "instance",
        group,
        _group_state(project, (first, second)),
    )

    try:
        initial = session.take_projection()

        assert initial is not None
        assert initial.state == {"widgets": [{"answer": 2}, {"answer": 4}]}
    finally:
        session.close()


def test_group_state_projection_preserves_explicit_watch_semantics() -> None:
    first = GroupStateWidget(selected=1, detail="first")
    second = GroupStateWidget(selected=2, detail="second")
    group = _WidgetGroup((first, second))
    state = StateProjection[GroupStateWidget](
        lambda widget: {
            "selected": widget.selected,
            "detail": widget.detail,
        },
        watch="selected",
    )
    session = WidgetSession(
        "instance",
        group,
        _group_state(state, (first, second)),
    )

    try:
        assert session.take_projection() is not None

        first.detail = "changed"
        assert session.snapshot().projection is None

        second.selected = 9
        changed = session.snapshot()
        assert changed.projection is not None
        assert changed.projection.state == {
            "widgets": [
                {"detail": "changed", "selected": 1},
                {"detail": "second", "selected": 9},
            ]
        }
    finally:
        session.close()


def test_group_callable_requires_a_mapping_from_each_widget() -> None:
    first = GroupStateWidget(selected=1)
    second = GroupStateWidget(selected=2)
    group = _WidgetGroup((first, second))

    def project(widget: GroupStateWidget) -> Any:
        if widget is first:
            return {"selected": widget.selected}
        return widget.selected

    session = WidgetSession(
        "instance",
        group,
        _group_state(project, (first, second)),
    )
    try:
        launch = session.launch_snapshot()

        assert launch.projection is None
        assert launch.projection_error == (
            "The widget state projection for sequence item 1 must return a "
            "mapping, got int"
        )
    finally:
        session.close()


def test_group_state_none_disables_the_aggregate_projection() -> None:
    widget = GroupStateWidget(selected=1)
    group = _WidgetGroup((widget,))
    session = WidgetSession(
        "instance",
        group,
        _group_state(None, (widget,)),
    )

    try:
        assert session.take_projection() is None
        widget.selected = 2
        assert session.snapshot().projection is None
    finally:
        session.close()


def test_group_state_keeps_a_bounded_preview_of_large_compositions() -> None:
    widgets = tuple(
        GroupStateWidget(detail=f"{index}-{'x' * 1000}") for index in range(10)
    )
    group = _WidgetGroup(widgets)
    session = WidgetSession(
        "instance",
        group,
        _group_state(DEFAULT_STATE, widgets),
    )

    try:
        initial = session.take_projection()
        assert initial is not None
        projected = initial.state["widgets"]
        assert len(json.dumps(initial.state, separators=(",", ":")).encode()) <= 8_000
        assert projected[0] == {
            "detail": f"0-{'x' * 1000}",
            "selected": 0,
        }
        assert any(item.get("_summary", {}).get("omitted", 0) > 0 for item in projected)
    finally:
        session.close()


@pytest.mark.parametrize("max_bytes", [8_000, 16_000, None])
def test_group_projection_applies_the_byte_budget_to_the_complete_sequence(
    max_bytes: int | None,
) -> None:
    widgets = tuple(GroupStateWidget(detail="x" * 4_000) for _ in range(3))
    group = _WidgetGroup(widgets)
    session = WidgetSession(
        "instance",
        group,
        _group_state(
            StateProjection(
                lambda widget: {"detail": widget.detail}, max_bytes=max_bytes
            ),
            widgets,
        ),
    )

    try:
        initial = session.take_projection()
        assert initial is not None
        encoded = json.dumps(initial.state, separators=(",", ":")).encode()
        if max_bytes == 8_000:
            assert len(encoded) <= 8_000
            assert initial.state["widgets"] != [{"detail": "x" * 4_000}] * 3
        else:
            assert initial.state == {"widgets": [{"detail": "x" * 4_000}] * 3}
        assert [widget.detail for widget in widgets] == ["x" * 4_000] * 3
    finally:
        session.close()
