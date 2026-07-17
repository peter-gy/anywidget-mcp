"""Unify graph identity and lifecycle for native and descriptor-backed widgets."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from anywidget import AnyWidget
from anywidget._descriptor import ReprMimeBundle

_claimed_widgets: dict[int, tuple[Callable[[], object | None], str, bool]] = {}
_claimed_widgets_lock = threading.RLock()
_CLOSED_MODEL_ID_ATTR = "_anywidget_mcp_closed_model_id"


class WidgetInUseError(RuntimeError):
    pass


class WidgetClaimCleanupError(ExceptionGroup):
    """Report claim rejection with models whose cleanup can be retried."""

    widgets: tuple[object, ...]


def claim_widgets(
    widgets: list[object],
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    """Claim a fresh model graph for one session.

    If any model is already claimed, fresh models in the rejected graph are
    closed before the ownership error is raised.
    """

    with _claimed_widgets_lock:
        reused = next(
            (widget for widget in widgets if claim_for(widget, controllers)),
            None,
        )
        if reused is not None:
            claim = claim_for(reused, controllers)
            assert claim is not None
            use_error = WidgetInUseError(
                f"Widget model {claim[1]} was already returned by a widget tool. "
                "Return a fresh root and fresh nested widgets for every tool call."
            )
            failed: list[object] = []
            cleanup_errors: list[Exception] = []
            for widget in reversed(widgets):
                if claim_for(widget, controllers) is None:
                    try:
                        close_widget(widget, controllers)
                    except Exception as error:
                        failed.append(widget)
                        cleanup_errors.append(error)
            if cleanup_errors:
                claim_error = WidgetClaimCleanupError(
                    "Failed to reject and clean up reused widget graph",
                    [use_error, *cleanup_errors],
                )
                claim_error.widgets = tuple(failed)
                raise claim_error from use_error
            raise use_error
        for widget in widgets:
            identity = id(widget)
            try:
                reference: Callable[[], object | None] = weakref.ref(
                    widget,
                    lambda expired, identity=identity: _remove_claim(
                        identity,
                        expired,
                    ),
                )
                weak = True
            except TypeError:

                def strong_reference(value: object = widget) -> object:
                    return value

                reference = strong_reference
                weak = False
            _claimed_widgets[identity] = (reference, model_id(widget), weak)


def claim_for(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> tuple[Callable[[], object | None], str, bool] | None:
    identity = id(widget)
    claim = _claimed_widgets.get(identity)
    if claim is None:
        controller = protocol_controller(widget, controllers)
        closed_model_id = (
            getattr(controller, _CLOSED_MODEL_ID_ATTR, None)
            if controller is not None
            else None
        )
        if not isinstance(closed_model_id, str):
            return None
        return (lambda: None, closed_model_id, False)
    if claim[0]() is widget:
        return claim
    _claimed_widgets.pop(identity, None)
    return None


def finalize_claim(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    identity = id(widget)
    with _claimed_widgets_lock:
        claim = _claimed_widgets.get(identity)
        if claim is None or claim[0]() is not widget or claim[2]:
            return
        _claimed_widgets.pop(identity, None)
        controller = protocol_controller(widget, controllers)
        if controller is not None:
            setattr(controller, _CLOSED_MODEL_ID_ATTR, claim[1])


def _remove_claim(
    identity: int,
    reference: Callable[[], object | None],
) -> None:
    with _claimed_widgets_lock:
        claim = _claimed_widgets.get(identity)
        if claim is not None and claim[0] is reference:
            _claimed_widgets.pop(identity, None)


def collect_widgets(
    root: AnyWidget,
    controllers: dict[int, ReprMimeBundle] | None = None,
    collected: list[object] | None = None,
) -> list[object]:
    """Return the distinct model graph reachable through synchronized state."""

    widgets = collected if collected is not None else []
    pending: list[object] = [root]
    seen: set[int] = set()
    while pending:
        widget = pending.pop()
        identity = id(widget)
        if identity in seen:
            continue
        seen.add(identity)
        widgets.append(widget)
        for value in synchronized_values(widget, controllers):
            collect_nested_widgets(value, pending, controllers)
    return widgets


def close_unclaimed_widget_graphs(roots: Iterable[AnyWidget]) -> None:
    """Close fresh widget graphs while preserving models owned by live sessions."""
    controllers: dict[int, ReprMimeBundle] = {}
    widgets: list[object] = []
    seen: set[int] = set()
    for root in roots:
        for widget in collect_widgets(root, controllers):
            identity = id(widget)
            if identity in seen:
                continue
            seen.add(identity)
            widgets.append(widget)

    errors: list[Exception] = []
    for widget in reversed(widgets):
        if safe_claim_for(widget, controllers) is not None:
            continue
        try:
            close_widget(widget, controllers)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("Failed to close unclaimed widget graphs", errors)


def safe_claim_for(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> tuple[Callable[[], object | None], str, bool] | None:
    try:
        return claim_for(widget, controllers)
    except Exception:
        return None


def protocol_controller(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> ReprMimeBundle | None:
    """Resolve and optionally cache a descriptor-backed model controller."""

    if isinstance(widget, AnyWidget):
        return None
    identity = id(widget)
    if controllers is not None:
        cached = controllers.get(identity)
        if cached is not None:
            return cached
    controller = getattr(widget, "_repr_mimebundle_", None)
    if not isinstance(controller, ReprMimeBundle):
        return None
    if controllers is not None:
        controllers[identity] = controller
    return controller


def model_id(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> str:
    if isinstance(widget, AnyWidget):
        return widget.model_id
    controller = protocol_controller(widget, controllers)
    if controller is None:
        raise TypeError(f"{type(widget).__name__} is not an AnyWidget-compatible model")
    return controller.model_id


def synchronized_values(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None,
) -> Iterable[object]:
    if isinstance(widget, AnyWidget):
        for name, trait in widget.traits().items():
            if trait.metadata.get("sync"):
                yield getattr(widget, name)
        return

    controller = protocol_controller(widget, controllers)
    if controller is None:
        return
    state = controller._get_state(widget, include=None)
    yield from state.values()
    yield from controller._extra_state.values()


def collect_nested_widgets(
    value: object,
    pending: list[object],
    controllers: dict[int, ReprMimeBundle] | None,
    seen: set[int] | None = None,
) -> None:
    if isinstance(value, AnyWidget) or protocol_controller(value, controllers):
        pending.append(value)
        return
    if not isinstance(value, (Mapping, list, tuple)):
        return
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    nested = value.values() if isinstance(value, Mapping) else value
    for item in nested:
        collect_nested_widgets(item, pending, controllers, seen)


def replace_widget_refs(
    value: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
    seen: set[int] | None = None,
) -> object:
    """Replace nested models with browser references while preserving containers.

    Recursive containers raise ``ValueError`` because synchronized browser state
    cannot represent their cycles.
    """

    if isinstance(value, AnyWidget) or protocol_controller(value, controllers):
        return f"anywidget:{model_id(value, controllers)}"
    if isinstance(value, Mapping):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError(
                "Synchronized widget state cannot contain recursive mappings"
            )
        seen.add(identity)
        try:
            return {
                key: replace_widget_refs(item, controllers, seen)
                for key, item in value.items()
            }
        finally:
            seen.remove(identity)
    if isinstance(value, list):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError("Synchronized widget state cannot contain recursive lists")
        seen.add(identity)
        try:
            return [replace_widget_refs(item, controllers, seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, tuple):
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            raise ValueError(
                "Synchronized widget state cannot contain recursive tuples"
            )
        seen.add(identity)
        try:
            return tuple(replace_widget_refs(item, controllers, seen) for item in value)
        finally:
            seen.remove(identity)
    return value


def synced_trait_names(widget: object) -> tuple[str, ...]:
    traits = getattr(widget, "traits", None)
    observe = getattr(widget, "observe", None)
    unobserve = getattr(widget, "unobserve", None)
    if not callable(traits) or not callable(observe) or not callable(unobserve):
        return ()
    return tuple(cast(dict[str, Any], traits(sync=True)))


def observe(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    observer = getattr(widget, "observe")
    observer(callback, names=names)


def unobserve(
    widget: object,
    callback: Callable[[object], None],
    names: tuple[str, ...],
) -> None:
    unobserver = getattr(widget, "unobserve")
    unobserver(callback, names=names)


def close_widget(
    widget: object,
    controllers: dict[int, ReprMimeBundle] | None = None,
) -> None:
    if isinstance(widget, AnyWidget):
        widget.close()
        return
    controller = protocol_controller(widget, controllers)
    if controller is None:
        return
    controller.unsync_object_with_view()
    controller._comm.close()


def contains_widget_ref(value: object, references: set[str]) -> bool:
    if isinstance(value, str):
        return value in references
    if isinstance(value, Mapping):
        return any(contains_widget_ref(item, references) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_widget_ref(item, references) for item in value)
    return False
