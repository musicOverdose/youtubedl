"""
Telemetry sidecar for Telegram Local Bot API.
Periodically measures disk usage of /var/lib/telegram-bot-api and /tmp/telegram-bot-api
and reports metrics to Redis under keys:
- telemetry:local_bot_api:data_bytes
- telemetry:local_bot_api:temp_bytes
"""
import os
import signal
import time
from typing import Optional

import redis

from src.core.logger import get_logger

logger = get_logger("local_bot_api_telemetry")


def get_dir_size(path: str) -> int:
    """Calculate total size of regular files under path in bytes."""
    total = 0
    if not os.path.exists(path):
        return 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.exists(fp) and not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return total


def collect_and_publish_metrics(
    r: redis.Redis,
    data_dir: str = "/var/lib/telegram-bot-api",
    temp_dir: str = "/tmp/telegram-bot-api",
) -> tuple[int, int]:
    """Collect directory sizes and publish them to Redis."""
    data_bytes = get_dir_size(data_dir)
    temp_bytes = get_dir_size(temp_dir)
    r.set("telemetry:local_bot_api:data_bytes", str(data_bytes))
    r.set("telemetry:local_bot_api:temp_bytes", str(temp_bytes))
    return data_bytes, temp_bytes


def main() -> None:
    running = True

    def _signal_handler(signum: int, frame: Optional[object]) -> None:
        nonlocal running
        logger.info("Received termination signal %s, exiting telemetry loop...", signum)
        running = False

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    logger.info("Starting Local Bot API telemetry sidecar connecting to Redis: %s", redis_url)

    r = redis.from_url(redis_url)

    while running:
        try:
            collect_and_publish_metrics(r)
        except Exception as e:
            logger.debug("Failed to record Local Bot API telemetry to Redis: %s", e)

        for _ in range(30):
            if not running:
                break
            time.sleep(1)

    try:
        r.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
