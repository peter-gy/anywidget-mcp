# Share state with the model

The `state` option controls which widget values the model can read. For a color
picker, `state="color"` exposes the current color. Choose a color in the widget,
then ask the model which color you chose. The exposed value is the widget's
model-visible state.

The examples use `ColorPicker` and `SortableList` from
[Wigglystuff](https://koaning.github.io/wigglystuff/). The same state API
applies to other [AnyWidgets](https://anywidget.dev/).

## Choose what the model can read

Register one trait by name:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")
mcp.widget(ColorPicker, state="color")
```

The `state` option accepts these forms:

| Value                                     | What the model receives                                                    |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| Omitted                                   | Public synchronized root traits except `layout`, `tabbable`, and `tooltip` |
| `"color"`                                 | One selected root trait                                                    |
| `("color", "show_label")`                 | Selected root traits                                                       |
| `None`                                    | Model-visible state is disabled                                            |
| `lambda widget: {...}`                    | A custom mapping updated by changes in the enrolled widget graph           |
| `StateProjection(project, watch="value")` | A custom mapping updated by selected root traits                           |

Unknown trait names raise `ValueError` when the first widget session opens.

## Define derived state

For derived state, provide a projection function. A projection turns the
current widget into the mapping sent to the model:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import SortableList

mcp = AnyWidgetMCP("List tools")
mcp.widget(
    SortableList,
    state=lambda widget: {
        "items": widget.value,
        "count": len(widget.value),
    },
)
```

The function receives the root widget. Changes in the enrolled widget graph
cause a plain projection function to run again.

Projection callables are read-only. Mutating a synchronized widget trait while
building a projection returns a state-projection error to the app.

## Choose when derived state updates

`StateProjection` separates the mapping from the traits that can change it:

```python
from anywidget_mcp import AnyWidgetMCP, StateProjection
from wigglystuff import SortableList


def list_summary(widget: SortableList) -> dict[str, object]:
    return {
        "items": widget.value,
        "count": len(widget.value),
    }


mcp = AnyWidgetMCP("List tools")
mcp.widget(
    SortableList,
    state=StateProjection(list_summary, watch="value"),
)
```

| `watch` value         | Recomputes when                                         |
| --------------------- | ------------------------------------------------------- |
| `None`                | Any observed trait in the enrolled widget graph changes |
| A string              | That root trait changes                                 |
| A sequence of strings | Any selected root trait changes                         |
| `()`                  | Never after the initial value                           |

Every `StateProjection` computes an initial value. A named `watch` keeps
recomputation tied to root traits even when the projection reads child widgets.

## Projection output

Projection mappings use one byte budget for the complete compact UTF-8 JSON
value, defaulting to 8,000 bytes. Collections, strings, keys, and nested values
that fit keep their full contents. A sequence of widgets shares that budget.
Oversized values become deterministic summaries with previews when space permits.
Binary values become records such as `{"type": "binary", "bytes": 2048}`.
Cycles and values that exceed Python's recursion capacity become summaries.

Set the budget on `StateProjection` when the model needs a larger selection:

```python
mcp.widget(
    SortableList,
    state=StateProjection(list_summary, watch="value", max_bytes=32000),
)
```

Use `max_bytes=None` for trusted finite projections that must be preserved in
full. The host's message capacity and the model's context window still apply.
Keep rendering data in widget traits or query results when the model needs just
a selection or summary.

Structured tool results and model context identify the registered tool through
the `tool` field. Use `name=` during registration when that identity should
differ from the class or factory name.

## How state reaches the model

The initial tool result includes one complete projection in its text content
and structured state. If the MCP SDK cannot encode a deeply nested structured
value, the text retains the complete JSON and the structured result retains its
identity fields. After browser interaction reaches Python, the app publishes the
latest complete projection through [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
`updateModelContext` when the host supports that method.

Each launch with model-visible state also returns a `state_id`. The
model-visible `anywidget_state` tool accepts that ID and returns the latest
complete projection. This gives hosts that do not implement
`updateModelContext` a pull path when the model needs the current widget state.
Reading state does not consume a projection waiting for the browser app. The
state handle expires with its widget session and is revoked on disposal.
