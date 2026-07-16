from __future__ import annotations

from collections.abc import Sequence

import anywidget
import traitlets

from ._state import StateSpec, _DefaultState, _GroupedState


class _WidgetGroup(anywidget.AnyWidget):
    """Render a fixed sequence of widgets through one MCP App root."""

    _esm = """
    async function render({ model, el, host, signal }) {
      const container = document.createElement("div");
      container.style.display = "grid";
      container.style.gap = "1rem";
      el.append(container);

      for (const ref of model.get("_items")) {
        signal.throwIfAborted();
        const childRoot = document.createElement("div");
        container.append(childRoot);
        const child = await host.getWidget(ref);
        await child.render({ el: childRoot, signal });
      }
    }
    export default { render };
    """

    _items = traitlets.List(anywidget.WidgetTrait()).tag(sync=True)

    def __init__(self, widgets: Sequence[anywidget.AnyWidget]) -> None:
        super().__init__(_items=list(widgets))


def _group_state(
    state: StateSpec | _DefaultState,
    widgets: Sequence[anywidget.AnyWidget],
) -> _GroupedState:
    """Apply one public state specification across a widget sequence."""
    return _GroupedState(tuple(widgets), state)
