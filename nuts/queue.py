import json
from redis import Redis
import logging


class WorkQueue():
    redis: Redis
    logger: logging.Logger

    pending_queue: str

    def __init__(self, redis: Redis, **kwargs):
        self.redis = redis
        self.logger = logging.getLogger(kwargs.get('logger', 'nuts|workqueue'))
        self.pending_queue = 'nuts|jobs|pending'

    def publish(self, job_name: str, job_parameters: object):
        self.redis.sadd(self.pending_queue, json.dumps([job_name, job_parameters]))
