<p align="center">
    <img src="https://raw.githubusercontent.com/huffmsa/nuts/refs/heads/master/nuts_logox200.jpeg">
</p>

# Not Unother Task Scheduler

It's another Redis backed task scheduler. Supports queueable and cronable jobs, a light DAG implementation for when you need to chain jobs together, and durable per-entity workflow instances for cases where the set of steps isn't the same every time.

Delivery is backed by Redis Streams: work is never lost (a crashed worker's claim is reclaimed and retried), failures retry with backoff before landing in a dead-letter queue, dispatches can carry a priority, and per-key concurrency limits keep you from overrunning a downstream vendor or org.


## Installation

Nuts is available as a published Python package for versions >=3.8.

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
from nuts import Worker
from .jobs import a_job, b_job
from redis import Redis

r = Redis()

worker = Worker(redis=r, jobs=[a_job, b_job])

while worker.should_run:
    worker.run()
```

You provide the list of jobs (discussed in the next section), a redis connection and you're off to the races.
If you are running multiple workers connected to the same worker, NUTS will automatically handle leadership assignment for scheduling management.

You may pass in an arbitrary `kwargs`, it is suggested to use these to provide your worker with any shared functionality that your jobs may need (database connections, etc).


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

queue.publish('MyFirstJob', {'job_args': [1, 2]})
```

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

A worker that dies mid-job doesn't lose the work either: Redis tracks the in-flight claim, and the leader periodically reclaims anything that's gone stale (`pel_min_idle_ms`, default 60s) and redelivers it. Because of this, `run()` implementations should be idempotent where practical -- a crash-reclaim can mean a job runs more than once, trading possible duplicate execution for never silently losing work.

#### Per-Key Concurrency Limits

`concurrency_key` + `concurrency_limit` cap how many in-flight items share a key at once -- e.g. "no more than 3 concurrent calls to vendor X regardless of how many jobs are queued for it." A dispatch that can't get a slot simply waits for one to free up rather than failing.

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

while worker.should_run:
    worker.run()
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

#### Workflow vs DAG Jobs vs Dynamic Instances

NUTS supports three ways to chain jobs:

1. **Workflows (YAML)**: Best for a fixed, recurring pipeline -- cron-triggered, same shape every run.
2. **Dynamic Instances (`create_instance`)**: Best when the step set or parameters vary per run -- per-entity pipelines, steps composed at runtime, or a step that needs to wait on an external event.
3. **DAG Jobs (`job.next`)**: Best for simple linear chains where you just want the next job to run with this job's result.
