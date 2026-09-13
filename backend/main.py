from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.adapter import MockAgentAdapter
from backend.api import router
from backend.context_store import ContextStore
from backend.coordinator import CoordinatorService
from backend.db import Database
from backend.errors import ApiError
from backend.events import EventHub
from backend.scheduler import Scheduler
from backend.settings import Settings
from backend.worker_manager import WorkerManager


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.db_path)
        await db.init()
        events = EventHub()
        context = ContextStore(settings)
        scheduler = Scheduler(db=db, events=events, settings=settings)
        workers = WorkerManager(
            db=db,
            events=events,
            context=context,
            scheduler=scheduler,
            settings=settings,
        )
        scheduler.set_starter(workers.start)
        coordinator = CoordinatorService(
            adapter=MockAgentAdapter(),
            db=db,
            context=context,
            workers=workers,
            scheduler=scheduler,
            events=events,
        )
        workers.bind_coordinator(coordinator)
        app.state.settings = settings
        app.state.db = db
        app.state.events = events
        app.state.context = context
        app.state.scheduler = scheduler
        app.state.workers = workers
        app.state.coordinator = coordinator
        app.state.adapter = coordinator.adapter
        try:
            yield
        finally:
            await workers.shutdown()
            await db.close()

    app = FastAPI(title="tini_projects", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": f"invalid request: {exc.errors()}"}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, ApiError):
            return JSONResponse({"error": exc.message}, status_code=exc.status_code)
        return JSONResponse({"error": "internal error"}, status_code=500)

    return app


app = create_app()
