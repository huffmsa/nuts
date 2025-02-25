from typing import Union
import json
from redis import Redis
import logging


class WorkQueue():
    redis: Redis
    logger: logging.Logger

    pending_queue: str

    def __init__(self, redis: Redis, **kwargs):
        self.redis = redis
        self.pending_queue = 'nuts|jobs|pending'

        self.logger = logging.getLogger(kwargs.get('logger', 'nuts|workqueue'))
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def publish(self, job_name: str, job_parameters: Union[object, list]):
        try:
            payload = json.dumps([job_name, job_parameters])
            self.logger.info(f'Added {job_name} {payload}')
            self.redis.sadd(self.pending_queue, payload)

        except Exception as ex:
            self.logger.error(f'Error adding {job_name} {payload}: {ex}')
