<p align="center">
    <img src="https://raw.githubusercontent.com/huffmsa/nuts/refs/heads/master/nuts_logox200.jpeg">
</p>

# Not Unother Task Scheduler

It's another Redis backed task scheduler. Supports queueable and cronable jobs and light DAG implementation for when you need to chain jobs together.


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
- **State Persistence**: Worker failures don't lose workflow progress (state saved to Redis)

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

#### Workflow vs DAG Jobs

NUTS supports two ways to chain jobs:

1. **Workflows (YAML)**: Best for complex pipelines, scheduled operations, and when you need visual workflow definitions
2. **DAG Jobs (job.next)**: Best for simple linear chains and dynamic job chaining based on results

Workflows are recommended for most use cases as they provide better visibility, validation, and error handling.
