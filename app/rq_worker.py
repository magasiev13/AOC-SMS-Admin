from __future__ import annotations

import logging
import os
import socket
import time

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Worker
from rq.worker import DequeueStrategy

RQ_WORKER_TTL_SECONDS: int = 60


def _connect_redis(redis_url: str, attempts: int) -> Redis:
    logger = logging.getLogger(__name__)
    connection = Redis.from_url(redis_url)
    last_error: RedisError | OSError | None = None
    for attempt_number in range(1, attempts + 1):
        try:
            connection.ping()
            return connection
        except (RedisError, OSError) as exc:
            last_error = exc
            logger.warning(
                "Redis worker connection attempt failed",
                extra={
                    "attempt_number": attempt_number,
                    "attempts": attempts,
                    "redis_url_scheme": redis_url.split(":", 1)[0],
                },
            )
            if attempt_number < attempts:
                time.sleep(1)
    raise RuntimeError(f"RQ worker could not connect to Redis after {attempts} attempts: {last_error}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    redis_url = str(os.environ.get("REDIS_URL") or "").strip()
    queue_name = str(os.environ.get("RQ_QUEUE_NAME") or "").strip()
    worker_name = str(os.environ.get("RQ_WORKER_NAME") or "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for the RQ worker.")
    if not queue_name:
        raise RuntimeError("RQ_QUEUE_NAME is required for the RQ worker.")
    if (
        os.environ.get("SAAS_MODE") == "1"
        and str(os.environ.get("FLASK_ENV") or "").strip().lower() == "production"
        and queue_name != "twinevia-saas"
    ):
        raise RuntimeError("Production SaaS workers must consume only the twinevia-saas queue.")

    resolved_worker_name = worker_name or f"twinevia-{socket.gethostname()}-{os.getpid()}"
    connection = _connect_redis(redis_url, 3)
    queue = Queue(queue_name, connection=connection)
    worker = Worker(
        [queue],
        name=resolved_worker_name,
        connection=connection,
        default_worker_ttl=RQ_WORKER_TTL_SECONDS,
    )
    worker.work(
        burst=False,
        logging_level="INFO",
        date_format="%Y-%m-%dT%H:%M:%S%z",
        log_format="%(asctime)s %(levelname)s %(name)s %(message)s",
        max_jobs=None,
        max_idle_time=None,
        with_scheduler=False,
        dequeue_strategy=DequeueStrategy.DEFAULT,
    )


if __name__ == "__main__":
    main()
