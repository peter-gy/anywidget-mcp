from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, overload

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ._state import DEFAULT_STATE
from ._targets import (
    TargetT,
    WidgetState,
    WidgetTarget,
    WidgetTargetDescription,
    describe_widget_target,
)
from ._widget_tools import (
    APP_MIME_TYPE,
    APP_RESOURCE_URI,
    AppCSP,
    AppPermissions,
    WidgetTools,
)

Transport = Literal["streamable-http", "stdio"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class _MCPMethodMiddleware:
    def __init__(self, app: ASGIApp, path: str) -> None:
        self._app = app
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        if scope["method"] == "HEAD":
            response = Response(
                status_code=200,
                headers={"Allow": "GET, POST, DELETE, HEAD"},
            )
            await response(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            response = Response(
                status_code=405,
                headers={"Allow": "GET, POST, DELETE, HEAD"},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def attach(
    mcp: FastMCP,
    *,
    app_uri: str = APP_RESOURCE_URI,
    csp: AppCSP | None = None,
    permissions: AppPermissions | None = None,
    prefers_border: bool = True,
    session_idle_timeout: float = 900.0,
) -> WidgetTools:
    """Attach AnyWidget registration and session handling to ``mcp``.

    Call this before creating the streamable HTTP app. The adapter composes
    session cleanup into the server lifespan. Await :meth:`WidgetTools.aclose`
    to close live sessions early.
    """
    if not isinstance(mcp, FastMCP):
        raise TypeError("attach() expected a FastMCP server")
    if getattr(mcp, "_anywidget_tools", None) is not None:
        raise ValueError("AnyWidget tools are already attached to this FastMCP server")
    if getattr(mcp, "_session_manager", None) is not None:
        raise ValueError("attach() must run before streamable_http_app() is created")
    tools = WidgetTools(
        mcp,
        app_uri=app_uri,
        csp=csp,
        permissions=permissions,
        prefers_border=prefers_border,
        session_idle_timeout=session_idle_timeout,
    )
    setattr(mcp, "_anywidget_tools", tools)
    return tools


class AnyWidgetMCP(FastMCP):
    """Run a FastMCP server that owns its AnyWidget session adapter."""

    def __init__(
        self,
        name: str | None = None,
        *,
        app_uri: str = APP_RESOURCE_URI,
        csp: AppCSP | None = None,
        permissions: AppPermissions | None = None,
        prefers_border: bool = True,
        cors_origins: Sequence[str] = (),
        session_idle_timeout: float = 900.0,
        **fastmcp_options: Any,
    ) -> None:
        super().__init__(name, **fastmcp_options)
        self._cors_origins = tuple(cors_origins)
        self._widget_tools = attach(
            self,
            app_uri=app_uri,
            csp=csp,
            permissions=permissions,
            prefers_border=prefers_border,
            session_idle_timeout=session_idle_timeout,
        )

    @overload
    def widget(
        self,
        target: TargetT,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> TargetT: ...

    @overload
    def widget(
        self,
        target: None = None,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> Callable[[TargetT], TargetT]: ...

    def widget(
        self,
        target: TargetT | None = None,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> TargetT | Callable[[TargetT], TargetT]:
        """Register an AnyWidget class or factory as an MCP App tool."""
        return self._widget_tools.widget(
            target,
            name=name,
            title=title,
            description=description,
            state=state,
            annotations=annotations,
            icons=icons,
        )

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.add_middleware(
            _MCPMethodMiddleware,
            path=self.settings.streamable_http_path,
        )
        if self._cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(self._cors_origins),
                allow_methods=["GET", "POST", "DELETE", "HEAD"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            )
        return app

    async def aclose(self) -> None:
        """Close every live widget session in the active server lifespan."""
        await self._widget_tools.aclose()


def serve(
    target: WidgetTarget,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    state: WidgetState = DEFAULT_STATE,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
    transport: Transport = "streamable-http",
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: LogLevel = "INFO",
    **fastmcp_options: Any,
) -> None:
    """Register one widget target and run its MCP server until the transport exits.

    Args:
        target: An ``AnyWidget`` subclass or factory registered as the sole widget
            tool.
        name: Tool name forwarded to :meth:`AnyWidgetMCP.widget`.
        title: Human-facing tool title.
        description: Tool description.
        state: Model-visible state selection or a read-only projection callable.
        annotations: Standard MCP tool behavior hints.
        icons: Icons applied to the sole widget tool and its MCP server.
        transport: ``"streamable-http"`` or ``"stdio"``.
        host: Streamable HTTP bind address.
        port: Streamable HTTP bind port.
        log_level: Server log level.
        **fastmcp_options: Additional options forwarded to :class:`AnyWidgetMCP`.

    Raises:
        TypeError: If the target, signature, or state option is invalid.
        ValueError: If the tool name is reserved or already registered.

    Registration errors propagate before the transport starts. Widget sessions
    are closed when the transport exits or raises.

    Example:
        ``serve(ColorPicker, transport="stdio")``
    """
    target_name = getattr(target, "__name__", type(target).__name__)
    server = AnyWidgetMCP(
        f"{target_name} MCP",
        host=host,
        port=port,
        log_level=log_level,
        icons=icons,
        **fastmcp_options,
    )
    server.widget(
        target,
        name=name,
        title=title,
        description=description,
        state=state,
        annotations=annotations,
        icons=icons,
    )
    server.run(transport=transport)


__all__ = [
    "APP_MIME_TYPE",
    "APP_RESOURCE_URI",
    "AnyWidgetMCP",
    "AppCSP",
    "AppPermissions",
    "WidgetTargetDescription",
    "WidgetTools",
    "attach",
    "describe_widget_target",
    "serve",
]
