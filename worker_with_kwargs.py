from nuts.worker import Worker
from nuts.job import NutsJob
from redis import Redis
r = Redis()


def middleware_func():
    return 'I am middleware'


class KwargsJob(NutsJob):
    def __init__(self):
        super().__init__()

        self.name = 'KwargsJob'
        self.schedule = '*/2 * * * * ? *'

    def run(self, job_parameters, **kwargs):
        print(kwargs.get('middleware')())

        self.success = True
        return


if __name__ == '__main__':
    w = Worker(redis=r, jobs=[KwargsJob], middleware=middleware_func)

    it = 0
    while it < 2:
        it += 1
        w.run()
