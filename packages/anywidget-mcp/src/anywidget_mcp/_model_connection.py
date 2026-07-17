"""Connect widget models to bridge comms and capture their initial browser state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from anywidget import AnyWidget
from anywidget._descriptor import ReprMimeBundle

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

    protocol_sync: dict[int, bool] = {}
    for widget in widgets:
        current_model_id = model_id(widget, controllers)
        controller = protocol_controller(widget, controllers)
        if isinstance(widget, AnyWidget):
            old_comm = widget.comm
        else:
            assert controller is not None
            old_comm = controller._comm
            protocol_sync[id(widget)] = bool(
                getattr(old_comm, "_msg_callback", None) or controller._disconnectors
            )
            controller.unsync_object_with_view()
        if old_comm is not None:
            old_comm.close()
        comm = BridgeComm(
            current_model_id,
            lambda message, source=widget: capture(source, message),
        )
        if isinstance(widget, AnyWidget):
            widget.comm = comm
        else:
            assert controller is not None
            cast(Any, controller)._comm = comm
        comms[current_model_id] = comm

    models: dict[str, dict[str, Any]] = {}
    for widget in widgets:
        first_message = len(messages)
        controller = protocol_controller(widget, controllers)
        if isinstance(widget, AnyWidget):
            widget.send_state()
        else:
            assert controller is not None
            if protocol_sync[id(widget)]:
                controller.sync_object_with_view()
            else:
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
