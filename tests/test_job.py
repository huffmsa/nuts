from ..nuts.job import NutsJob


def test_job():
    class Job(NutsJob):
        def __init__(self):
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
            self.name = 'fail_job'

        def run(self, args):
            self.success = False

