import datetime
from redis import Redis
from . import lua


class ConcurrencyGate:
    '''
        Per-key concurrency limiting (e.g. max N in-flight items for a given
        vendor id or org id), backed by a single atomic Lua script so
        evict-expired + check-limit + admit never races across workers.
    '''
    redis: Redis
    key_prefix: str

    def __init__(self, redis: Redis, key_prefix: str = 'nuts|concurrency|'):
        self.redis = redis
        self.key_prefix = key_prefix

    def acquire(self, key: str, holder: str, limit: int, lease_seconds: int) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        return lua.concurrency_acquire(self.redis, f'{self.key_prefix}{key}', holder, limit, lease_seconds, now)

    def release(self, key: str, holder: str) -> None:
        lua.concurrency_release(self.redis, f'{self.key_prefix}{key}', holder)
