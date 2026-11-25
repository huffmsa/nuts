<p align="center">
    <img src="https://raw.githubusercontent.com/huffmsa/nuts/refs/heads/master/nuts_logox200.jpeg">
</p>

# Not Unother Task Scheduler

It's another Redis backed task scheduler. Supports queueable and cronable jobs, a light DAG implementation for when you need to chain jobs together, and durable per-entity workflow instances for cases where the set of steps isn't the same every time.

Delivery is backed by Redis Streams: work is never lost (a crashed worker's claim is reclaimed and retried), failures retry with backoff before landing in a dead-letter queue, dispatches can carry a priority, and per-key concurrency limits keep you from overrunning a downstream vendor or org.


## Installation

Nuts is a published Python package. It needs Python 3.11+ and Redis 6.2+ (a single node, not Cluster).

```bash
pip install nuts-scheduler
```

## Worker Setup

The main functionality of NUTS is the Worker class. It accepts a list of jobs and manages their scheduling, logging and exectution.
An example project using NUTS might have the structure.

```
/
-- jobs/
    -- a_job.py
    -- b_job.py
main.py
```

An example `main.py` might look like

```python
import signal
from nuts import Worker
from .jobs import a_job, b_job
from redis import Redis

r = Redis()

worker = Worker(redis=r, jobs=[a_job, b_job], max_concurrent_jobs=8)

signal.signal(signal.SIGTERM, worker.shutdown)
worker.serve()
```

You provide the list of jobs (discussed in the next section), a redis connection and you're off to the races.
If you are running multiple workers connected to the same Redis, NUTS will automatically handle leadership assignment for scheduling management.

`serve()` runs until `shutdown()` is called, then waits for in-flight jobs before returning. It runs:

- the leader's duties (cron, delayed jobs, retries, workflow advancement, recovery) in their own thread, so a long job never stalls scheduling;
- jobs in a pool of `max_concurrent_jobs` threads (default 1), which suits IO-bound jobs such as vendor API calls;
- a heartbeat that renews the worker's claim on every job it holds, so slow jobs aren't mistaken for crashed ones.

When idle it blocks on Redis (`block_ms`, default 1000) instead of polling.

`worker.run()` is a single step (one leader tick, then at most one job) and returns immediately when there's nothing to do. It's handy in tests; use `serve()` in production.

You may pass in an arbitrary `kwargs`, it is suggested to use these to provide your worker with any shared functionality that your jobs may need (database connections, etc). With `max_concurrent_jobs` above 1, jobs run on several threads at once, so anything shared this way must be thread-safe.

Worker options:

| Option | Default | Meaning |
|---|---|---|
| `max_concurrent_jobs` | 1 | Jobs run at once by `serve()` |
| `block_ms` | 1000 | How long an idle `serve()` waits on Redis per read |
| `pel_min_idle_ms` | 60000 | How long a claim can go without a heartbeat before another worker takes it over |
| `heartbeat_interval_seconds` | `pel_min_idle_ms / 3` | How often claims and concurrency leases are renewed |
| `concurrency_lease_seconds` | 300 | How long a concurrency slot survives without a heartbeat (e.g. after a crash) |
| `leader_interval_seconds` | 1.0 | Pause between leader ticks under `serve()` |
| `leader_tick_budget_seconds` | 5.0 | Max time per tick spent advancing workflow instances |
| `full_sweep_interval_seconds` | 300 | How often the leader re-checks every active instance as a safety net |
| `instance_ttl_seconds` | 604800 | How long finished instances are kept |


### Jobs

NUTS workers run NutsJobs. You can create a simple job as

```python
from nuts import NutsJob

class Job(NutsJob):

    def __init__(self, args, **kwargs):
        super().__init__()
        self.name = 'MyFirstJob'  # Required
        self.schedule = ''  # A 7 position cron statement, optional

    def run(self, job_args, **kwargs):


        self.result = job_args[0] + job_args[1]
        self.success = True
        return
```

Your job files should all contain a class named Job which extends `NutsJob`. Name is a required property for the worker state management. A schedule is optional. When provided, the Worker will run the job on at the specified frequency. All jobs should implement a `run` method which takes your job arguements as parameters and optional `kwargs` which will be provided by the worker. These `kwargs` are suggested as a way to pass in common functions or data source connections from your worker to reduce the amount of initializations you need to do in code.

Setting the `result` attribute at the completion of your job is optional, but improves the default logging for better traces, and allows you to use the DAG functionality that NUTS implements.

Setting `success` on completion of your job is required.


### Dispatching Jobs

Use `WorkQueue` to dispatch a registered job by name from anywhere -- it doesn't need to be the same process running the `Worker`:

```python
from nuts import WorkQueue
from redis import Redis

queue = WorkQueue(Redis())

dispatch_id = queue.publish('MyFirstJob', {'job_args': [1, 2]})
```

`publish` returns a dispatch id and raises if Redis rejects the write, so a caller always knows whether the job was queued. Pass the id to `queue.cancel(dispatch_id)` to skip that one dispatch (and any retries of it) when a worker next picks it up. A job that's already running finishes.

`publish` accepts a few optional, keyword-only arguments:

```python
queue.publish(
    'MyFirstJob',
    {'job_args': [1, 2]},
    priority='high',             # 'high' | 'normal' (default) | 'low'
    delay_seconds=60,            # run no sooner than 60s from now
    concurrency_key='vendor:42', # see "Per-Key Concurrency Limits" below
    concurrency_limit=3,
    max_attempts=3,              # overrides the job's own default (see Retries below)
)
```

#### Priority

A worker checks the `high` stream before `normal`, and `normal` before `low`. This is a small number of discrete tiers rather than numeric priority with anti-starvation aging -- simple, and sufficient for "this matters more than that."

#### Retries, Backoff, and the Dead-Letter Queue

If a job raises (or sets `success = False`), it's retried with exponential backoff (jittered, capped) rather than dropped. Once a job exhausts its attempts it's moved to a dead-letter stream (`nuts|jobs|dlq`) with the error and full dispatch context attached, instead of disappearing silently.

Defaults are 5 attempts, a 5s base backoff, capped at 300s. Override globally via `Worker(..., retry_policy=RetryPolicy(base=..., cap=..., max_attempts=...))`, per job class via a `max_attempts` attribute on your `Job`, or per dispatch via `WorkQueue.publish(..., max_attempts=...)` -- resolved in that order (dispatch overrides job class overrides the global default).

A worker that dies mid-job doesn't lose the work either: Redis tracks the in-flight claim, and the leader reclaims anything whose worker has stopped heartbeating for `pel_min_idle_ms` (default 60s) and redelivers it. A live worker keeps renewing its claim however long the job runs. To cap that, set `timeout_seconds` on the job class: once a job passes it, the worker stops renewing and the job may be picked up again elsewhere (the original run isn't killed).

Because of this, `run()` implementations should be idempotent where practical -- a crash-reclaim can mean a job runs more than once, trading possible duplicate execution for never silently losing work.

Acked entries are deleted from the streams, so they don't grow without bound. The dead-letter stream is kept for inspection.

#### Per-Key Concurrency Limits

`concurrency_key` + `concurrency_limit` cap how many running jobs share a key at once -- e.g. "no more than 3 concurrent calls to vendor X regardless of how many jobs are queued for it." This applies to every kind of dispatch: immediate, delayed, retries, batch flushes and workflow steps.

A slot is taken when a worker starts the job, not when it's queued, and released when it finishes. A job that can't get a slot is parked in a first-in, first-out line for its key and handed the next slot that frees, so it neither fails nor loses its place to later arrivals. Slots held by a crashed worker expire after `concurrency_lease_seconds`; running jobs keep theirs renewed by the heartbeat.

#### Batch Jobs

Some downstream APIs take many items per call. `publish_to_batch` collects items and runs the job once per group:

```python
queue.publish_to_batch(
    'UpdateDevices',
    {'sim': 1234, 'plan': 'b'},
    batch_key='verizon:change_plan',  # items are grouped by job, priority and batch_key
    max_items=500,                     # run as soon as this many are waiting
    window_seconds=10,                 # or this long after the first item, whichever is first
    concurrency_key='verizon',
)
```

The job receives `items`, a list of the params each item was published with. To fail some items while the rest succeed, set `failed_items` to `{index: error}` and `success = True`; only those items are retried (alone, with backoff), and an item that runs out of attempts goes to the dead-letter stream by itself. If the job raises, the whole batch is retried with the same items.

```python
class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'UpdateDevices'

    def run(self, items, **kwargs):
        response = carrier.update([i['sim'] for i in items])
        self.failed_items = {n: err for n, err in response.errors.items()}
        self.success = True
```

A batch run is an ordinary dispatch, so priority, concurrency limits, retries, heartbeats and reclaim all apply to it.

### Chaining Jobs - DAG

NUTS supports a very basic directed acyclic graph style functionality. When defining your job, setting the `next` attribute of your class to the name of the next job you would like to run will tell the worker to enqueue that job with the data stored on your jobs `result` attribute as parameters. This is useful for breaking up functionality into logical components, or break up long processes into more controllable steps.

### Workflows

NUTS supports complex workflows (DAGs) through YAML configuration files. Workflows allow you to define multi-job pipelines with dependencies, scheduling, and automatic error handling.

#### Creating a Workflow

Create a YAML file defining your workflow:

```yaml
# data_pipeline.yaml
workflow:
  name: data-pipeline
  schedule: "0 0 2 ? * * *"  # Daily at 2 AM
  jobs:
    - name: ExtractData
      requires: null  # Root job - no dependencies
    - name: TransformData
      requires:
        - ExtractData  # Waits for ExtractData to complete
    - name: LoadData
      requires:
        - TransformData
    - name: SendNotification
      requires:
        - LoadData
```

#### Loading Workflows

Pass a directory containing workflow YAML files to the Worker:

```python
from nuts import Worker
from redis import Redis
from .jobs import extract_data, transform_data, load_data, send_notification

r = Redis()

worker = Worker(
    redis=r,
    jobs=[extract_data, transform_data, load_data, send_notification],
    workflow_directory='./workflows'  # Directory containing .yaml files
)

worker.serve()
```

#### Workflow Features

- **Dependency Management**: Jobs automatically wait for their dependencies to complete
- **Parallel Execution**: Jobs without dependencies can run in parallel
- **Error Handling**: If any job fails, the workflow stops and marks as failed
- **Automatic Rescheduling**: Workflows reschedule automatically based on their cron schedule
- **State Persistence**: Each cron occurrence runs as its own durable workflow instance (see below) -- progress lives in Redis continuously, not just on a clean shutdown, so a worker crash never loses it

#### Workflow Job Requirements

Jobs used in workflows must be registered with the Worker and follow the standard NutsJob pattern:

```python
from nuts import NutsJob

class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'ExtractData'  # Must match workflow YAML

    def run(self, **kwargs):
        # Job logic here
        self.result = {'data': 'extracted'}
        self.success = True
```

**Important**: Job names in the workflow YAML must exactly match the `name` attribute of your job classes.

#### Workflow Validation

Workflows are validated on load to catch common errors:

- Circular dependencies (job A requires B, B requires A)
- Missing job definitions (workflow references jobs not registered with Worker)
- Orphaned jobs (no path from root jobs to job)

Invalid workflows are logged and skipped.

#### Example: Data Pipeline

See the `examples/` directory for a complete data pipeline workflow implementation including:

- **ExtractData**: Fetches data from an API
- **TransformData**: Cleans and processes data
- **LoadData**: Loads data to warehouse
- **SendNotification**: Sends completion notification

This demonstrates a common ETL pattern with sequential dependencies.

### Dynamic Workflow Instances

A YAML workflow is a single named template, cron-triggered. Sometimes you don't know the steps until runtime, or you need many concurrent, independently-parameterized runs for different entities (per vendor, per org) rather than one shared singleton. For that, create instances directly:

```python
from nuts import create_instance

instance_id = create_instance(
    redis,
    'onboarding',
    plan=[
        {'step_id': 'verify', 'op': 'VerifyAccount', 'requires': None},
        {'step_id': 'vendor_ack', 'kind': 'wait', 'event': 'vendor_ack', 'requires': ['verify']},
        {'step_id': 'activate', 'op': 'ActivateAccount', 'requires': ['vendor_ack']},
    ],
    params={'vendor': 'acme-co'},
)
```

Each `create_instance` call gets its own plan, built from whatever subset and ordering of your registered ops this particular run needs -- another instance of the same workflow name can use a completely different plan. There's no separate "skip" mechanism: a step you don't want for this run is simply left out of the plan.

A step's `requires` entries are also how it receives upstream data: a step's `run(**kwargs)` is called with the instance's `params` merged with each step named in its own `requires` list, keyed by that step's `step_id`, holding that step's result.

#### Steps That Wait on an External Signal

Give a step `kind: 'wait'` and an `event` name instead of an `op`, and it pauses the instance until something external resolves it:

```python
from nuts import signal_instance

signal_instance(redis, instance_id, 'vendor_ack', {'approved': True})
```

This works regardless of whether the signal arrives before or after the instance's workflow actually reaches that step -- nothing is lost either way.

Add `timeout_seconds` to a wait step to give up if the signal doesn't arrive in time. The step fails with a "timed out" error, which fails the instance.

#### Instances Tied to an Entity

Pass `entity_key` to tie an instance to something outside NUTS, such as a device or an order. The code that produces signals then only needs to know the entity:

```python
from nuts import create_instance, signal_entity, cancel_entity

create_instance(redis, 'configure-sim', plan, params, entity_key='sim:1234', replace=True)

# later, from whatever sees the device come online
signal_entity(redis, 'sim:1234', 'device_online', {'seen_at': '...'})
```

Each entity has at most one active instance. Creating another raises `EntityBusyError`, unless `replace=True`, in which case the old one is cancelled and the new one takes its place. `cancel_instance(redis, instance_id)` and `cancel_entity(redis, entity_key)` stop an instance: steps not yet started never run, a queued step is skipped, and a running step finishes but nothing after it starts.

#### How Instances Advance

Anything that can move an instance forward (creating it, a step finishing, a signal, a wait timing out) marks it dirty, and the leader only processes dirty instances. Instances that are just waiting cost nothing per tick. As a safety net the leader also re-checks every active instance every `full_sweep_interval_seconds`; that pass reads in batches and only does work for instances that can actually move.

Delivery is at least once: if the leader dies between queuing a step and recording it, the step may be queued twice. Make steps idempotent.

#### Workflow vs DAG Jobs vs Dynamic Instances

NUTS supports three ways to chain jobs:

1. **Workflows (YAML)**: Best for a fixed, recurring pipeline -- cron-triggered, same shape every run.
2. **Dynamic Instances (`create_instance`)**: Best when the step set or parameters vary per run -- per-entity pipelines, steps composed at runtime, or a step that needs to wait on an external event.
3. **DAG Jobs (`job.next`)**: Best for simple linear chains where you just want the next job to run with this job's result.

## Admin UI

An optional, server-rendered admin UI ships with the package. It's plain HTML from FastAPI and Jinja templates, with no JavaScript build and nothing extra to deploy.

```bash
pip install nuts-scheduler[web]
REDIS_URL=redis://localhost:6379 uvicorn nuts.web:app --port 8000
```

Or mount it inside an app you already run:

```python
from nuts.web import create_app

app.mount('/nuts', create_app(redis))
```

Pages:

- **Overview**: queue depths per priority, delayed and retrying counts, dead letters, the current leader and live workers.
- **Jobs**: what's running, what's waiting for a concurrency slot, what's queued, delayed or waiting to retry, and cron schedules. Publish a job, cancel a dispatch, or make a delayed one run now.
- **History**: recent finished attempts with outcome, duration and error. Each worker keeps the last `history_length` (default 1000; 0 turns it off).
- **Dead letters**: redrive (queue again with a fresh attempt count) or delete.
- **Workflows**: active and recently finished instances, lookup by instance id or entity key, and an instance page with each step's status and result. Send a signal or cancel from there.
- **Concurrency**: each key's running holders and the length of its line.

Add `?refresh=10` to any page to reload it every 10 seconds.

The UI has no authentication of its own and its buttons change state. Keep it on an internal network, or pass FastAPI dependencies that check credentials:

```python
create_app(redis, dependencies=[Depends(require_admin)])
```

Everything the UI shows or does is also available as plain functions in `nuts.admin`, for scripts and shells:

```python
from nuts import admin

admin.overview(redis)
admin.redrive(redis, admin.dead_letters(redis)[0]['entry_id'])
```

## Development

The tests run `FLUSHALL`, so point them at a throwaway Redis.

```bash
pip install -e '.[dev]'
docker run -d --rm --name nuts-test-redis -p 6390:6379 redis:7
NUTS_TEST_REDIS_URL=redis://localhost:6390/0 pytest
```

`make build` and `make publish` build and upload to PyPI.
