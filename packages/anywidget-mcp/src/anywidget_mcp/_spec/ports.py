"""Operations required by widget compilation and live model consumers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, TypeVar

from .types import CallableSpec, InputSpec, ModelSpec, TargetSpec


class SourceInspector(Protocol):
    def __call__(self, source: object, /) -> TargetSpec: ...


InputsT_co = TypeVar("InputsT_co", bound=InputSpec, covariant=True)


class InputCompiler(Protocol[InputsT_co]):
    def __call__(self, definition: CallableSpec, /) -> InputsT_co: ...


class CommPort(Protocol):
    kernel: bool
    comm_id: str

    def on_msg(self, callback: Callable[[dict[str, Any]], None] | None) -> None: ...

    def send(
        self,
        data: dict[str, Any] | None = None,
        buffers: Iterable[bytes | bytearray | memoryview] | None = None,
        **kwargs: Any,
    ) -> None: ...

    def close(self, **kwargs: Any) -> None: ...


class ModelBinding(Protocol):
    @property
    def source(self) -> object: ...

    @property
    def model_id(self) -> str: ...

    def describe(self) -> ModelSpec: ...

    def synchronized_values(self) -> Iterable[object]: ...

    def observe(
        self, callback: Callable[[Any], None], names: tuple[str, ...]
    ) -> None: ...

    def unobserve(
        self, callback: Callable[[Any], None], names: tuple[str, ...]
    ) -> None: ...

    def read(self, names: tuple[str, ...]) -> Mapping[str, Any]: ...

    def serialize(self, values: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def connect(self, comm: CommPort) -> None: ...

    def send_state(self) -> None: ...

    def close(self) -> None: ...


class OwnedSession(Protocol):
    def close(self) -> None: ...
