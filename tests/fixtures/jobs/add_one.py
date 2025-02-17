from ....nuts.job import NutsJob


class Job(NutsJob):
    def __init__(self):
        super().__init__()
        self.name = 'AddOne'

    def run(self, **kwargs):
        base = kwargs.get('base')
        self.result = base + 1
        self.success = True
