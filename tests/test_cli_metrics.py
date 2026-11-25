import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from nuts import cli, dispatch, metrics
from nuts.job import NutsJob
from nuts.queue import WorkQueue
from nuts.worker import Worker


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
def job_package(tmp_path, monkeypatch):
    pkg = tmp_path / 'myjobs'
    (pkg / 'partner').mkdir(parents=True)
    (pkg / '__init__.py').write_text('')
    (pkg / 'helpers.py').write_text('VALUE = 1\n')
    (pkg / 'partner' / '__init__.py').write_text('')
    for module, name in [('ping', 'Ping'), ('partner/refresh', 'Refresh')]:
        (pkg / f'{module}.py').write_text(textwrap.dedent(f'''
            from nuts import NutsJob

            class Job(NutsJob):
                def __init__(self):
                    super().__init__()
                    self.name = '{name}'

                def run(self, **kwargs):
                    self.result = kwargs.get('greeting')
                    self.success = True
        '''))
    (tmp_path / 'setup_mod.py').write_text('def configure():\n    return {"greeting": "hi", "history_length": 50}\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def test_load_job_modules_walks_packages(job_package):
    names = sorted(m.Job().name for m in cli.load_job_modules(['myjobs']))
    assert names == ['Ping', 'Refresh']
    assert [m.Job().name for m in cli.load_job_modules(['myjobs.ping'])] == ['Ping']
    with pytest.raises(SystemExit):
        cli.load_job_modules(['myjobs.helpers'])


def test_publish_command_prints_dispatch_id(redis, capsys):
    url = os.environ['NUTS_TEST_REDIS_URL']
    cli.main(['--redis-url', url, 'publish', 'Ping', '{"order": 1}', '--priority', 'high'])
    dispatch_id = capsys.readouterr().out.strip()
    fields = redis.xrange(dispatch.stream_key('high'))[0][1]
    payload = json.loads(fields[b'data'])
    assert payload['dispatch_id'] == dispatch_id and payload['params'] == {'order': 1}
    with pytest.raises(SystemExit):
        cli.main(['--redis-url', url, 'publish', 'Ping', '{nope'])


def test_worker_command_runs_jobs_and_stops_on_sigterm(redis, job_package):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(job_package), os.getcwd()]))
    proc = subprocess.Popen(
        [sys.executable, '-m', 'nuts', '--redis-url', os.environ['NUTS_TEST_REDIS_URL'],
         'worker', 'myjobs', '--setup', 'setup_mod:configure', '--max-concurrent-jobs', '2'],
        cwd=job_package, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        WorkQueue(redis).publish('Refresh', {'order': 1})
        deadline = time.time() + 15
        while time.time() < deadline and not redis.xlen(dispatch.HISTORY_STREAM):
            time.sleep(0.1)
        entry = json.loads(redis.xrange(dispatch.HISTORY_STREAM)[0][1][b'data'])
        assert entry['job_name'] == 'Refresh' and entry['outcome'] == 'succeeded'
    finally:
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=15)[0].decode()
    assert proc.returncode == 0, output
    assert 'starting with 2 jobs' in output


def test_on_job_finished_sees_every_outcome(redis):
    events = []

    def hook(event):
        events.append(event)
        raise ValueError('a broken hook must not break the worker')

    worker = Worker(redis, [make_job('Ok', succeed), make_job('Boom', explode)], on_job_finished=hook, history_length=0)
    q = WorkQueue(redis)
    q.publish('Ok', {})
    q.publish('Boom', {}, max_attempts=1)
    worker.run()
    worker.run()
    assert [(e['job_name'], e['outcome']) for e in events] == [('Ok', 'succeeded'), ('Boom', 'dead')]
    assert events[0]['duration_seconds'] is not None
    assert events[1]['error'] == 'partner API said no'
    assert redis.xlen(dispatch.HISTORY_STREAM) == 0


def test_metrics_render(redis):
    worker = Worker(redis, [make_job('Ok', succeed)])
    worker.check_leader()
    q = WorkQueue(redis)
    q.publish('Ok', {}, priority='high')
    q.publish('Ok', {}, delay_seconds=60)
    worker.concurrency.acquire('ver"izon', 'holder-1', limit=1, lease_seconds=60)
    text = metrics.render(redis)
    assert 'nuts_queued{priority="high"} 1' in text
    assert 'nuts_delayed 1' in text
    assert 'nuts_leader 1' in text
    assert 'nuts_concurrency_holders{key="ver\\"izon"} 1' in text
    assert '# TYPE nuts_dead_letters gauge' in text


def test_metrics_endpoint(redis):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from nuts.web import create_app
    response = TestClient(create_app(redis)).get('/metrics')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/plain; version=0.0.4')
    assert 'nuts_queued{priority="normal"} 0' in response.text
