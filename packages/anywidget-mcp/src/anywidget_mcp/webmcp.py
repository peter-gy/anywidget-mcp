"""Expose AnyWidget instances and creation functions through WebMCP."""

from __future__ import annotations

from ._webmcp.policy import WidgetOptions
from ._webmcp.host import capture_host
from ._webmcp.session import Session, Widgets

__all__ = ["Session", "WidgetOptions", "enable", "disable", "is_enabled"]

_active: dict[int | None, Session] = {}


def _closed(session: Session) -> None:
    key = session._host.key
    if _active.get(key) is session:
        del _active[key]


def enable(*, widgets: Widgets | None = None, discover: bool | None = None) -> Session:
    """Configure WebMCP and return its displayable session.

    widgets accepts a sequence of AnyWidget instances, classes, and typed
    creation functions, or a mapping from targets to exposure settings. False
    excludes a target. Repeated calls patch the active session. Omitted fields
    retain their values. discover initially defaults to True and includes live
    and subsequently constructed AnyWidget instances.

    Display the returned session to publish creation tools and render their
    results. Python validates inputs and remains authoritative for widget state.
    Browser tool availability depends on WebMCP support and host permissions.
    """
    host = capture_host()
    active = _active.get(host.key)
    if active is not None:
        active.configure(widgets, discover)
        return active
    session = Session(_closed, host)
    try:
        session.configure(widgets, discover)
    except BaseException:
        session.close()
        raise
    _active[host.key] = session
    return session


def disable() -> None:
    """Withdraw WebMCP tools, leaving widgets open for their existing owners."""
    active = _active.pop(capture_host().key, None)
    if active is not None:
        active.disable()


def is_enabled() -> bool:
    """Return whether this Python session has active WebMCP exposure."""
    return capture_host().key in _active
