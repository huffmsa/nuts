from ....nuts.job import NutsJob


class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'ScheduledJob'
        self.schedule = '0 * * * * ? *'

    def run(self, **kwargs):
        base = kwargs.get('base')

        self.result = base + 1
        self.success = True

        print("KWARGS", kwargs)
