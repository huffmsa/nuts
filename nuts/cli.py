import argparse
import importlib
import json
import logging
import os
import pkgutil
import signal
import sys
from types import ModuleType
from typing import Callable, Optional

from redis import Redis

from . import dispatch


def _redis(url: Optional[str]) -> Redis:
    return Redis.from_url(url or os.environ.get('REDIS_URL', 'redis://localhost:6379'))


def load_job_modules(paths: list) -> list:
    '''
        Each path is a module with a Job class, or a package whose submodules
        (searched recursively) are checked for one.
    '''
    found: dict = {}
    for path in paths:
        module = importlib.import_module(path)
        candidates = [module]
        if hasattr(module, '__path__'):
            candidates += [
                importlib.import_module(info.name)
                for info in pkgutil.walk_packages(module.__path__, prefix=f'{module.__name__}.')
            ]
        for candidate in candidates:
            if isinstance(getattr(candidate, 'Job', None), type):
                found[candidate.__name__] = candidate
    if not found:
        raise SystemExit(f'No module with a Job class found in: {", ".join(paths)}')
    return list(found.values())


def _load_callable(spec: str) -> Callable:
    module_name, _, attr = spec.partition(':')
    if not attr:
        raise SystemExit(f'--setup must look like module:function, got {spec!r}')
    module: ModuleType = importlib.import_module(module_name)
    return getattr(module, attr)


def run_worker(args) -> None:
    from .worker import Worker

    options = dict(_load_callable(args.setup)()) if args.setup else {}
    if args.max_concurrent_jobs is not None:
        options['max_concurrent_jobs'] = args.max_concurrent_jobs
    redis = options.pop('redis', None) or _redis(args.redis_url)

    worker = Worker(redis, load_job_modules(args.jobs), workflow_directory=args.workflows, **options)
    signal.signal(signal.SIGTERM, worker.shutdown)
    signal.signal(signal.SIGINT, worker.shutdown)
    logging.getLogger('nuts').info(f'Worker {worker.id} starting with {len(worker.registry.definitions())} jobs')
    worker.serve()


def run_web(args) -> None:
    try:
        import uvicorn
        from .web import create_app
    except ImportError:
        raise SystemExit("The admin UI needs the web extra: pip install 'nuts-scheduler[web]'")
    uvicorn.run(create_app(_redis(args.redis_url)), host=args.host, port=args.port, log_level=args.log_level.lower())


def run_publish(args) -> None:
    from .queue import WorkQueue

    try:
        params = json.loads(args.params) if args.params else None
    except ValueError as ex:
        raise SystemExit(f'params is not valid JSON: {ex}')
    print(WorkQueue(_redis(args.redis_url)).publish(
        args.job_name, params, priority=args.priority, delay_seconds=args.delay,
    ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='nuts', description='Run NUTS workers, the admin UI, or publish a job.')
    parser.add_argument('--redis-url', help='Defaults to $REDIS_URL, then redis://localhost:6379')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    commands = parser.add_subparsers(dest='command', required=True)

    worker = commands.add_parser('worker', help='Run a worker until SIGTERM or SIGINT')
    worker.add_argument('jobs', nargs='+', help='Modules with a Job class, or packages containing them')
    worker.add_argument('--workflows', help='Directory of workflow YAML templates')
    worker.add_argument('--max-concurrent-jobs', type=int)
    worker.add_argument('--setup', metavar='MODULE:FUNCTION',
                        help='Called once at startup. Returns a dict of Worker options and job kwargs, '
                             'e.g. database clients, on_job_finished, or a redis client.')
    worker.set_defaults(handler=run_worker)

    web = commands.add_parser('web', help='Serve the admin UI')
    web.add_argument('--host', default='127.0.0.1')
    web.add_argument('--port', type=int, default=8000)
    web.set_defaults(handler=run_web)

    publish = commands.add_parser('publish', help='Queue one job and print its dispatch id')
    publish.add_argument('job_name')
    publish.add_argument('params', nargs='?', help='JSON object passed to the job')
    publish.add_argument('--priority', default='normal', choices=dispatch.PRIORITIES)
    publish.add_argument('--delay', type=float, default=0, help='Seconds to wait before it can run')
    publish.set_defaults(handler=run_publish)
    return parser


def main(argv: Optional[list] = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # Like uvicorn: modules named on the command line resolve from the current directory.
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    args.handler(args)


if __name__ == '__main__':
    main()
