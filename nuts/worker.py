import os
from redis import Redis
from .job import Job


class Worker():
    '''
        A NUTS worker
    '''
    id: int
    redis: Redis

    def __init__(self, redis: Redis, jobs: list[Job]):
        self.id = os.getpid()
        self.redis = redis

        is_leader = self.check_leader()

        if is_leader:
            # TODO: Register cron jobs
            print(is_leader)

    def check_leader(self) -> bool:
        is_leader = False
        leadership_check = self.redis.setnx('leader_id', self.id)

        if not leadership_check:
            leader_id = self.redis.get('leader_id')

            if leader_id == self.id:
                self.redis.setex('leader_id', 600)

                is_leader = True

        else:
            self.redis.setex('leader_id', 600)
            is_leader = True

        return is_leader

    def run(self):
        print('running')
