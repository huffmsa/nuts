"""
Small, single-round-trip Lua scripts for atomic operations that would
otherwise need WATCH/MULTI (which doesn't fit redis-py's connection-pooled,
one-shot call pattern used throughout this codebase).
"""
import json
from typing import Any, Union
from redis import Redis

_CAS_SET = """
local current = redis.call('GET', KEYS[1])
if current == false then
    return 0
end
local doc = cjson.decode(current)
if tostring(doc._v) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2])
if ARGV[3] ~= '' then
    redis.call('EXPIRE', KEYS[1], ARGV[3])
end
return 1
"""

_CONCURRENCY_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
    return 1
end
return 0
"""


def cas_set(redis: Redis, key: str, expected_version: int, new_value: dict, ttl_seconds: Union[int, None] = None) -> bool:
    '''
        Atomically replace key's JSON document with new_value, but only if
        the document currently stored still has `_v == expected_version`.
        Returns True on success, False if the version didn't match (caller
        should re-read and retry) or the key doesn't exist.
    '''
    script = redis.register_script(_CAS_SET)
    result = script(
        keys=[key],
        args=[str(expected_version), json.dumps(new_value), str(ttl_seconds) if ttl_seconds else ''],
    )
    return bool(result)


def concurrency_acquire(redis: Redis, key: str, holder: str, limit: int, lease_seconds: int, now: float) -> bool:
    '''
        Atomically evict expired holders from the concurrency set at `key`,
        then admit `holder` if the number of live holders is under `limit`.
        Returns True if the slot was acquired.
    '''
    script = redis.register_script(_CONCURRENCY_ACQUIRE)
    result = script(
        keys=[key],
        args=[holder, str(limit), str(now + lease_seconds), str(now)],
    )
    return bool(result)


def concurrency_release(redis: Redis, key: str, holder: str) -> None:
    redis.zrem(key, holder)
