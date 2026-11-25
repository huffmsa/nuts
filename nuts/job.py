from typing import Union, Any, Protocol


class NutsJob():
    '''
        A Nuts Job
    '''
    name: str
    schedule: Union[str, None]
    success: bool
    result: Any
    error: Exception
    next: Union[str, None]
    max_attempts: int = 5
    timeout_seconds: Union[float, None] = None
    failed_items: dict

    def __init__(self, **kwargs):
        '''
            Initialize the NutsJob class.

            Job subclasses must keep __init__ side-effect-free (do real work in
            run()) -- the registry probes a throwaway instance at registration
            time to learn name/schedule/max_attempts, and constructs a fresh
            instance for every single execution.
        '''
        self.name = kwargs.get('name', None)
        self.schedule = kwargs.get('schedule', None)
        self.success = kwargs.get('success', False)
        self.result = kwargs.get('result', None)
        self.error = kwargs.get('error', None)
        self.next = kwargs.get('next', None)
        self.max_attempts = kwargs.get('max_attempts', self.max_attempts)
        # After this long the worker stops renewing its claim, so another
        # worker may pick the job up again. None means no limit.
        self.timeout_seconds = kwargs.get('timeout_seconds', self.timeout_seconds)
        # Batch jobs only: {index into items: error} for items that failed
        # while the batch as a whole succeeded. Those items are retried alone.
        self.failed_items = {}


class JobModule(Protocol):
    """Protocol for job modules that contain a Job class."""
    Job: type[NutsJob]


class JobDefinition:
    '''
        Metadata probed from a registered job class: name, schedule,
        max_attempts and timeout. Does not hold any execution state.
    '''
    name: str
    schedule: Union[str, None]
    max_attempts: int
    timeout_seconds: Union[float, None]
    job_class: type[NutsJob]

    def __init__(self, job_class: type[NutsJob]):
        probe = job_class()
        self.name = probe.name
        self.schedule = probe.schedule
        self.max_attempts = probe.max_attempts
        self.timeout_seconds = getattr(probe, 'timeout_seconds', None)
        self.job_class = job_class


class JobRegistry:
    '''
        Separates job *definition* (name/schedule/max_attempts, probed once)
        from job *execution* (always a fresh instance). This is what prevents
        success/result/error from one run leaking into the next.
    '''
    def __init__(self, jobs: list[JobModule]):
        self._definitions: dict[str, JobDefinition] = {}
        for job_module in jobs:
            definition = JobDefinition(job_module.Job)
            self._definitions[definition.name] = definition

    def new(self, name: str) -> NutsJob:
        '''Construct a fresh instance of the job class registered under name.'''
        return self._definitions[name].job_class()

    def get(self, name: str) -> Union[JobDefinition, None]:
        return self._definitions.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._definitions

    def definitions(self) -> list[JobDefinition]:
        return list(self._definitions.values())
