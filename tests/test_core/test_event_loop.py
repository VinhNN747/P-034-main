import asyncio
import sys

from src.shared.core.event_loop import database_compatible_loop_factory


def test_database_compatible_loop_factory():
    loop = database_compatible_loop_factory()
    try:
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
        assert loop.run_until_complete(asyncio.sleep(0, result="ok")) == "ok"
    finally:
        loop.close()
