"""
Server-rendered admin UI for NUTS.

    pip install nuts-scheduler[web]
    REDIS_URL=redis://... uvicorn nuts.web:app

Or build it around an existing client and mount it in another app:

    from nuts.web import create_app
    parent.mount('/nuts', create_app(redis))

The UI has no authentication of its own. Pass FastAPI dependencies to
create_app() to add some, or keep it on an internal network.
"""
from .app import create_app, app

__all__ = ['create_app', 'app']
