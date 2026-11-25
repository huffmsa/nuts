import os
import pytest
from redis import Redis


@pytest.fixture
def redis():
    '''A real Redis connection, flushed before and after each test. Point NUTS_TEST_REDIS_URL at a throwaway instance.'''
    r = Redis.from_url(os.environ.get('NUTS_TEST_REDIS_URL', 'redis://localhost:6379/0'))
    r.flushall()
    yield r
    r.flushall()
