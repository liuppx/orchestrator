from __future__ import annotations

import logging
import time
from typing import Callable


def run_forever(loop_interval_seconds: int, cycle: Callable[[], None], logger: logging.Logger) -> None:
    while True:
        started = time.time()
        cycle()
        elapsed = time.time() - started
        sleep_seconds = max(1, loop_interval_seconds - int(elapsed))
        logger.info("cycle completed, sleeping %ss", sleep_seconds)
        time.sleep(sleep_seconds)
