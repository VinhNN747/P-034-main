"""Event-loop factories used by the async application runtime.

The async database stack used by this project is compatible with the
selector-based event loop on Windows.  Uvicorn expects a factory returning a
fresh event loop, so keep this function side-effect free and do not install a
global event-loop policy here.
"""

from __future__ import annotations

import asyncio
import selectors


def database_compatible_loop_factory() -> asyncio.AbstractEventLoop:
    """Create a selector-based event loop for database/ASGI execution.

    ``SelectorEventLoop`` is available on all supported platforms and avoids
    Windows Proactor-specific incompatibilities in async database drivers.
    Uvicorn calls this factory once per server loop.
    """

    return asyncio.SelectorEventLoop(selectors.SelectSelector())


__all__ = ["database_compatible_loop_factory"]
