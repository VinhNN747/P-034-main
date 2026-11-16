import asyncio
import logging

from src.backend_ai.services.comparison_service import run_worker
from src.shared.core.event_loop import database_compatible_loop_factory

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with asyncio.Runner(loop_factory=database_compatible_loop_factory) as runner:
        runner.run(run_worker())
