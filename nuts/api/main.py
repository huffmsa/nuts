"""FastAPI application for NUTS job monitoring and control."""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis

from .routes import jobs, workflows


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - setup and teardown Redis connection."""
    # Startup: create Redis connection
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    app.state.redis = Redis.from_url(redis_url, decode_responses=False)

    # Test connection
    try:
        app.state.redis.ping()
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Redis at {redis_url}: {e}")

    yield

    # Shutdown: close Redis connection
    app.state.redis.close()


def create_app(
    redis: Optional[Redis] = None,
    cors_origins: Optional[list[str]] = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        redis: Optional Redis client to use. If not provided, will connect
               using REDIS_URL environment variable.
        cors_origins: List of allowed CORS origins. Defaults to allowing all.

    Returns:
        Configured FastAPI application.
    """
    # Use custom lifespan only if no Redis client provided
    app = FastAPI(
        title="NUTS API",
        description="API for monitoring and controlling NUTS jobs and workflows",
        version="0.4.0",
        lifespan=None if redis else lifespan,
    )

    # If Redis client provided, attach it directly
    if redis:
        app.state.redis = redis

    # Configure CORS
    if cors_origins is None:
        cors_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(jobs.router)
    app.include_router(workflows.router)

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        try:
            app.state.redis.ping()
            return {"status": "healthy", "redis": "connected"}
        except Exception as e:
            return {"status": "unhealthy", "redis": str(e)}

    return app


# Default app instance for uvicorn
app = create_app()
