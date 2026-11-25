import datetime
from typing import Union
from redis import Redis
import logging
from . import dispatch
from . import batch
from .job import NutsJob


class WorkQueue():
    redis: Redis
    logger: logging.Logger

    delayed_queue: str

    def __init__(self, redis: Redis, **kwargs):
        self.redis = redis
        # Separate from nuts|jobs|scheduled (which only ever holds bare cron
        # job names, deduped by name) -- delayed ad-hoc dispatches are
        # anonymous JSON payloads with no dedup concept.
        self.delayed_queue = dispatch.DELAYED_QUEUE

        self.logger = logging.getLogger(kwargs.get('logger', 'nuts|workqueue'))

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
    ) -> str:
        '''
            Dispatch job_name for execution and return its dispatch id, which
            can be passed to cancel(). Pass delay_seconds to stage it in the
            delayed-due zset instead; the leader moves it onto the stream once
            due. Raises if Redis rejects the write, so callers know it wasn't
            queued.
        '''
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
        return payload['dispatch_id']

    def publish_to_batch(
        self,
        job_name: str,
        job_parameters: Union[dict, None] = None,
        *,
        batch_key: str = '',
        max_items: int = 100,
        window_seconds: float = 5,
        priority: str = 'normal',
        concurrency_key: Union[str, None] = None,
        concurrency_limit: int = 1,
        max_attempts: Union[int, None] = None,
    ) -> str:
        '''
            Add one item to a batch job. The job runs with items=[params, ...]
            once max_items have arrived or window_seconds after the first
            item, whichever comes first. Returns the item id.
        '''
        if max_items < 1:
            raise ValueError('max_items must be at least 1')
        return batch.append(
            self.redis,
            job_name,
            job_parameters,
            batch_key=batch_key,
            max_items=max_items,
            window_seconds=window_seconds,
            priority=priority,
            max_attempts=max_attempts if max_attempts is not None else NutsJob.max_attempts,
            concurrency_key=concurrency_key,
            concurrency_limit=concurrency_limit,
        )

    def cancel(self, dispatch_id: str) -> None:
        dispatch.cancel(self.redis, dispatch_id)
