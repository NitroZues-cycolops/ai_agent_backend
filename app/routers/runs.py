"""Routes for managing runs and ingesting teams."""
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select, func

from app import ai_client
from app.database import get_session
from app.models import Run, Team, RunStatus, TeamStatus
from app.schemas import RunRead, IngestReport, Pass1Status, Pass1Progress, Pass1BandCounts, TeamRead
from app.ingest import ingest_rows, missing_columns
from app.sheets import get_sheet_rows

logger = logging.getLogger(__name__)

router = APIRouter()


def latest_run(session: Session) -> Run | None:
    """Return the most recently created run, or None if there are no runs."""
    statement = select(Run).order_by(Run.id.desc()).limit(1)
    return session.exec(statement).first()


def build_run_read(session: Session, run: Run) -> RunRead:
    """Build a RunRead response with computed team counts.

    Uses a single GROUP BY query to efficiently count teams by status.
    """
    # Count teams by status using a GROUP BY query
    statement = (
        select(Team.status, func.count(Team.id))
        .where(Team.run_id == run.id)
        .group_by(Team.status)
    )
    results = session.exec(statement).all()

    counts_by_status = {status: count for status, count in results}
    total_teams = sum(counts_by_status.values())

    return RunRead(
        id=run.id,
        status=run.status,
        created_at=run.created_at,
        frozen_at=run.frozen_at,
        shortlist_size=run.shortlist_size,
        error=run.error,
        total_teams=total_teams,
        counts_by_status=counts_by_status,
    )


@router.post("/runs", response_model=RunRead)
def create_run(session: Session = Depends(get_session)):
    """Create a new run.

    A run represents one full shortlisting attempt.
    """
    run = Run(status=RunStatus.CREATED)
    session.add(run)
    session.commit()
    session.refresh(run)

    return build_run_read(session, run)


@router.get("/runs/current", response_model=RunRead)
def get_current_run(session: Session = Depends(get_session)):
    """Get the most recent run, creating one if none exists.

    This route MUST be declared before GET /runs/{run_id} so it's not
    matched as a run_id parameter.
    """
    run = latest_run(session)
    if not run:
        # Create a new run if none exists
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

    return build_run_read(session, run)


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: int, session: Session = Depends(get_session)):
    """Get a specific run by ID."""
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return build_run_read(session, run)


@router.post("/runs/{run_id}/ingest", response_model=IngestReport)
def ingest_teams(run_id: int, session: Session = Depends(get_session)):
    """Ingest teams from the Google Sheet into this run.

    Steps:
    1. Read the sheet (fail with 502 if unreachable)
    2. No rows -> 400
    3. Missing required columns -> 400, saving nothing
    4. Lock the run (row lock with with_for_update())
    5. Validate run exists and status is created or ingested (404/409 otherwise)
    6. Parse and upsert teams
    7. Update run status to ingested
    8. Commit everything (all-or-nothing transaction)
    9. Return summary
    """
    # Step 1: Read the sheet FIRST
    try:
        rows = get_sheet_rows()
    except Exception as exc:
        logger.error("Failed to read Google Sheet: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Could not read the Google Sheet"
        ) from exc

    # Step 2: No rows -> 400
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="Sheet has no data rows"
        )

    # Step 3: Check for missing required columns
    missing = missing_columns(rows)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Sheet is missing required columns: {', '.join(missing)}"
        )

    # Step 4: Lock the run (row lock)
    # with_for_update() acquires a row-level lock (SELECT ... FOR UPDATE in SQL)
    # so that only one ingest can modify this run at a time.
    statement = select(Run).where(Run.id == run_id).with_for_update()
    run = session.exec(statement).first()

    # Step 5: Validate run exists and is in an ingestable status
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if run.status not in (RunStatus.CREATED, RunStatus.INGESTED, RunStatus.PASS1_INCOMPLETE):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot ingest: run is in status '{run.status}'"
        )

    # Step 6: Parse and upsert teams (does NOT commit)
    summary = ingest_rows(session, run_id, rows)

    # Safety net: if sheet has rows but no team_id could be read from any row
    if summary["teams_in_sheet"] == 0:
        raise HTTPException(
            status_code=400,
            detail="No team_id could be read from any row"
        )

    # Step 7: Update run status
    run.status = RunStatus.INGESTED

    # Step 8: Commit everything (all-or-nothing)
    session.commit()

    # Step 9: Return summary
    return IngestReport(
        run_id=run_id,
        status=run.status,
        **summary,
    )


@router.post("/runs/{run_id}/pass1")
def start_pass1(
    run_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
):
    """Start Pass 1 scoring for all ingested teams in this run.

    Steps:
    1. Check AI service is ready (503 if no LLM key)
    2. Lock the run row
    3. Validate run exists and status allows Pass 1 (404/409 otherwise)
    4. Set status to pass1_running and commit BEFORE scheduling background task
    5. Schedule the background task
    6. Return immediately with teams_to_score count
    """
    # Step 1: Check AI service is ready
    try:
        ai_client.check_ready()
    except ai_client.AIServiceError as exc:
        logger.error("AI service not ready: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=str(exc)
        ) from exc

    # Step 2: Lock the run row
    statement = select(Run).where(Run.id == run_id).with_for_update()
    run = session.exec(statement).first()

    # Step 3: Validate run exists and is in a pass1-able status
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    allowed_statuses = (RunStatus.INGESTED, RunStatus.PASS1_INCOMPLETE, RunStatus.INTERRUPTED)
    if run.status not in allowed_statuses:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start Pass 1: run is in status '{run.status}'"
        )

    # Count teams that will be scored
    teams_to_score_count = session.exec(
        select(func.count(Team.id))
        .where(
            Team.run_id == run_id,
            Team.status == TeamStatus.P1_QUEUED,
            Team.p1_composite == None
        )
    ).one()

    # Step 4: Set status to pass1_running and commit BEFORE scheduling background task
    run.status = RunStatus.PASS1_RUNNING
    run.error = None
    session.commit()

    # Step 5: Schedule the background task
    from app.pass1_runner import run_pass1
    background_tasks.add_task(run_pass1, run_id)

    # Step 6: Return immediately
    logger.info("Pass 1 started for run_id=%d, %d teams to score", run_id, teams_to_score_count)
    return {
        "run_id": run_id,
        "status": run.status,
        "teams_to_score": teams_to_score_count
    }


@router.get("/runs/{run_id}/pass1", response_model=Pass1Status)
def get_pass1_status(run_id: int, session: Session = Depends(get_session)):
    """Get Pass 1 scoring status and results for a run.

    Returns:
        - run_id, run_status
        - progress: total, scored, failed, pending counts
        - bands_final: True if bands are computed (no failures remain)
        - band_counts: counts per band (REJECT, BORDERLINE, FAST_TRACK)
        - teams: all teams with pass1 data, ordered by p1_composite desc
    """
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Get all teams for this run
    all_teams = session.exec(
        select(Team).where(Team.run_id == run_id)
    ).all()

    # Compute progress
    total = len([t for t in all_teams if t.status != TeamStatus.INCOMPLETE])
    scored = len([t for t in all_teams if t.p1_composite is not None])
    failed = len([t for t in all_teams if t.status == TeamStatus.P1_QUEUED and t.error is not None])
    pending = total - scored - failed

    # Bands are final only when all eligible teams are scored (no failures)
    bands_final = (failed == 0) and (pending == 0) and (scored > 0)

    # Count teams in each band
    reject_count = len([t for t in all_teams if t.p1_band == "REJECT"])
    borderline_count = len([t for t in all_teams if t.p1_band == "BORDERLINE"])
    fast_track_count = len([t for t in all_teams if t.p1_band == "FAST_TRACK"])

    # Get teams with pass1 data, sorted by composite score descending
    teams_with_scores = [t for t in all_teams if t.p1_composite is not None]
    teams_with_scores.sort(key=lambda t: (-t.p1_composite, t.team_id))

    return Pass1Status(
        run_id=run_id,
        run_status=run.status,
        progress=Pass1Progress(
            total=total,
            scored=scored,
            failed=failed,
            pending=pending
        ),
        bands_final=bands_final,
        band_counts=Pass1BandCounts(
            REJECT=reject_count,
            BORDERLINE=borderline_count,
            FAST_TRACK=fast_track_count
        ),
        teams=[TeamRead.from_model(t) for t in teams_with_scores]
    )


@router.post("/runs/{run_id}/pass2")
def start_pass2(
    run_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
):
    """Start Pass 2 evaluation for all promoted teams in this run.

    Steps:
    1. Check AI service is ready (503 if no LLM key)
    2. Lock the run row
    3. Validate run exists and status allows Pass 2 (404/409 otherwise)
    4. Count promoted teams (FAST_TRACK + BORDERLINE with p1_band set)
    5. Set status to pass2_running and commit BEFORE scheduling background task
    6. Schedule the background task
    7. Return immediately with teams_to_score count
    """
    # Step 1: Check AI service is ready
    try:
        ai_client.check_ready()
    except ai_client.AIServiceError as exc:
        logger.error("AI service not ready: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=str(exc)
        ) from exc

    # Step 2: Lock the run row
    statement = select(Run).where(Run.id == run_id).with_for_update()
    run = session.exec(statement).first()

    # Step 3: Validate run exists and status allows Pass 2
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    allowed_statuses = (RunStatus.PASS1_DONE, RunStatus.PASS2_INCOMPLETE, RunStatus.INTERRUPTED)
    if run.status not in allowed_statuses:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start Pass 2: run is in status '{run.status}'"
        )

    # If run is INTERRUPTED, it must be from Pass-2, not Pass-1
    if run.status == RunStatus.INTERRUPTED and (not run.error or "Pass-2" not in run.error):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start Pass 2: run is interrupted from Pass-1"
        )

    # Step 4: Count promoted teams (FAST_TRACK or BORDERLINE with p1_band set, not already p2_done)
    teams_to_score_count = session.exec(
        select(func.count(Team.id))
        .where(
            Team.run_id == run_id,
            Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
            Team.status != TeamStatus.P2_DONE,
        )
    ).one()

    # Step 5: Set status to pass2_running and commit
    run.status = RunStatus.PASS2_RUNNING
    run.error = None
    session.commit()

    # Step 6: Schedule the background task
    from app.pass2_runner import run_pass2
    background_tasks.add_task(run_pass2, run_id)

    # Step 7: Return immediately
    logger.info("Pass 2 started for run_id=%d, %d teams to score", run_id, teams_to_score_count)
    return {
        "run_id": run_id,
        "status": run.status,
        "teams_to_score": teams_to_score_count
    }


@router.get("/runs/{run_id}/pass2")
def get_pass2_status(run_id: int, session: Session = Depends(get_session)):
    """Get Pass 2 evaluation status and results for a run.

    Returns:
        - run_id, run_status
        - progress: promoted (total promoted teams), completed (with p2_score),
          failed (with error), pending (not yet scored)
        - teams: all promoted teams with pass2 data, ordered by final_rank asc
    """
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Get all promoted teams for this run
    promoted_teams = session.exec(
        select(Team).where(
            Team.run_id == run_id,
            Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
        )
    ).all()

    # Compute progress
    promoted = len(promoted_teams)
    completed = len([t for t in promoted_teams if t.p2_score is not None])
    failed = len([t for t in promoted_teams if t.p2_score is None and t.error is not None])
    pending = promoted - completed - failed

    # Get teams, sorted by final_rank asc (nulls last)
    promoted_teams.sort(key=lambda t: (t.final_rank is None, t.final_rank or float('inf')))

    return {
        "run_id": run_id,
        "run_status": run.status,
        "progress": {
            "promoted": promoted,
            "completed": completed,
            "failed": failed,
            "pending": pending
        },
        "teams": [TeamRead.from_model(t) for t in promoted_teams]
    }
