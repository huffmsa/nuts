import datetime
import json
import os
from typing import Any, Optional, Sequence
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from redis import Redis

from .. import admin
from .. import dispatch

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _ago(ts: Any) -> str:
    if ts is None or ts == '':
        return ''
    if isinstance(ts, str):
        ts = datetime.datetime.fromisoformat(ts).timestamp()
    delta = ts - datetime.datetime.now(datetime.timezone.utc).timestamp()
    seconds = abs(delta)
    if seconds < 60:
        text = f'{seconds:.0f}s'
    elif seconds < 3600:
        text = f'{seconds / 60:.0f}m'
    elif seconds < 86400:
        text = f'{seconds / 3600:.1f}h'
    else:
        text = f'{seconds / 86400:.1f}d'
    return f'in {text}' if delta > 0 else f'{text} ago'


def _iso(ts: Any) -> str:
    if ts is None or ts == '':
        return ''
    if isinstance(ts, str):
        return ts
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat(timespec='seconds')


def _entry_time(entry_id: str) -> float:
    '''Stream entry ids start with the millisecond timestamp they were added at.'''
    return int(str(entry_id).split('-')[0]) / 1000


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _compact(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters['ago'] = _ago
    templates.env.filters['iso'] = _iso
    templates.env.filters['pretty'] = _pretty
    templates.env.filters['entry_time'] = _entry_time
    templates.env.filters['compact'] = _compact
    return templates


def _parse_json(text: str) -> Any:
    text = (text or '').strip()
    return json.loads(text) if text else None


def create_app(redis: Optional[Redis] = None, dependencies: Optional[Sequence[Depends]] = None, title: str = 'NUTS') -> FastAPI:
    '''
        Build the admin app. Without a client it connects to REDIS_URL
        (default redis://localhost:6379). dependencies are applied to every
        route, which is the place to add authentication.
    '''
    app = FastAPI(title=title, dependencies=list(dependencies or []), docs_url=None, redoc_url=None, openapi_url=None)
    templates = _templates()
    client = redis
    nav = [
        ('overview', 'Overview'),
        ('jobs', 'Jobs'),
        ('history', 'History'),
        ('dead_letters', 'Dead letters'),
        ('workflows', 'Workflows'),
        ('concurrency', 'Concurrency'),
    ]

    def r() -> Redis:
        nonlocal client
        if client is None:
            client = Redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'))
        return client

    def render(request: Request, template: str, active: str, **context) -> HTMLResponse:
        refresh = request.query_params.get('refresh', context.pop('default_refresh', '0'))
        return templates.TemplateResponse(request, template, {
            'title': title,
            'nav': nav,
            'active': active,
            'msg': request.query_params.get('msg'),
            'error': request.query_params.get('error'),
            'refresh': int(refresh) if str(refresh).isdigit() else 0,
            'priorities': dispatch.PRIORITIES,
            **context,
        })

    def back(request: Request, route: str, msg: Optional[str] = None, error: Optional[str] = None, **params) -> RedirectResponse:
        url = str(request.url_for(route, **params))
        query = {k: v for k, v in (('msg', msg), ('error', error)) if v}
        return RedirectResponse(f'{url}?{urlencode(query)}' if query else url, status_code=303)

    @app.get('/health')
    def health():
        try:
            r().ping()
            return {'status': 'ok'}
        except Exception as ex:
            return JSONResponse({'status': 'error', 'error': str(ex)}, status_code=503)

    @app.get('/', response_class=HTMLResponse, name='overview')
    def overview(request: Request):
        return render(request, 'overview.html', 'overview', data=admin.overview(r()), default_refresh='10')

    @app.get('/jobs', response_class=HTMLResponse, name='jobs')
    def jobs(request: Request):
        client = r()
        return render(
            request, 'jobs.html', 'jobs',
            queued={p: admin.queued(client, p) for p in dispatch.PRIORITIES},
            in_flight=admin.in_flight(client),
            parked=admin.parked(client),
            delayed=admin.delayed(client),
            retrying=admin.retrying(client),
            schedules=admin.schedules(client),
        )

    @app.post('/jobs/publish', name='publish')
    def publish(request: Request, job_name: str = Form(...), params: str = Form(''),
                priority: str = Form('normal'), delay_seconds: float = Form(0)):
        try:
            dispatch_id = admin.publish(r(), job_name.strip(), _parse_json(params), priority, delay_seconds)
        except (ValueError, TypeError) as ex:
            return back(request, 'jobs', error=f'Not published: {ex}')
        return back(request, 'jobs', msg=f'Published {job_name} as {dispatch_id}')

    @app.post('/jobs/{dispatch_id}/cancel', name='cancel_dispatch')
    def cancel_dispatch(request: Request, dispatch_id: str):
        admin.cancel(r(), dispatch_id)
        return back(request, 'jobs', msg=f'{dispatch_id} will be skipped when a worker reaches it')

    @app.post('/jobs/{dispatch_id}/run-now', name='run_now')
    def run_now(request: Request, dispatch_id: str):
        if admin.run_now(r(), dispatch_id):
            return back(request, 'jobs', msg=f'{dispatch_id} is due now')
        return back(request, 'jobs', error=f'{dispatch_id} is no longer waiting')

    @app.post('/jobs/{dispatch_id}/drop', name='drop_waiting')
    def drop_waiting(request: Request, dispatch_id: str):
        if admin.drop_waiting(r(), dispatch_id):
            return back(request, 'jobs', msg=f'Removed {dispatch_id}')
        return back(request, 'jobs', error=f'{dispatch_id} is no longer waiting')

    @app.post('/schedules/run-now', name='run_schedule_now')
    def run_schedule_now(request: Request, kind: str = Form(...), name: str = Form(...)):
        if admin.run_schedule_now(r(), kind, name):
            return back(request, 'jobs', msg=f'{name} is due now')
        return back(request, 'jobs', error=f'{name} is not scheduled')

    @app.get('/history', response_class=HTMLResponse, name='history')
    def history(request: Request, job: str = '', outcome: str = ''):
        rows = admin.history(r(), job_name=job or None, outcome=outcome or None)
        return render(request, 'history.html', 'history', rows=rows, job=job, outcome=outcome,
                      outcomes=['succeeded', 'retrying', 'dead', 'reclaimed', 'cancelled'])

    @app.get('/dead', response_class=HTMLResponse, name='dead_letters')
    def dead_letters(request: Request):
        return render(request, 'dead.html', 'dead_letters', rows=admin.dead_letters(r()))

    @app.post('/dead/{entry_id}/redrive', name='redrive')
    def redrive(request: Request, entry_id: str):
        try:
            dispatch_id = admin.redrive(r(), entry_id)
        except (KeyError, ValueError) as ex:
            return back(request, 'dead_letters', error=str(ex))
        return back(request, 'dead_letters', msg=f'Requeued as {dispatch_id}')

    @app.post('/dead/{entry_id}/delete', name='delete_dead_letter')
    def delete_dead_letter(request: Request, entry_id: str):
        admin.delete_dead_letter(r(), entry_id)
        return back(request, 'dead_letters', msg=f'Deleted {entry_id}')

    @app.post('/dead/purge', name='purge_dead_letters')
    def purge_dead_letters(request: Request):
        count = admin.purge_dead_letters(r())
        return back(request, 'dead_letters', msg=f'Deleted {count} dead letters')

    @app.get('/workflows', response_class=HTMLResponse, name='workflows')
    def workflows(request: Request, workflow: str = ''):
        client = r()
        return render(
            request, 'workflows.html', 'workflows',
            workflow=workflow,
            active_rows=admin.active_instances(client, workflow_name=workflow or None),
            finished_rows=admin.finished_instances(client),
            active_count=client.scard(admin.instance.ACTIVE_SET),
            schedules=admin.schedules(client)['workflows'],
        )

    @app.get('/workflows/find', name='find_instance')
    def find_instance(request: Request, q: str = ''):
        instance_id = admin.find_instance(r(), q.strip()) if q.strip() else None
        if instance_id is None:
            return back(request, 'workflows', error=f'No instance or active entity matches {q!r}')
        return back(request, 'instance', instance_id=instance_id)

    @app.get('/workflows/{instance_id}', response_class=HTMLResponse, name='instance')
    def instance_page(request: Request, instance_id: str):
        doc = admin.instance_detail(r(), instance_id)
        if doc is None:
            return back(request, 'workflows', error=f'Instance {instance_id} not found (it may have expired)')
        return render(request, 'instance.html', 'workflows', doc=doc)

    @app.post('/workflows/{instance_id}/cancel', name='cancel_instance')
    def cancel_instance(request: Request, instance_id: str, reason: str = Form('')):
        admin.cancel_instance(r(), instance_id, reason.strip() or 'cancelled from admin')
        return back(request, 'instance', msg='Cancelled', instance_id=instance_id)

    @app.post('/workflows/{instance_id}/signal', name='signal_instance')
    def signal_instance(request: Request, instance_id: str, event: str = Form(...), payload: str = Form('')):
        try:
            data = _parse_json(payload)
        except ValueError as ex:
            return back(request, 'instance', error=f'Payload is not valid JSON: {ex}', instance_id=instance_id)
        admin.signal_instance(r(), instance_id, event.strip(), data)
        return back(request, 'instance', msg=f'Sent {event}', instance_id=instance_id)

    @app.get('/concurrency', response_class=HTMLResponse, name='concurrency')
    def concurrency(request: Request):
        return render(request, 'concurrency.html', 'concurrency', rows=admin.concurrency(r()), default_refresh='5')

    return app


app = create_app()
