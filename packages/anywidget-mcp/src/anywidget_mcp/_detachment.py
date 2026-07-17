"""Retain removed widget models until browser removal is acknowledged."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from anywidget._descriptor import ReprMimeBundle

from ._comm import BridgeComm
from ._widget_protocol import close_widget, finalize_claim


@dataclass
class DetachedModel:
    """Record cleanup progress for one model removed from the live graph."""

    widget: object
    comm: BridgeComm | None
    comm_closed: bool = False
    gate_restored: bool = False
    widget_closed: bool = False


class DetachedModels:
    """Manage models between graph removal and browser acknowledgment.

    Announced models remain callable until acknowledgment completes their comm,
    notification gate, widget, and ownership cleanup.
    """

    def __init__(self) -> None:
        self._pending: dict[str, DetachedModel] = {}
        self._announced: dict[str, DetachedModel] = {}
        self._acknowledged: set[str] = set()

    def values(self) -> tuple[DetachedModel, ...]:
        return (*self._pending.values(), *self._announced.values())

    def identities(self) -> set[int]:
        return {id(detached.widget) for detached in self.values()}

    def clear(self) -> None:
        self._pending.clear()
        self._announced.clear()
        self._acknowledged.clear()

    def add(self, model_id: str, widget: object, comm: BridgeComm | None) -> None:
        self._pending[model_id] = DetachedModel(widget=widget, comm=comm)

    def acknowledge(
        self,
        model_ids: Iterable[str],
        *,
        messages: list[Any],
        comms: dict[str, BridgeComm],
        controllers: dict[int, ReprMimeBundle],
        restore_gate: Callable[[object], None],
    ) -> list[Any]:
        """Finalize acknowledged models and retain messages for failed cleanup.

        Successful IDs remain recognized until the full acknowledgment set
        completes, which lets callers retry a partially failed set.
        """

        acknowledged = tuple(dict.fromkeys(model_ids))
        unknown = next(
            (
                model_id
                for model_id in acknowledged
                if model_id not in self._announced
                and model_id not in self._acknowledged
            ),
            None,
        )
        if unknown is not None:
            raise KeyError(
                f"Widget model removal is not awaiting acknowledgment: {unknown}"
            )
        detached = {
            model_id: self._announced[model_id]
            for model_id in acknowledged
            if model_id in self._announced
        }
        failed, errors = _finalize(
            detached,
            comms=comms,
            controllers=controllers,
            restore_gate=restore_gate,
        )
        for model_id in detached:
            if model_id in failed:
                continue
            self._announced.pop(model_id, None)
            self._acknowledged.add(model_id)
        retained = [
            message
            for message in messages
            if getattr(message, "model_id", None) not in detached
            or message.model_id in failed
        ]
        if errors:
            raise ExceptionGroup("Failed to finalize detached widget models", errors)
        self._acknowledged.difference_update(acknowledged)
        return retained

    def finalize_pending(
        self,
        *,
        comms: dict[str, BridgeComm],
        controllers: dict[int, ReprMimeBundle],
        restore_gate: Callable[[object], None],
    ) -> None:
        """Finalize models removed before their removal reached the browser."""

        failed, errors = _finalize(
            dict(self._pending),
            comms=comms,
            controllers=controllers,
            restore_gate=restore_gate,
        )
        self._pending = failed
        if errors:
            raise ExceptionGroup("Failed to finalize detached widget models", errors)

    def announce(self, model_ids: Iterable[str]) -> None:
        for model_id in model_ids:
            detached = self._pending.pop(model_id, None)
            if detached is not None:
                self._announced[model_id] = detached


def _finalize(
    detached: dict[str, DetachedModel],
    *,
    comms: dict[str, BridgeComm],
    controllers: dict[int, ReprMimeBundle],
    restore_gate: Callable[[object], None],
) -> tuple[dict[str, DetachedModel], list[Exception]]:
    """Advance each model through retryable comm, gate, widget, and claim cleanup."""

    failed: dict[str, DetachedModel] = {}
    errors: list[Exception] = []
    for model_id, detached_model in detached.items():
        widget = detached_model.widget
        comm = detached_model.comm
        model_errors: list[Exception] = []
        if comm is not None and not detached_model.comm_closed:
            try:
                comm.close()
                if comms.get(model_id) is comm:
                    comms.pop(model_id, None)
            except Exception as error:
                model_errors.append(error)
            else:
                detached_model.comm_closed = True
        if not detached_model.gate_restored:
            try:
                restore_gate(widget)
            except Exception as error:
                model_errors.append(error)
            else:
                detached_model.gate_restored = True
        if not detached_model.widget_closed:
            try:
                close_widget(widget, controllers)
            except Exception as error:
                model_errors.append(error)
            else:
                detached_model.widget_closed = True
        if model_errors:
            failed[model_id] = detached_model
            errors.extend(model_errors)
        else:
            finalize_claim(widget, controllers)
            controllers.pop(id(widget), None)
    return failed, errors
