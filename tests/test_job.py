from nuts.job import NutsJob, JobRegistry


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


def test_job_registry_builds_fresh_instances():
    """
    Regression test for the shared-mutable-instance bug: Worker used to
    construct each Job once and reuse it for every execution, so success/
    result/error from a previous run could leak into the next if a run
    threw before setting them. JobRegistry.new() must always hand back an
    independent instance.
    """
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'LeakCheck'
                self.schedule = '0 * * * * ? *'

            def run(self, **kwargs):
                pass

    registry = JobRegistry([Mod])

    first = registry.new('LeakCheck')
    first.success = True
    first.result = 'leaked?'
    first.error = RuntimeError('leaked?')

    second = registry.new('LeakCheck')

    assert second is not first
    assert second.success is False
    assert second.result is None
    assert second.error is None


def test_job_registry_probes_metadata_once():
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Metadata'
                self.schedule = '0 * * * * ? *'
                self.max_attempts = 7

            def run(self, **kwargs):
                pass

    registry = JobRegistry([Mod])
    definition = registry.get('Metadata')

    assert definition.name == 'Metadata'
    assert definition.schedule == '0 * * * * ? *'
    assert definition.max_attempts == 7
    assert 'Metadata' in registry
    assert registry.get('NoSuchJob') is None
