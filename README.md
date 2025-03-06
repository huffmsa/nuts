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

while True:
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
