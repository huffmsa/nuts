import os
import pytest
from redis import Redis


@pytest.fixture
def redis():
    '''A real Redis connection, flushed before and after each test.'''
    url = os.environ.get('NUTS_TEST_REDIS_URL')
    if not url:
        pytest.fail('Set NUTS_TEST_REDIS_URL to a throwaway Redis. These tests run FLUSHALL.')
    r = Redis.from_url(url)
    r.flushall()
    yield r
    r.flushall()
