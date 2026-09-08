import marimo

__generated_with = "0.24.0"
app = marimo.App()


@app.cell
def _():
    import asyncio

    import anywidget
    import marimo as mo
    import traitlets

    from anywidget_mcp import webmcp

    return anywidget, asyncio, mo, traitlets, webmcp


@app.cell
def _(anywidget, asyncio, traitlets):
    closed = []

    class Counter(anywidget.AnyWidget):
        value = traitlets.Int(1, min=0).tag(sync=True)
        doubled = traitlets.Int(2, read_only=True).tag(sync=True)
        notes = traitlets.Unicode("private notebook note").tag(sync=True)
        _esm = """
        export default {
          render({model, el, signal}) {
            const output = document.createElement('output');
            output.setAttribute('aria-label', 'Counter state');
            const draw = () => {
              output.textContent = `${model.get('value')} / ${model.get('doubled')}`;
            };
            model.on('change:value', draw);
            model.on('change:doubled', draw);
            const button = document.createElement('button');
            button.textContent = 'Increment';
            button.addEventListener('click', () => {
              model.set('value', model.get('value') + 1);
              model.save_changes();
            }, {signal});
            draw();
            el.append(output, button);
            return () => {
              model.off('change:value', draw);
              model.off('change:doubled', draw);
            };
          }
        };
        """

        def __init__(self, start: int = 1):
            super().__init__(value=start)

        @traitlets.observe("value")
        def _double(self, change):
            self.set_trait("doubled", change.new * 2)

        @traitlets.validate("value")
        def _validate_value(self, proposal):
            if proposal.value == 13:
                raise traitlets.TraitError("Counter value 13 is reserved")
            return proposal.value

        def close(self):
            if self.comm is not None:
                closed.append(self.model_id)
            super().close()

    def create_counter(start: int = 3, fail: bool = False) -> Counter:
        """Create an interactive counter at the requested value."""
        counter = Counter(start=start)
        if fail:
            raise ValueError("Counter creation failed after allocation")
        return counter

    async def create_async_counter(start: int = 5) -> Counter:
        """Create a read-only counter after asynchronous work finishes."""
        await asyncio.sleep(0)
        return Counter(start=start)

    return Counter, closed, create_async_counter, create_counter


@app.cell
def _(mo):
    restart = mo.ui.button(
        value=0, on_click=lambda value: value + 1, label="Restart WebMCP"
    )
    restart  # pyright: ignore[reportUnusedExpression]
    return (restart,)


@app.cell
def _(Counter, closed, create_async_counter, create_counter, mo, restart, webmcp):
    restart.value
    tools = webmcp.enable(
        widgets={
            Counter: {"private": {"notes"}},
            create_counter: {},
            create_async_counter: {"read_only": True},
        },
        discover=False,
    )
    mo.vstack([tools, mo.md(f"Closed widgets: {len(closed)}")])
    return (tools,)


if __name__ == "__main__":
    app.run()
