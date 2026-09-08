from __future__ import annotations

import sys
from importlib.metadata import distribution
from importlib.resources import files

from anywidget_mcp import create_anywidget
from anywidget_mcp.server import AnyWidgetMCP, attach, serve


def verify_release(expected_version: str) -> None:
    installed = distribution("anywidget-mcp")
    if installed.version != expected_version:
        raise SystemExit(
            f"Installed anywidget-mcp version {installed.version} does not match "
            f"release {expected_version}"
        )

    public_objects = {
        "AnyWidgetMCP": AnyWidgetMCP,
        "attach": attach,
        "create_anywidget": create_anywidget,
        "serve": serve,
    }
    invalid_objects = [
        name
        for name, public_object in public_objects.items()
        if not callable(public_object)
    ]
    if invalid_objects:
        raise SystemExit(
            f"Public objects are not callable: {', '.join(invalid_objects)}"
        )

    app = files("anywidget_mcp").joinpath("static/index.html").read_bytes()
    if not app.startswith(b"<!doctype html>"):
        raise SystemExit("The packaged browser app is missing or invalid")

    notice_files = [
        path
        for path in installed.files or ()
        if str(path).endswith(".dist-info/licenses/THIRD_PARTY_NOTICES")
    ]
    if len(notice_files) != 1:
        raise SystemExit(
            f"The installed distribution contains {len(notice_files)} notice files"
        )
    notices = installed.locate_file(notice_files[0]).read_text()
    if not notices.startswith("Third-Party Notices\n"):
        raise SystemExit("The packaged third-party notices are invalid")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/verify_release.py VERSION")

    expected_version = sys.argv[1]
    verify_release(expected_version)
    print(f"Verified anywidget-mcp {expected_version}")


if __name__ == "__main__":
    main()
