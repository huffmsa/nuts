from nuts.concurrency import ConcurrencyGate


def test_acquire_respects_limit(redis):
    gate = ConcurrencyGate(redis)

    assert gate.acquire('vendor-1', 'holder-a', limit=2, lease_seconds=60) is True
    assert gate.acquire('vendor-1', 'holder-b', limit=2, lease_seconds=60) is True
    assert gate.acquire('vendor-1', 'holder-c', limit=2, lease_seconds=60) is False


def test_release_frees_a_slot(redis):
    gate = ConcurrencyGate(redis)

    gate.acquire('vendor-1', 'holder-a', limit=1, lease_seconds=60)
    assert gate.acquire('vendor-1', 'holder-b', limit=1, lease_seconds=60) is False

    gate.release('vendor-1', 'holder-a')

    assert gate.acquire('vendor-1', 'holder-b', limit=1, lease_seconds=60) is True


def test_expired_lease_is_evicted_automatically(redis):
    gate = ConcurrencyGate(redis)

    # A lease that's already in the past (negative lease_seconds) should
    # not count against the limit on the next acquire.
    gate.acquire('vendor-1', 'stale-holder', limit=1, lease_seconds=-10)

    assert gate.acquire('vendor-1', 'fresh-holder', limit=1, lease_seconds=60) is True


def test_different_keys_are_independent(redis):
    gate = ConcurrencyGate(redis)

    assert gate.acquire('vendor-1', 'a', limit=1, lease_seconds=60) is True
    assert gate.acquire('vendor-2', 'b', limit=1, lease_seconds=60) is True
