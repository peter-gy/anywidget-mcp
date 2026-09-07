from __future__ import annotations

from collections.abc import Sequence
import gc
from pathlib import Path
import sys
import types
import weakref

import anywidget
import pytest

from anywidget_mcp import AnyWidgetMCP, create_anywidget

from ._server_support import bootstrap_runtime, connected, read_blob, state_id

ROOT = Path(__file__).parents[3]
RETRY_BUDGET_CODE = (ROOT / "examples" / "retry_budget.py").read_text()
MULTI_WIDGET_CODE = (
    RETRY_BUDGET_CODE
    + '''


class RetryGuidance(anywidget.AnyWidget):
    _esm = """
    function render({ model, el }) {
      el.textContent = model.get("recommendation");
    }
    export default { render };
    """

    recommendation = traitlets.Unicode(
        "Keep the total delay below the request deadline."
    ).tag(sync=True)
'''
)


def test_create_anywidget_resolves_classnames_in_the_requested_order() -> None:
    code = """
import anywidget
import traitlets

class FirstWidget(anywidget.AnyWidget):
    label = traitlets.Unicode("first").tag(sync=True)

class UnselectedWidget(anywidget.AnyWidget):
    def __init__(self):
        raise RuntimeError("must not be constructed")

class SecondWidget(anywidget.AnyWidget):
    label = traitlets.Unicode("second").tag(sync=True)
"""

    created = create_anywidget(code, classnames=["SecondWidget", "FirstWidget"])
    assert isinstance(created, Sequence)
    try:
        assert [getattr(widget, "label") for widget in created] == [
            "second",
            "first",
        ]
    finally:
        for widget in reversed(created):
            assert isinstance(widget, anywidget.AnyWidget)
            widget.close()


def test_create_anywidget_uses_the_last_qualifying_binding_as_fallback() -> None:
    code = """
import anywidget
import traitlets

class FirstWidget(anywidget.AnyWidget):
    label = traitlets.Unicode("first").tag(sync=True)

class SecondWidget(anywidget.AnyWidget):
    label = traitlets.Unicode("second").tag(sync=True)
"""

    created = create_anywidget(code)
    assert isinstance(created, anywidget.AnyWidget)
    try:
        assert getattr(created, "label") == "second"
    finally:
        created.close()


def test_create_anywidget_requires_every_requested_classname() -> None:
    with pytest.raises(
        ValueError,
        match=r"classnames\[1\] 'MissingWidget' was not found",
    ):
        create_anywidget(
            "import anywidget\nclass PresentWidget(anywidget.AnyWidget): pass",
            classnames=["PresentWidget", "MissingWidget"],
        )


def test_create_anywidget_requires_requested_anywidget_classes() -> None:
    with pytest.raises(
        TypeError,
        match=r"classnames\[0\] 'PlainPythonClass' must bind an AnyWidget subclass",
    ):
        create_anywidget(
            "class PlainPythonClass: pass",
            classnames=["PlainPythonClass"],
        )


def test_create_anywidget_requires_a_generated_widget_class() -> None:
    with pytest.raises(
        ValueError,
        match="code must define at least one top-level AnyWidget subclass when classnames is omitted",
    ):
        create_anywidget("class PlainPythonClass: pass")


def test_create_anywidget_constructs_widgets_without_arguments() -> None:
    code = """
import anywidget

class NeedsInput(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __init__(self, value):
        super().__init__()
        self.value = value
"""

    with pytest.raises(TypeError, match="required positional argument: 'value'"):
        create_anywidget(code)


def test_create_anywidget_rejects_a_class_that_constructs_a_non_widget() -> None:
    code = """
import anywidget

class RogueWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __new__(cls):
        return object()
"""

    with pytest.raises(
        TypeError,
        match="Generated class RogueWidget constructed object, expected AnyWidget",
    ):
        create_anywidget(code)


def test_create_anywidget_releases_the_generated_module_with_its_widget() -> None:
    widget = create_anywidget(
        """
import anywidget

class TemporaryWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"
"""
    )
    assert isinstance(widget, anywidget.AnyWidget)
    module_name = type(widget).__module__
    widget_ref = weakref.ref(widget)

    widget.close()
    del widget
    gc.collect()

    assert widget_ref() is None
    assert module_name not in sys.modules


def test_create_anywidget_closes_prior_widgets_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    hooks = types.ModuleType("generated_widget_test_hooks")
    setattr(hooks, "events", events)
    monkeypatch.setitem(sys.modules, hooks.__name__, hooks)
    code = """
import anywidget
from generated_widget_test_hooks import events

class FirstWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def close(self):
        if self.comm is not None:
            events.append("first closed")
        super().close()

class BrokenWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __init__(self):
        raise RuntimeError("construction failed")
"""

    with pytest.raises(RuntimeError, match="construction failed"):
        create_anywidget(code, classnames=["FirstWidget", "BrokenWidget"])

    assert events == ["first closed"]


@pytest.mark.anyio
async def test_create_anywidget_exposes_schema_and_generated_assets() -> None:
    server = AnyWidgetMCP("test")
    server.widget(create_anywidget)

    async with connected(server) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "create_anywidget"
        )
        launch = await client.call_tool(
            "create_anywidget",
            {"code": RETRY_BUDGET_CODE, "classnames": ["RetryBudget"]},
        )
        runtime = await bootstrap_runtime(client, launch)
        model = runtime["models"][runtime["rootModelId"]]
        sources = {
            name: await read_blob(client, runtime["instanceId"], ref)
            for name, ref in model["sourceRefs"].items()
        }
        await client.call_tool(
            "anywidget_dispose",
            {"session_id": runtime["instanceId"]},
        )

    assert tool.input_schema["required"] == ["code"]
    assert tool.input_schema["properties"]["code"] == {
        "title": "Code",
        "type": "string",
    }
    assert tool.input_schema["properties"]["classnames"] == {
        "default": [],
        "items": {"type": "string"},
        "title": "Classnames",
        "type": "array",
    }
    assert launch.structured_content == {
        "tool": "create_anywidget",
        "state": {"attempts": 4, "base_delay": 0.5, "total_wait": 3.5},
        "state_id": state_id(launch),
    }
    assert set(model["sourceRefs"]) == {"_css", "_esm"}
    assert set(model["state"]).isdisjoint(model["sourceRefs"])
    assert set(sources) == {"_esm", "_css"}
    assert all(source.decode("utf-8") for source in sources.values())


@pytest.mark.anyio
async def test_create_anywidget_launches_multiple_selected_classes() -> None:
    server = AnyWidgetMCP("test")
    server.widget(create_anywidget)

    async with connected(server) as client:
        launch = await client.call_tool(
            "create_anywidget",
            {
                "code": MULTI_WIDGET_CODE,
                "classnames": ["RetryBudget", "RetryGuidance"],
            },
        )
        runtime = await bootstrap_runtime(client, launch)
        await client.call_tool(
            "anywidget_dispose",
            {"session_id": runtime["instanceId"]},
        )

    assert launch.structured_content == {
        "tool": "create_anywidget",
        "state_id": state_id(launch),
        "state": {
            "widgets": [
                {"attempts": 4, "base_delay": 0.5, "total_wait": 3.5},
                {"recommendation": "Keep the total delay below the request deadline."},
            ]
        },
    }
    assert len(runtime["models"]) == 3
