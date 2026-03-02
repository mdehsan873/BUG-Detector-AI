from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.api.routes import analysis, auth, health, notifications, projects
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.utils.logger import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Buglyft agent...")
    scheduler = start_scheduler()
    app.state.scheduler = scheduler
    yield
    logger.info("Shutting down...")
    stop_scheduler(scheduler)


app = FastAPI(
    title="Buglyft API",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(health.router, tags=["health"])
app.include_router(projects.router, prefix="/projects", tags=["projects"])
app.include_router(analysis.router, prefix="/projects", tags=["analysis"])
app.include_router(notifications.router, prefix="/projects", tags=["notifications"])
