import pytest
from redis import Redis


@pytest.fixture
def redis():
    '''A real Redis connection, flushed before and after each test.'''
    r = Redis()
    r.flushall()
    yield r
    r.flushall()
