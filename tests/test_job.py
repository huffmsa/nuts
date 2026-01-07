from ..nuts.job import NutsJob


def test_job():
    class Job(NutsJob):
        def __init__(self):
            super().__init__()
            self.name = 'job'

        def run(self, args):
            self.result = args.get('base') + 1
            self.success = True

    job = Job()
    job.run({'base': 1})

    assert job.name == 'job'
    assert job.success is True
    assert job.result == 2


def test_job_failure():
    class FailJob(NutsJob):
        def __init__(self):
            super().__init__()
            self.name = 'fail_job'

        def run(self, **kwargs):
            try:
                # Simulate a failure condition
                raise ValueError("Intentional failure for testing")
            except Exception as e:
                self.success = False
                self.error = e

    job = FailJob()
    job.run()

    # Assertions for failure test
    assert job.name == 'fail_job'
    assert job.success is False
    assert job.error is not None
    assert isinstance(job.error, ValueError)
    assert 'Intentional failure' in str(job.error)
