from ....nuts.job import NutsJob


class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'ScheduledJob'
        self.schedule = '0 * * * * ? *'

    def run(self, args):
        base = args.get('base')

        self.result = base + 1
        self.success = True
