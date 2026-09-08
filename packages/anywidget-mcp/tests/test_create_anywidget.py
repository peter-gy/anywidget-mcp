from __future__ import annotations

from collections.abc import Sequence
import gc
from pathlib import Path
import sys
import types
import weakref
from typing import get_type_hints

import anywidget
from mcp.types import TextContent
import pytest

from anywidget_mcp import WidgetCreationError, create_anywidget
from anywidget_mcp.server import AnyWidgetMCP

from ._server_support import bootstrap_runtime, connected, read_blob, state_id

ROOT = Path(__file__).parents[3]
RETRY_BUDGET_CODE = (ROOT / "examples" / "retry_budget.py").read_text()


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
        WidgetCreationError,
        match=r"classnames\[1\] 'MissingWidget' was not found",
    ):
        create_anywidget(
            "import anywidget\nclass PresentWidget(anywidget.AnyWidget): pass",
            classnames=["PresentWidget", "MissingWidget"],
        )


def test_create_anywidget_requires_requested_anywidget_classes() -> None:
    with pytest.raises(
        WidgetCreationError,
        match=r"classnames\[0\] 'PlainPythonClass' must bind an AnyWidget subclass",
    ):
        create_anywidget(
            "class PlainPythonClass: pass",
            classnames=["PlainPythonClass"],
        )


def test_create_anywidget_requires_a_generated_widget_class() -> None:
    with pytest.raises(
        WidgetCreationError,
        match="code must define at least one top-level AnyWidget subclass when classnames is omitted",
    ):
        create_anywidget("class PlainPythonClass: pass")


def test_create_anywidget_rejects_a_class_that_constructs_a_non_widget() -> None:
    code = """
import anywidget

class RogueWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __new__(cls):
        return object()
"""

    with pytest.raises(
        WidgetCreationError,
        match="Generated class RogueWidget constructed object, expected AnyWidget",
    ):
        create_anywidget(code)


def test_create_anywidget_releases_the_generated_module_with_its_widget() -> None:
    widget = create_anywidget(
        """
import anywidget

current = None

class TemporaryWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __init__(self):
        global current
        super().__init__()
        current = self
"""
    )
    assert isinstance(widget, anywidget.AnyWidget)
    module_name = type(widget).__module__
    widget_ref = weakref.ref(widget)

    widget.close()
    assert module_name not in sys.modules
    del widget
    gc.collect()

    assert widget_ref() is None


def test_generated_module_lives_until_last_widget_closes() -> None:
    widgets = create_anywidget(
        """
import anywidget

class Details:
    value = 7

class First(anywidget.AnyWidget): pass
class Second(anywidget.AnyWidget):
    details: "Details"
""",
        classnames=["First", "Second"],
    )
    assert isinstance(widgets, Sequence)
    first, second = widgets
    module_name = type(first).__module__

    try:
        first.close()
        first.close()
        assert get_type_hints(type(second))["details"]().value == 7
        assert second.comm is not None
        second.close()
        assert module_name not in sys.modules
    finally:
        first.close()
        second.close()


def test_generated_widget_keeps_its_module_when_close_requires_a_retry() -> None:
    widget = create_anywidget(
        """
import anywidget

class RetryClose(anywidget.AnyWidget):
    attempts = 0

    def close(self):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("retry close")
        super().close()
"""
    )
    assert isinstance(widget, anywidget.AnyWidget)
    module_name = type(widget).__module__

    try:
        with pytest.raises(RuntimeError, match="retry close"):
            widget.close()
        assert module_name in sys.modules
        assert widget.comm is not None
        widget.close()
        assert widget.comm is None
        assert module_name not in sys.modules
    finally:
        widget.close()


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

class ChildWidget(anywidget.AnyWidget):
    def close(self):
        if self.comm is not None:
            events.append("child closed")
        super().close()

class FirstWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"
    child = anywidget.WidgetTrait().tag(sync=True)

    def __init__(self):
        super().__init__(child=ChildWidget())

    def close(self):
        if self.comm is not None:
            events.append("first closed")
        super().close()

class BrokenWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __init__(self):
        raise RuntimeError("construction failed")
"""

    with pytest.raises(WidgetCreationError, match="construction failed"):
        create_anywidget(code, classnames=["FirstWidget", "BrokenWidget"])

    assert events == ["child closed", "first closed"]


def test_generated_error_message_is_bounded() -> None:
    with pytest.raises(WidgetCreationError) as error:
        create_anywidget('raise RuntimeError("x" * 10000)')

    message = str(error.value)
    assert message.startswith("RuntimeError at line 1: xxx")
    assert message.endswith("...")
    assert len(message) < 1_100


def test_generated_source_preserves_process_cancellation() -> None:
    with pytest.raises(KeyboardInterrupt, match="stop"):
        create_anywidget('raise KeyboardInterrupt("stop")')


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "diagnostic"),
    [
        (
            "import anywidget\nclass Invalid(anywidget.AnyWidget)\n    pass\n",
            "SyntaxError at line 2: expected ':'",
        ),
        (
            "import anywidget\n"
            "class NeedsInput(anywidget.AnyWidget):\n"
            "    def __init__(self, value): pass\n",
            "TypeError: NeedsInput.__init__() missing 1 required positional argument: 'value'",
        ),
        (
            "import anywidget\n"
            "class Child(anywidget.AnyWidget):\n"
            "    def __init__(self, value): super().__init__()\n"
            "class Parent(anywidget.AnyWidget):\n"
            "    def __init__(self):\n"
            "        child = Child()\n"
            "        super().__init__()\n",
            "TypeError at line 6: Child.__init__() missing 1 required positional "
            "argument: 'value'",
        ),
    ],
)
async def test_generated_tool_reports_source_errors_and_accepts_corrected_retry(
    code: str, diagnostic: str
) -> None:
    server = AnyWidgetMCP("test")
    server.widget(create_anywidget)

    async with connected(server) as client:
        failed = await client.call_tool("create_anywidget", {"code": code})
        assert failed.is_error is True
        assert isinstance(failed.content[0], TextContent)
        assert diagnostic in failed.content[0].text

        corrected = await client.call_tool(
            "create_anywidget",
            {
                "code": (
                    "import anywidget, traitlets\n"
                    "class Corrected(anywidget.AnyWidget):\n"
                    "    value = traitlets.Int(7).tag(sync=True)\n"
                )
            },
        )
        assert corrected.is_error is False
        assert corrected.structured_content is not None
        assert corrected.structured_content["state"] == {"value": 7}
        runtime = await bootstrap_runtime(client, corrected)
        disposed = await client.call_tool(
            "anywidget_dispose", {"session_id": runtime["instanceId"]}
        )
        assert disposed.is_error is False


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
    assert all(source.decode("utf-8") for source in sources.values())
