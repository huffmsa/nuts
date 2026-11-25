import json
import time
import pytest

pytest.importorskip('fastapi')
pytest.importorskip('jinja2')
from fastapi.testclient import TestClient

from nuts.web import create_app
from nuts.worker import Worker, RetryPolicy
from nuts.job import NutsJob
from nuts.queue import WorkQueue
from nuts import dispatch
from nuts import admin
from nuts import instance as inst


def make_job(name, fn):
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = name

            def run(self, **kwargs):
                fn(self, **kwargs)
    return Mod


def succeed(job, **kwargs):
    job.success = True


def explode(job, **kwargs):
    raise RuntimeError('partner API said no')


@pytest.fixture
def seeded(redis):
    worker = Worker(redis, [make_job('Ok', succeed), make_job('Boom', explode)],
                    retry_policy=RetryPolicy(base=0.01, cap=0.01))
    q = WorkQueue(redis)
    q.publish('Ok', {'order': 1})
    q.publish('Boom', {'order': 2}, max_attempts=1)
    worker.run()
    worker.run()
    q.publish('Ok', {'order': 3}, priority='high')
    q.publish('Ok', {'order': 4}, delay_seconds=3600)
    instance_id = inst.create_instance(redis, 'fulfil-order', [
        {'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received', 'timeout_seconds': 3600},
        {'step_id': 'apply', 'op': 'Ok', 'requires': ['paid']},
    ], {'order': 5}, entity_key='order:5')
    worker.advance_dirty_instances()
    return {'worker': worker, 'instance_id': instance_id, 'client': TestClient(create_app(redis))}


def test_pages_render(seeded):
    client = seeded['client']
    for path, expected in [
        ('/', 'Leader'),
        ('/jobs', 'Queued: high (1)'),
        ('/history', 'partner API said no'),
        ('/dead', 'partner API said no'),
        ('/workflows', 'fulfil-order'),
        (f"/workflows/{seeded['instance_id']}", 'waiting_for:payment_received'),
        ('/concurrency', 'Concurrency'),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.text, path
    assert client.get('/health').json() == {'status': 'ok'}


def test_overview_counts(redis, seeded):
    data = admin.overview(redis)
    assert {s['priority']: s['queued'] for s in data['streams']} == {'high': 1, 'normal': 0, 'low': 0}
    assert data['delayed'] == 1
    assert data['dead'] == 1
    assert data['active_instances'] == 1
    assert data['leader'] == seeded['worker'].id


def test_queued_excludes_in_flight(redis, seeded):
    stream = dispatch.stream_key('high')
    redis.xreadgroup(admin.CONSUMER_GROUP, 'someone', {stream: '>'}, count=1)
    assert admin.queued(redis, 'high') == []
    held = admin.in_flight(redis)
    assert len(held) == 1 and held[0]['params'] == {'order': 3} and held[0]['worker_id'] == 'someone'


def test_publish_form_and_bad_json(redis, seeded):
    client = seeded['client']
    ok = client.post('/jobs/publish', data={'job_name': 'Ok', 'params': '{"order": 9}', 'priority': 'low', 'delay_seconds': '0'})
    assert 'Published Ok' in ok.text
    assert admin.queued(redis, 'low')[0]['params'] == {'order': 9}

    bad = client.post('/jobs/publish', data={'job_name': 'Ok', 'params': '{nope', 'priority': 'low', 'delay_seconds': '0'})
    assert 'Not published' in bad.text


def test_run_now_and_drop_delayed(redis, seeded):
    client = seeded['client']
    dispatch_id = admin.delayed(redis)[0]['dispatch_id']
    client.post(f'/jobs/{dispatch_id}/run-now')
    assert admin.delayed(redis)[0]['due_at'] <= time.time()
    client.post(f'/jobs/{dispatch_id}/drop')
    assert admin.delayed(redis) == []


def test_cancel_queued_dispatch(redis, seeded):
    client = seeded['client']
    dispatch_id = admin.queued(redis, 'high')[0]['dispatch_id']
    client.post(f'/jobs/{dispatch_id}/cancel')
    seeded['worker'].consume_one()
    assert admin.history(redis, limit=1)[0]['outcome'] == 'cancelled'


def test_redrive_dead_letter(redis, seeded):
    client = seeded['client']
    entry_id = admin.dead_letters(redis)[0]['entry_id']
    response = client.post(f'/dead/{entry_id}/redrive')
    assert 'Requeued' in response.text
    assert admin.dead_letters(redis) == []
    requeued = admin.queued(redis, 'normal')[0]
    assert requeued['job_name'] == 'Boom' and requeued['attempt'] == 1 and 'error' not in requeued


def test_redrive_batch_item(redis):
    def merge(job, items, **kwargs):
        job.failed_items = {0: 'rejected'} if len(calls) == 0 else {}
        calls.append([i['order'] for i in items])
        job.success = True

    calls = []
    worker = Worker(redis, [make_job('Merge', merge)])
    WorkQueue(redis).publish_to_batch('Merge', {'order': 1}, max_items=1, max_attempts=1)
    worker.run()
    assert admin.dead_letters(redis)[0]['kind'] == 'batch_item'

    admin.redrive(redis, admin.dead_letters(redis)[0]['entry_id'])
    worker.run()
    assert calls == [[1], [1]]


def test_find_signal_and_cancel_instance(redis, seeded):
    client = seeded['client']
    instance_id = seeded['instance_id']

    found = client.get('/workflows/find', params={'q': 'order:5'}, follow_redirects=False)
    assert found.headers['location'].endswith(f'/workflows/{instance_id}')
    missing = client.get('/workflows/find', params={'q': 'order:404'})
    assert 'No instance' in missing.text

    client.post(f'/workflows/{instance_id}/signal', data={'event': 'payment_received', 'payload': '{"at": 1}'})
    assert json.loads(redis.hget(inst.signal_key(instance_id), 'payment_received')) == {'at': 1}

    page = client.post(f'/workflows/{instance_id}/cancel', data={'reason': 'operator'})
    assert 'cancelled' in page.text
    assert inst.get_instance(redis, instance_id)['error'] == 'operator'
    assert 'fulfil-order' in client.get('/workflows').text


def test_concurrency_page_shows_holders_and_line(redis, seeded):
    worker = seeded['worker']
    worker.concurrency.acquire('partner-api', 'holder-1', limit=1, lease_seconds=60)
    worker.concurrency.acquire('partner-api', 'holder-2', limit=1, lease_seconds=60, park_payload=json.dumps({'dispatch_id': 'x'}))
    assert admin.parked(redis) == [{'dispatch_id': 'x', 'waiting_on': 'partner-api'}]
    rows = admin.concurrency(redis)
    assert rows == [{'key': 'partner-api', 'holders': [{'dispatch_id': 'holder-1', 'lease_seconds_left': pytest.approx(60, abs=1)}], 'waiting': 1}]
    assert 'partner-api' in seeded['client'].get('/concurrency').text


def test_mounted_under_a_prefix(redis, seeded):
    from fastapi import FastAPI
    parent = FastAPI()
    parent.mount('/nuts', create_app(redis))
    client = TestClient(parent)
    page = client.get('/nuts/jobs')
    assert page.status_code == 200
    assert 'href="http://testserver/nuts/history"' in page.text
    redirect = client.post('/nuts/dead/purge', follow_redirects=False)
    assert redirect.headers['location'].startswith('http://testserver/nuts/dead')
