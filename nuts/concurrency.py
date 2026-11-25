import datetime
from typing import Union
from redis import Redis
from . import lua
from . import dispatch

WAITING_KEYS_SET = 'nuts|concurrency|waiting_keys'


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


class ConcurrencyGate:
    '''
        Per-key concurrency limiting (e.g. max N in-flight items for a given
        vendor id or org id). Slots are taken when a job starts running, not
        when it's queued. A job that can't get a slot is parked in a FIFO
        list for its key and handed the next free slot.
    '''
    redis: Redis
    key_prefix: str

    def __init__(self, redis: Redis, key_prefix: str = 'nuts|concurrency|'):
        self.redis = redis
        self.key_prefix = key_prefix

    def holders_key(self, key: str) -> str:
        return f'{self.key_prefix}{key}'

    def waiting_key(self, key: str) -> str:
        return f'{self.key_prefix}waiting|{key}'

    def acquire(self, key: str, holder: str, limit: int, lease_seconds: int, park_payload: Union[str, None] = None) -> bool:
        '''
            Take a slot for holder. If none is free and park_payload is given,
            the payload is parked atomically and will be re-dispatched by
            release() or pump() once a slot frees.
        '''
        return lua.concurrency_acquire(
            self.redis, self.holders_key(key), self.waiting_key(key), WAITING_KEYS_SET, key,
            holder, limit, lease_seconds, _now(), park_payload,
        )

    def refresh(self, key: str, holder: str, lease_seconds: int) -> None:
        self.redis.zadd(self.holders_key(key), {holder: _now() + lease_seconds}, xx=True)

    def release(self, key: str, holder: str, lease_seconds: int = 300) -> int:
        return self._pump(key, holder, lease_seconds)

    def pump(self, key: str, lease_seconds: int = 300) -> int:
        '''Hand slots freed by expired leases to parked waiters.'''
        return self._pump(key, None, lease_seconds)

    def waiting_keys(self) -> list[str]:
        return [k.decode() for k in self.redis.smembers(WAITING_KEYS_SET)]

    def _pump(self, key: str, release_holder: Union[str, None], lease_seconds: int) -> int:
        return lua.concurrency_pump(
            self.redis, self.holders_key(key), self.waiting_key(key), WAITING_KEYS_SET,
            [dispatch.stream_key(p) for p in dispatch.PRIORITIES], key,
            release_holder, lease_seconds, _now(),
        )
