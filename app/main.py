"""Main FastAPI application for KnowCode backend."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import FRONTEND_URL
from app.database import create_db_and_tables
from app.routers import runs, teams

# Configure logging at INFO level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager that initializes database tables on startup."""
    logger.info("Initializing database tables...")
    create_db_and_tables()
    logger.info("Database tables initialized.")

    # Startup recovery: mark any pass1_running or pass2_running runs as interrupted
    from app.database import engine
    from sqlmodel import Session, select
    from app.models import Run, RunStatus

    with Session(engine) as session:
        interrupted_runs = session.exec(
            select(Run).where(Run.status.in_([RunStatus.PASS1_RUNNING, RunStatus.PASS2_RUNNING]))
        ).all()

        if interrupted_runs:
            logger.warning("Found %d runs in pass1_running or pass2_running state, marking as interrupted", len(interrupted_runs))
            for run in interrupted_runs:
                prior_status = run.status
                run.status = RunStatus.INTERRUPTED
                if prior_status == RunStatus.PASS1_RUNNING:
                    run.error = "Server restarted during Pass-1"
                else:
                    run.error = "Server restarted during Pass-2"
                session.add(run)
            session.commit()
            logger.info("Marked %d runs as interrupted", len(interrupted_runs))

    yield


app = FastAPI(
    title="KnowCode Shortlister Backend",
    version="0.1.0",
    lifespan=lifespan
)


# Global exception handler: dual error shape (detail + message)
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Return both 'detail' (for existing tests) and 'message' (for frontend)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "message": exc.detail,
        },
        headers=exc.headers,
    )


# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create the existing API router (for /api)
api_router = APIRouter()
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(teams.router, tags=["teams"])

# Mount the existing API router at /api (unchanged)
app.include_router(api_router, prefix="/api")

# Create and mount the new frontend router at /api/v1
from app.routers import frontend
frontend_router = APIRouter()
frontend_router.include_router(frontend.router, tags=["frontend"])
app.include_router(frontend_router, prefix="/api/v1")


@app.get("/")
def root():
    """Root health check endpoint."""
    return {"status": "ok", "service": "knowcode-backend"}
