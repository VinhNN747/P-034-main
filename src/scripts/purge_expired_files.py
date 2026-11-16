# Deletes the bytes of expired Files API uploads. Uploads already purge their
# own application's expired files; schedule this (e.g. hourly cron) so
# applications that stop uploading are swept too:
#   python src/scripts/purge_expired_files.py
# ruff: noqa: E402
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend_ai.gateway.factory import build_file_limits, get_file_storage
from src.backend_ai.gateway.files.service import GatewayFileService
from src.shared.config import get_settings
from src.shared.database.session import create_engine_from_settings, create_session_factory


async def main() -> int:
    settings = get_settings()
    engine = create_engine_from_settings()
    session_factory = create_session_factory(engine)
    total = 0
    try:
        async with session_factory() as session:
            service = GatewayFileService(session, get_file_storage(settings), build_file_limits(settings))
            while purged := await service.purge_expired(limit=500):
                total += purged
    finally:
        await engine.dispose()
    print(f"Purged {total} expired file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
