"""Adapt AnyWidget comm messages and binary buffers to the browser protocol."""

from __future__ import annotations

import base64
import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


def _encode_buffer(buffer: bytes | bytearray | memoryview) -> str:
    return base64.b64encode(memoryview(buffer).tobytes()).decode("ascii")


def _decode_buffer(buffer: str) -> bytes:
    return base64.b64decode(buffer, validate=True)


@dataclass(frozen=True)
class WidgetMessage:
    model_id: str
    data: dict[str, Any]
    buffers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "modelId": self.model_id,
            "data": self.data,
            "buffers": list(self.buffers),
        }


class BridgeComm:
    """Implement the kernel-style comm interface expected by AnyWidget.

    Outbound buffers become base64 wire values. Inbound values are decoded into
    the callback envelope consumed by widget comm handlers.
    """

    kernel = True

    def __init__(self, comm_id: str, emit: Callable[[WidgetMessage], None]) -> None:
        self.comm_id = comm_id
        self._emit = emit
        self._on_msg: Callable[[dict[str, Any]], None] | None = None
        self._closed = False

    def on_msg(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_msg = callback

    def send(
        self,
        data: dict[str, Any] | None = None,
        buffers: Iterable[bytes | bytearray | memoryview] | None = None,
        **_: Any,
    ) -> None:
        if self._closed:
            return
        self._emit(
            WidgetMessage(
                model_id=self.comm_id,
                data=data or {},
                buffers=tuple(_encode_buffer(buffer) for buffer in buffers or ()),
            )
        )

    def receive(self, data: dict[str, Any], buffers: Iterable[str] = ()) -> None:
        if self._closed:
            raise RuntimeError("The widget session is closed")
        if self._on_msg is None:
            raise RuntimeError("The widget has no comm message handler")
        self._on_msg(
            {
                "content": {"data": copy.deepcopy(data)},
                "buffers": [_decode_buffer(buffer) for buffer in buffers],
            }
        )

    def close(self, **_: Any) -> None:
        self._closed = True
