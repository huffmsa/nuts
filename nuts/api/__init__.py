"""
NUTS API - FastAPI service for monitoring and controlling jobs and workflows.

Usage:
    uvicorn nuts.api.main:app --reload

Requires the 'api' optional dependency:
    pip install nuts-scheduler[api]
"""

from .main import app, create_app

__all__ = ["app", "create_app"]
