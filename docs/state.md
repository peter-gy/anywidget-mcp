# Model-visible state

The `state` option selects the widget data sent to the model. The initial tool
result includes one complete projection. After browser interaction reaches
Python, the app publishes the latest complete projection through
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
`updateModelContext` when the host supports that method.

The examples use `ColorPicker` and `SortableList` from
[Wigglystuff](https://koaning.github.io/wigglystuff/). The same state API
applies to other [AnyWidget](https://anywidget.dev/) classes.

## Select synchronized traits

Register one trait by name:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")
mcp.widget(ColorPicker, state="color")
```

The `state` option accepts these forms:

| Value                                     | Projection                                                                 |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| Omitted                                   | Public synchronized root traits except `layout`, `tabbable`, and `tooltip` |
| `"color"`                                 | One selected root trait                                                    |
| `("color", "show_label")`                 | Selected root traits                                                       |
| `None`                                    | State projection and model-context updates are disabled                    |
| `lambda widget: {...}`                    | A custom mapping invalidated by changes in the enrolled widget graph       |
| `StateProjection(project, watch="value")` | A custom mapping invalidated by selected root traits                       |

Unknown trait names raise `ValueError` when the first widget session opens.

## Define a projected mapping

A projection callable can select and derive fields from a widget model:

```python
from wigglystuff import SortableList

mcp.widget(
    SortableList,
    state=lambda widget: {
        "items": widget.value,
        "count": len(widget.value),
    },
)
```

The callable receives the root widget and returns a mapping. Changes in the
enrolled widget graph invalidate a plain callable.

Projection callables are read-only. Mutating a synchronized widget trait while
building a projection returns a state-projection error to the app.

## Choose invalidation traits

`StateProjection` separates the mapping from the traits that can change it:

```python
from anywidget_mcp import StateProjection
from wigglystuff import SortableList


def list_summary(widget: SortableList) -> dict[str, object]:
    return {
        "items": widget.value,
        "count": len(widget.value),
    }


mcp.widget(
    SortableList,
    state=StateProjection(list_summary, watch="value"),
)
```

| `watch` value         | Invalidation                                    |
| --------------------- | ----------------------------------------------- |
| `None`                | Any observed trait in the enrolled widget graph |
| A string              | That root trait                                 |
| A sequence of strings | Those root traits                               |
| `()`                  | Compute once when the session opens             |

Every `StateProjection` computes an initial value. A named `watch` keeps
recomputation tied to root traits even when the projection reads child widgets.

## Projection output

Projection mappings are converted to JSON-safe values and bounded before they
reach model context. Binary values become a record such as
`{"type": "binary", "bytes": 2048}`. Large and recursive values become
deterministic summaries.

Structured tool results and model context identify the registered tool through
the `tool` field. Use `name=` during registration when that identity should
differ from the class or factory name.
