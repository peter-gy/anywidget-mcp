from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from typing import Any, Annotated, cast

import pytest
from mcp.server.mcpserver import Context, MCPServer, Resolve
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    SecretStr,
    field_serializer,
    model_serializer,
)

from anywidget_mcp.server import AnyWidgetMCP, attach

from ._server_support import CounterWidget, bootstrap_runtime, connected, state_id


class Query(BaseModel):
    day: date
    limits: list[int]


@pytest.mark.anyio
async def test_saved_creation_reacquires_managed_resources_in_a_new_server() -> None:
    events: list[str] = []
    contexts: list[Context] = []

    @contextmanager
    def explorer(
        query: Query, ctx: Context, scale: int = 2
    ) -> Generator[CounterWidget, None, None]:
        contexts.append(ctx)
        events.append("open")
        assert query.day == date(2026, 9, 12)
        widget = CounterWidget(value=query.limits[0] * scale)
        query.limits.clear()
        try:
            yield widget
        finally:
            assert widget.comm is None
            events.append("close")

    first = AnyWidgetMCP("first")
    first.widget(explorer, reopen="manual")
    async with connected(first) as client:
        result = await client.call_tool(
            "explorer", {"query": {"day": "2026-09-12", "limits": ["3"]}}
        )
        assert not result.is_error
        assert result.meta is not None
        descriptor = result.meta["anywidget"]["reopen"]
        assert descriptor == {
            "version": 1,
            "tool": "explorer",
            "mode": "manual",
            "ui": True,
            "arguments": {"query": {"day": "2026-09-12", "limits": [3]}, "scale": 2},
        }
        original = await bootstrap_runtime(client, result)
    assert events == ["open", "close"]

    second = MCPServer("second")
    attach(second).widget(explorer, reopen="manual")
    async with connected(second) as client:
        missing = await client.call_tool(
            "anywidget_state", {"state_id": state_id(result)}
        )
        assert missing.is_error
        recreated = await client.call_tool(descriptor["tool"], descriptor["arguments"])
        current = await bootstrap_runtime(client, recreated)
        assert original["instanceId"] != current["instanceId"]
        assert original["rootModelId"] != current["rootModelId"]
        assert state_id(result) != state_id(recreated)
        assert current["context"]["state"]["value"] == 6
    assert events == ["open", "close", "open", "close"]
    assert contexts[0] is not contexts[1]


@pytest.mark.anyio
async def test_reopen_preserves_input_aliases_and_a_declared_loading_message() -> None:
    server = AnyWidgetMCP("test")

    @server.widget(reopen="auto")
    def counter(
        value: Annotated[int, Field(alias="initial")] = 3,
        loading_message: str = "Opening counter",
    ) -> CounterWidget:
        return CounterWidget(value=value)

    async with connected(server) as client:
        result = await client.call_tool(
            "counter", {"initial": 7, "loading_message": "Preparing counter"}
        )
        assert not result.is_error
        assert result.meta is not None
        arguments = result.meta["anywidget"]["reopen"]["arguments"]
        assert arguments == {"initial": 7, "loading_message": "Preparing counter"}
        reopened = await client.call_tool("counter", arguments)
        assert reopened.structured_content is not None
        assert reopened.structured_content["state"]["value"] == 7


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(object(), id="unsupported-json"),
        pytest.param("λ" * 33000, id="utf8-byte-limit"),
        pytest.param(float("nan"), id="non-finite"),
        pytest.param(Secret("sentinel-must-not-leak"), id="generic-secret"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
async def test_invalid_reopen_inputs_fail_before_factory_acquisition(
    value: Any,
) -> None:
    server = AnyWidgetMCP("test")
    invoked = False

    @server.widget(reopen="manual")
    def counter(input: Any = Field(default_factory=lambda: value)) -> CounterWidget:
        nonlocal invoked
        invoked = True
        return CounterWidget()

    async with connected(server) as client:
        result = await client.call_tool("counter", {})
    assert result.is_error
    text = " ".join(item.text for item in result.content if item.type == "text")
    assert "counter" in text
    assert "identifier" in text
    if isinstance(value, Secret):
        assert value.get_secret_value() not in text
    assert not invoked


@pytest.mark.anyio
async def test_reopen_metadata_is_opt_in() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)
    async with connected(server) as client:
        result = await client.call_tool("counter_widget", {})
        assert result.meta is not None
        assert "reopen" not in result.meta.get("anywidget", {})


def test_reopen_requires_valid_configuration() -> None:
    with pytest.raises(ValueError, match="reopen"):
        AnyWidgetMCP("test").widget(CounterWidget, reopen=cast(Any, True))
    with pytest.raises(TypeError, match="reopen_ui"):
        AnyWidgetMCP("test").widget(
            CounterWidget, reopen="auto", reopen_ui=cast(Any, "yes")
        )


@pytest.mark.anyio
async def test_reopening_resolves_dependencies_again_and_revalidates_saved_inputs() -> (
    None
):
    calls: list[int] = []

    def acquire_scale() -> int:
        return len(calls) + 2

    server = AnyWidgetMCP("first")

    @server.widget(reopen="auto")
    def explorer(
        value: int, scale: Annotated[int, Resolve(acquire_scale)]
    ) -> CounterWidget:
        calls.append(scale)
        return CounterWidget(value=value * scale)

    async with connected(server) as client:
        original = await client.call_tool("explorer", {"value": 3})
        assert original.meta is not None
        descriptor = original.meta["anywidget"]["reopen"]
        assert descriptor["arguments"] == {"value": 3}
        recreated = await client.call_tool("explorer", descriptor["arguments"])
        assert recreated.structured_content is not None
        assert recreated.structured_content["state"]["value"] == 9
    assert calls == [2, 3]

    replacement = AnyWidgetMCP("replacement")

    @replacement.widget(name="explorer", reopen="auto")
    def changed_explorer(value: Annotated[int, Field(ge=5)]) -> CounterWidget:
        pytest.fail("Invalid saved inputs reached the factory")

    async with connected(replacement) as client:
        rejected = await client.call_tool("explorer", descriptor["arguments"])
        assert rejected.is_error


@pytest.mark.anyio
async def test_reopen_rejects_secrets_before_custom_serializers_can_reveal_them() -> (
    None
):
    serialized: list[str] = []

    class Credentials(BaseModel):
        token: SecretStr

        @field_serializer("token")
        def reveal(self, value: SecretStr) -> str:
            serialized.append("called")
            return value.get_secret_value()

    server = AnyWidgetMCP("test")

    @server.widget(reopen="manual")
    def explorer(credentials: Credentials) -> CounterWidget:
        pytest.fail("Secret inputs reached the factory")

    async with connected(server) as client:
        result = await client.call_tool(
            "explorer", {"credentials": {"token": "sentinel-must-not-leak"}}
        )
    assert result.is_error
    text = " ".join(item.text for item in result.content if item.type == "text")
    assert "credentials" in text
    assert "contains a secret value" in text
    assert "sentinel-must-not-leak" not in text
    assert serialized == []


@pytest.mark.anyio
async def test_reopen_rejects_secrets_in_extra_model_fields() -> None:
    serialized: list[str] = []

    class Credentials(BaseModel):
        model_config = ConfigDict(extra="allow")

        @model_serializer
        def reveal(self) -> dict[str, str]:
            serialized.append("called")
            return {"token": "sentinel-must-not-leak"}

    credentials = Credentials.model_validate(
        {"token": SecretStr("sentinel-must-not-leak")}
    )
    server = AnyWidgetMCP("test")

    @server.widget(reopen="manual")
    def explorer(
        settings: Credentials = Field(default_factory=lambda: credentials),
    ) -> CounterWidget:
        pytest.fail("Secret inputs reached the factory")

    async with connected(server) as client:
        result = await client.call_tool("explorer", {})
    assert result.is_error
    text = " ".join(item.text for item in result.content if item.type == "text")
    assert "settings" in text
    assert "contains a secret value" in text
    assert "sentinel-must-not-leak" not in text
    assert serialized == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode, controls, expected",
    [
        ("auto", None, False),
        ("manual", None, True),
        ("auto", True, True),
        ("manual", False, False),
    ],
)
async def test_reopen_controls_are_independent_of_creation_mode(
    mode: Any, controls: bool | None, expected: bool
) -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget, reopen=mode, reopen_ui=controls)
    async with connected(server) as client:
        result = await client.call_tool("counter_widget", {})
    assert result.meta is not None
    assert result.meta["anywidget"]["reopen"]["ui"] is expected
