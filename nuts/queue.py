import datetime
from typing import Union
from redis import Redis
import logging
from . import dispatch
from .job import NutsJob


class WorkQueue():
    redis: Redis
    logger: logging.Logger

    delayed_queue: str

    def __init__(self, redis: Redis, **kwargs):
        self.redis = redis
        # Separate from nuts|jobs|scheduled (which only ever holds bare cron
        # job names, deduped by name) -- delayed ad-hoc dispatches are
        # anonymous JSON payloads with no dedup concept, so they get their
        # own key even though both are drained the same way (due_items()).
        self.delayed_queue = 'nuts|jobs|delayed'

        self.logger = logging.getLogger(kwargs.get('logger', 'nuts|workqueue'))
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def publish(
        self,
        job_name: str,
        job_parameters: Union[dict, None] = None,
        *,
        priority: str = 'normal',
        delay_seconds: float = 0,
        concurrency_key: Union[str, None] = None,
        concurrency_limit: int = 1,
        max_attempts: Union[int, None] = None,
    ):
        '''
            Dispatch job_name for execution. By default it's pushed onto the
            priority stream immediately; pass delay_seconds to stage it in
            the delayed-due zset instead, where the leader will pick it up
            once due (the same due_items() mechanism retries use).
        '''
        try:
            payload = dispatch.build_payload(
                job_name,
                job_parameters,
                priority=priority,
                max_attempts=max_attempts if max_attempts is not None else NutsJob.max_attempts,
                concurrency_key=concurrency_key,
                concurrency_limit=concurrency_limit,
            )
            if delay_seconds > 0:
                due_ts = datetime.datetime.now(datetime.timezone.utc).timestamp() + delay_seconds
                self.logger.info(f'Scheduling {job_name} in {delay_seconds}s: {payload}')
                dispatch.dispatch_later(self.redis, self.delayed_queue, payload, due_ts)
            else:
                self.logger.info(f'Dispatching {job_name}: {payload}')
                dispatch.enqueue_payload(self.redis, payload)

        except Exception as ex:
            self.logger.error(f'Error publishing {job_name}: {ex}')
