"""
Small, single-round-trip Lua scripts for atomic operations that would
otherwise need WATCH/MULTI (which doesn't fit redis-py's connection-pooled,
one-shot call pattern used throughout this codebase).

Several scripts touch keys they compute from their inputs (priority streams,
gate keys), so they assume a single Redis node, not Redis Cluster.
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

_DEL_IF_EQUALS = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_EXPIRE_IF_EQUALS = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# KEYS: holders zset, waiting list, waiting-keys set
# ARGV: holder, limit, lease expiry, now, payload to park ('' = don't park), gate name
# A holder that already has a slot (e.g. one handed to it by _CONCURRENCY_PUMP)
# is re-admitted, so a hand-off is never lost to a fresh arrival.
_CONCURRENCY_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
    return 1
end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
    return 1
end
if ARGV[5] ~= '' then
    redis.call('RPUSH', KEYS[2], ARGV[5])
    redis.call('SADD', KEYS[3], ARGV[6])
end
return 0
"""

# KEYS: holders zset, waiting list, waiting-keys set, high stream, normal stream, low stream
# ARGV: holder to release ('' = none), now, lease expiry, gate name
# Releases a slot, then hands free slots to waiters in FIFO order: the waiter
# is admitted as a holder and pushed back onto its priority stream.
_CONCURRENCY_PUMP = """
if ARGV[1] ~= '' then
    redis.call('ZREM', KEYS[1], ARGV[1])
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local streams = {high = KEYS[4], normal = KEYS[5], low = KEYS[6]}
local moved = 0
while true do
    local head = redis.call('LINDEX', KEYS[2], 0)
    if not head then
        redis.call('SREM', KEYS[3], ARGV[4])
        break
    end
    local doc = cjson.decode(head)
    if redis.call('ZCARD', KEYS[1]) >= tonumber(doc.concurrency_limit or 1) then
        break
    end
    redis.call('LPOP', KEYS[2])
    redis.call('ZADD', KEYS[1], ARGV[3], doc.dispatch_id)
    redis.call('XADD', streams[doc.priority] or KEYS[5], '*', 'data', head)
    moved = moved + 1
end
return moved
"""

# KEYS: buffer list, inflight list, flush marker
# ARGV: max items
# Moves up to max items from the shared buffer into this flush's own inflight
# list. A retried or reclaimed flush finds its inflight list already populated
# and gets the same items back. Returns {items, remaining_in_buffer}.
_BATCH_TAKE = """
local existing = redis.call('LRANGE', KEYS[2], 0, -1)
if #existing > 0 then
    return {existing, redis.call('LLEN', KEYS[1])}
end
redis.call('DEL', KEYS[3])
local taken = {}
for i = 1, tonumber(ARGV[1]) do
    local item = redis.call('LPOP', KEYS[1])
    if not item then
        break
    end
    redis.call('RPUSH', KEYS[2], item)
    taken[#taken + 1] = item
end
if #taken > 0 then
    redis.call('EXPIRE', KEYS[2], 604800)
end
return {taken, redis.call('LLEN', KEYS[1])}
"""

# KEYS: buffer list, flush marker, stream for the flush's priority, delayed zset
# ARGV: item, max items, immediate flush payload, delayed flush payload, flush due ts, marker ttl
# Appends an item, then makes sure a flush is coming: immediately each time the
# buffer fills a batch, otherwise once per window via the marker.
_BATCH_APPEND = """
local length = redis.call('RPUSH', KEYS[1], ARGV[1])
if length % tonumber(ARGV[2]) == 0 then
    redis.call('XADD', KEYS[3], '*', 'data', ARGV[3])
    return 2
end
if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[6]) then
    redis.call('ZADD', KEYS[4], ARGV[5], ARGV[4])
    return 1
end
return 0
"""


def _run(redis: Redis, source: str, keys: list, args: list):
    return redis.register_script(source)(keys=keys, args=args)


def cas_set(redis: Redis, key: str, expected_version: int, new_value: dict, ttl_seconds: Union[int, None] = None) -> bool:
    '''
        Atomically replace key's JSON document with new_value, but only if
        the document currently stored still has `_v == expected_version`.
        Returns False if the version didn't match or the key doesn't exist.
    '''
    result = _run(redis, _CAS_SET, [key], [str(expected_version), json.dumps(new_value), str(ttl_seconds) if ttl_seconds else ''])
    return bool(result)


def del_if_equals(redis: Redis, key: str, value: str) -> bool:
    return bool(_run(redis, _DEL_IF_EQUALS, [key], [value]))


def expire_if_equals(redis: Redis, key: str, value: str, ttl_seconds: int) -> bool:
    return bool(_run(redis, _EXPIRE_IF_EQUALS, [key], [value, str(ttl_seconds)]))


def concurrency_acquire(
    redis: Redis,
    holders_key: str,
    waiting_key: str,
    waiting_set_key: str,
    gate_name: str,
    holder: str,
    limit: int,
    lease_seconds: int,
    now: float,
    park_payload: Union[str, None] = None,
) -> bool:
    result = _run(
        redis, _CONCURRENCY_ACQUIRE,
        [holders_key, waiting_key, waiting_set_key],
        [holder, str(limit), str(now + lease_seconds), str(now), park_payload or '', gate_name],
    )
    return bool(result)


def concurrency_pump(
    redis: Redis,
    holders_key: str,
    waiting_key: str,
    waiting_set_key: str,
    stream_keys: list[str],
    gate_name: str,
    release_holder: Union[str, None],
    lease_seconds: int,
    now: float,
) -> int:
    return int(_run(
        redis, _CONCURRENCY_PUMP,
        [holders_key, waiting_key, waiting_set_key, *stream_keys],
        [release_holder or '', str(now), str(now + lease_seconds), gate_name],
    ))


def batch_take(redis: Redis, buffer_key: str, inflight_key: str, marker_key: str, max_items: int) -> tuple[list[Any], int]:
    items, remaining = _run(redis, _BATCH_TAKE, [buffer_key, inflight_key, marker_key], [str(max_items)])
    return [json.loads(i) for i in items], int(remaining)


def batch_append(
    redis: Redis,
    buffer_key: str,
    marker_key: str,
    stream_key: str,
    delayed_key: str,
    item: dict,
    max_items: int,
    immediate_flush: dict,
    delayed_flush: dict,
    flush_due_ts: float,
    marker_ttl_seconds: int,
) -> int:
    return int(_run(
        redis, _BATCH_APPEND,
        [buffer_key, marker_key, stream_key, delayed_key],
        [json.dumps(item), str(max_items), json.dumps(immediate_flush), json.dumps(delayed_flush), str(flush_due_ts), str(marker_ttl_seconds)],
    ))
