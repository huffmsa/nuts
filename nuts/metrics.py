"""
Point-in-time gauges in the Prometheus text format, read from Redis.

Per-job counters and durations aren't here: each worker sees only its own
jobs, so those come from Worker(on_job_finished=...) instead.
"""
from redis import Redis

from . import admin

CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


def _label(value) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _gauge(lines: list, name: str, help_text: str, samples: list) -> None:
    lines.append(f'# HELP {name} {help_text}')
    lines.append(f'# TYPE {name} gauge')
    for labels, value in samples:
        label_text = ','.join(f'{k}="{_label(v)}"' for k, v in labels.items())
        lines.append(f'{name}{{{label_text}}} {value}' if label_text else f'{name} {value}')


def render(redis: Redis) -> str:
    data = admin.overview(redis)
    keys = admin.concurrency(redis)
    lines: list = []
    _gauge(lines, 'nuts_queued', 'Entries waiting for a worker.',
           [({'priority': s['priority']}, s['queued']) for s in data['streams']])
    _gauge(lines, 'nuts_in_flight', 'Entries a worker has claimed and not finished.',
           [({'priority': s['priority']}, s['in_flight']) for s in data['streams']])
    _gauge(lines, 'nuts_delayed', 'Dispatches waiting for their scheduled time.', [({}, data['delayed'])])
    _gauge(lines, 'nuts_retrying', 'Dispatches waiting out retry backoff.', [({}, data['retrying'])])
    _gauge(lines, 'nuts_dead_letters', 'Entries in the dead letter stream.', [({}, data['dead'])])
    _gauge(lines, 'nuts_active_instances', 'Workflow instances that have not finished.', [({}, data['active_instances'])])
    _gauge(lines, 'nuts_workers', 'Workers seen in the last two minutes.', [({}, len(data['workers']))])
    _gauge(lines, 'nuts_leader', '1 if a worker holds leadership.', [({}, 1 if data['leader'] else 0)])
    _gauge(lines, 'nuts_concurrency_holders', 'Running jobs holding a slot for a concurrency key.',
           [({'key': k['key']}, len(k['holders'])) for k in keys])
    _gauge(lines, 'nuts_concurrency_waiting', 'Jobs parked waiting for a slot on a concurrency key.',
           [({'key': k['key']}, k['waiting']) for k in keys])
    return '\n'.join(lines) + '\n'
