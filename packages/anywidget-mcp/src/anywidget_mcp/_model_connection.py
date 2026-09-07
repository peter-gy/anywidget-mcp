"""Connect widget models to bridge comms and capture their initial browser state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from anywidget import AnyWidget
from anywidget._descriptor import ReprMimeBundle
from anywidget._util import put_buffers

from ._comm import BridgeComm, WidgetMessage
from ._widget_protocol import model_id, protocol_controller


def connect_models(
    widgets: list[object],
    *,
    controllers: dict[int, ReprMimeBundle],
    comms: dict[str, BridgeComm],
    messages: list[WidgetMessage],
    capture: Callable[[object, WidgetMessage], None],
) -> dict[str, dict[str, Any]]:
    """Replace model comms and return their initial browser records.

    Initialization emissions become model state instead of entering the
    incremental outbound message queue.
    """

    for widget in widgets:
        current_model_id = model_id(widget, controllers)
        controller = protocol_controller(widget, controllers)
        if isinstance(widget, AnyWidget):
            old_comm = widget.comm
        else:
            assert controller is not None
            old_comm = controller._comm
        incoming = getattr(old_comm, "_msg_callback", None)
        if old_comm is not None:
            old_comm.on_msg(None)
            old_comm.close()
        comm = BridgeComm(
            current_model_id,
            lambda message, source=widget: capture(source, message),
        )
        if isinstance(widget, AnyWidget):
            widget.comm = comm
            comm.on_msg(_widget_message_handler(widget))
        else:
            assert controller is not None
            cast(Any, controller)._comm = comm
            comm.on_msg(incoming)
        comms[current_model_id] = comm

    models: dict[str, dict[str, Any]] = {}
    for widget in widgets:
        first_message = len(messages)
        controller = protocol_controller(widget, controllers)
        if isinstance(widget, AnyWidget):
            widget.send_state()
        else:
            assert controller is not None
            controller.send_state()
        emitted = messages[first_message:]
        del messages[first_message:]
        current_model_id = model_id(widget, controllers)
        initial = next(
            (
                message
                for message in reversed(emitted)
                if message.model_id == current_model_id
                and message.data.get("method") == "update"
                and isinstance(message.data.get("state"), dict)
            ),
            None,
        )
        if initial is None:
            raise RuntimeError("AnyWidget did not emit an initial state update")
        state = initial.data.get("state")
        assert isinstance(state, dict)
        models[current_model_id] = {
            "modelId": current_model_id,
            "state": state,
            "bufferPaths": initial.data.get("buffer_paths", []),
            "buffers": list(initial.buffers),
        }
    return models


def _widget_message_handler(widget: AnyWidget) -> Callable[[dict[str, Any]], None]:
    if type(widget)._handle_msg is not AnyWidget._handle_msg:
        return widget._handle_msg

    def receive(message: dict[str, Any]) -> None:
        data = message["content"]["data"]
        if data.get("method") == "update" and "state" in data:
            state = data["state"]
            if "buffer_paths" in data:
                put_buffers(state, data["buffer_paths"], message["buffers"])
            # The notebook message handler swallows trait validation errors.
            # MCP must report rejection before the browser accepts its echo.
            widget.set_state(state)
        else:
            widget._handle_msg(message)

    return receive
