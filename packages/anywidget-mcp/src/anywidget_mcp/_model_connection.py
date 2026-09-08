"""Connect widget models to bridge comms and capture their initial browser state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anywidget._descriptor import ReprMimeBundle

from ._comm import BridgeComm, WidgetMessage
from ._models import bind_model


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

    bindings = [bind_model(widget, controllers) for widget in widgets]
    for binding in bindings:
        current_model_id = binding.model_id
        comm = BridgeComm(
            current_model_id,
            lambda message, source=binding.source: capture(source, message),
        )
        binding.connect(comm)
        comms[current_model_id] = comm

    models: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        first_message = len(messages)
        binding.send_state()
        emitted = messages[first_message:]
        del messages[first_message:]
        current_model_id = binding.model_id
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
