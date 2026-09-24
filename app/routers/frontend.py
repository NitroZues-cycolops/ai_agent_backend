"""Frontend-specific API router for the Next.js frontend adapter."""
import json
import logging
from typing import Optional, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select, func, or_

from app.database import get_session
from app.models import Team, Run, AuditLog, TeamStatus, RunStatus, utcnow
from app.routers.runs import latest_run
from app.ingest import parse_rows, ingest_rows, missing_columns
from app.sheets import get_sheet_rows

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Request / Response Schemas matching API_CONTRACT.md and src/types/
# ============================================================================

class Pass1ScoreModel(BaseModel):
    problemClarity: float
    originality: float
    execution: float
    feasibility: float
    articulation: float
    composite: float
    reasons: dict[str, str] = Field(default_factory=dict)
    track: str
    band: str


class TeamModel(BaseModel):
    id: str
    name: str
    theme: Optional[str] = None
    idea: str
    track: str
    projectLinks: list[str] = Field(default_factory=list)
    memberCount: Optional[int] = None
    status: str
    pass1: Optional[Pass1ScoreModel] = None
    pass2Score: Optional[float] = None
    pass2Verdict: Optional[list[str]] = None
    critique: Optional[dict[str, Any]] = None
    evidenceLinks: Optional[list[dict[str, Any]]] = None
    integrityFlags: Optional[list[str]] = None
    finalRank: Optional[int] = None
    overrideScore: Optional[float] = None
    auditorId: Optional[str] = None
    auditorNote: Optional[str] = None
    incompleteReasons: Optional[list[str]] = None
    createdAt: str
    updatedAt: str


class TeamsResponse(BaseModel):
    teams: list[TeamModel]
    total: int
    filtered: int


class FreezeSummary(BaseModel):
    frozenAt: str
    frozenBy: str
    shortlistCount: int
    rejectedCount: int
    overridesApplied: int
    snapshotId: str


class FreezeStatus(BaseModel):
    isFrozen: bool
    summary: Optional[FreezeSummary] = None


class FreezeResponse(BaseModel):
    success: bool
    summary: FreezeSummary


class FreezePayload(BaseModel):
    shortlistSize: int
    auditorId: str


class OverridePayload(BaseModel):
    overrideScore: float
    auditorNote: str
    auditorId: str


class OverrideResponse(BaseModel):
    success: bool
    team: TeamModel


class Pass1Bucket(BaseModel):
    range: str
    count: int


class Pass1RecentActivity(BaseModel):
    id: str
    time: str
    status: str
    score: Optional[float] = None
    band: Optional[str] = None


class Pass1StatsResponse(BaseModel):
    totalComplete: int
    scoredCount: int
    remainingCount: int
    activeWorkers: int
    idleWorkers: int
    estimatedCost: float
    meanScore: float
    medianScore: float
    bands: dict[str, int]
    scoreBuckets: list[Pass1Bucket]
    recentActivity: list[Pass1RecentActivity]


class Pass2StreamProgress(BaseModel):
    completed: int
    total: int
    percentage: int


class Pass2StatsResponse(BaseModel):
    promotedTotal: int
    completed: int
    running: int
    queued: int
    estimatedCost: float
    streams: dict[str, Pass2StreamProgress]


class DisputeItem(BaseModel):
    id: str
    teamId: str
    teamName: str
    projectTitle: str
    currentScore: float
    proposedScore: Optional[float] = None
    band: str
    reason: str
    aiVerdictSummary: str
    auditorId: Optional[str] = None
    auditorNote: Optional[str] = None
    status: str
    createdAt: str


class IncompleteTeamSummary(BaseModel):
    id: str
    name: str
    reasons: list[str]


class IngestResult(BaseModel):
    dryRun: bool
    rowsDetected: int
    complete: int
    incomplete: int
    queuedForP1: int
    incompleteList: list[IncompleteTeamSummary]


class RunState(BaseModel):
    id: str
    status: str
    startedAt: str
    elapsedSeconds: int
    totalTeams: int
    completeTeams: int
    incompleteTeams: int
    p1Completed: int
    p1Queued: int
    p2Promoted: int
    p2Completed: int
    p2Running: int
    p2Queued: int
    shortlistSize: int
    maxShortlistTarget: int
    activeWorkers: int
    totalWorkers: int
    estimatedCost: float
    p1Cost: float
    p2Cost: float
    budgetLimit: float
    isBudgetKillSwitchTriggered: bool
    frozenAt: Optional[str] = None
    frozenBy: Optional[str] = None
    currentStage: str


class RestartPayload(BaseModel):
    auditorId: Optional[str] = None


# ============================================================================
# Serializer Helpers
# ============================================================================

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime as ISO-8601 UTC string with 'Z' suffix."""
    if not dt:
        return None
    utc_dt = ensure_utc(dt)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def team_to_frontend(team: Team) -> TeamModel:
    """Convert database Team model to frontend TeamModel shape."""
    # Split project links
    project_links = []
    if team.project_links:
        project_links = [link.strip() for link in team.project_links.split(",") if link.strip()]

    # Pass-1 score and reasons
    pass1_score = None
    if team.p1_composite is not None:
        reasons_dict = {}
        if team.p1_reasons:
            try:
                raw_reasons = json.loads(team.p1_reasons)
                if isinstance(raw_reasons, dict):
                    for k, v in raw_reasons.items():
                        # Remap "clarity" -> "problemClarity"
                        if k == "clarity":
                            reasons_dict["problemClarity"] = str(v)
                        else:
                            reasons_dict[k] = str(v)
            except Exception:
                pass

        pass1_score = Pass1ScoreModel(
            problemClarity=team.p1_problem_clarity or 0.0,
            originality=team.p1_originality or 0.0,
            execution=team.p1_execution or 0.0,
            feasibility=team.p1_feasibility or 0.0,
            articulation=team.p1_articulation or 0.0,
            composite=team.p1_composite or 0.0,
            reasons=reasons_dict,
            track=team.track or "new_idea",
            band=team.p1_band or "BORDERLINE",
        )

    # Pass-2 verdict
    pass2_verdict = None
    if team.p2_verdict:
        try:
            parsed = json.loads(team.p2_verdict)
            if isinstance(parsed, list):
                pass2_verdict = parsed
            elif isinstance(parsed, str):
                pass2_verdict = [parsed]
        except Exception:
            pass2_verdict = [team.p2_verdict]

    # Critique
    critique = None
    if team.p2_critiques:
        try:
            critique = json.loads(team.p2_critiques)
        except Exception:
            pass

    # Evidence links
    evidence_links = None
    if team.evidence_links:
        try:
            evidence_links = json.loads(team.evidence_links)
        except Exception:
            pass

    # Integrity flags
    integrity_flags = None
    if team.integrity_flags:
        try:
            integrity_flags = json.loads(team.integrity_flags)
        except Exception:
            pass

    # Incomplete reasons
    incomplete_reasons = None
    if team.status == TeamStatus.INCOMPLETE and team.error:
        incomplete_reasons = [team.error]

    return TeamModel(
        id=team.team_id,
        name=team.team_name or "",
        theme=team.theme,
        idea=team.idea or "",
        track=team.track or "new_idea",
        projectLinks=project_links,
        memberCount=1,
        status=team.status,
        pass1=pass1_score,
        pass2Score=team.p2_score,
        pass2Verdict=pass2_verdict,
        critique=critique,
        evidenceLinks=evidence_links,
        integrityFlags=integrity_flags,
        finalRank=team.final_rank,
        overrideScore=team.override_score,
        auditorId=team.auditor_id,
        auditorNote=team.auditor_note,
        incompleteReasons=incomplete_reasons,
        createdAt=format_iso_utc(team.created_at) or "",
        updatedAt=format_iso_utc(team.updated_at) or "",
    )


def map_run_status(status: str) -> str:
    """Map DB Run.status to Frontend RunStatus enum."""
    if status == RunStatus.FROZEN:
        return "FROZEN"
    if status in (RunStatus.PASS1_RUNNING, RunStatus.PASS2_RUNNING):
        return "RUNNING"
    if status in (RunStatus.PASS1_DONE, RunStatus.PASS2_DONE):
        return "COMPLETED"
    if status in (RunStatus.PASS1_INCOMPLETE, RunStatus.INTERRUPTED):
        return "FAILED"
    return "IDLE"


def map_current_stage(status: str) -> str:
    """Map DB Run.status to Frontend currentStage enum."""
    if status == RunStatus.FROZEN:
        return "FROZEN"
    if status == RunStatus.PASS1_RUNNING:
        return "PASS_1"
    if status == RunStatus.PASS2_RUNNING:
        return "PASS_2"
    if status == RunStatus.PASS1_DONE:
        return "HUMAN_GATE"
    if status == RunStatus.PASS2_DONE:
        return "RANKING"
    return "INGEST"


def build_run_state(session: Session, run: Run) -> RunState:
    """Build the frontend RunState response."""
    # Query team counts
    teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    total_teams = len(teams)
    complete_teams = len([t for t in teams if t.status != TeamStatus.INCOMPLETE])
    incomplete_teams = total_teams - complete_teams

    p1_completed = len([t for t in teams if t.p1_composite is not None])
    p1_queued = len([t for t in teams if t.status == TeamStatus.P1_QUEUED])

    p2_promoted = len([
        t for t in teams
        if t.p1_band in ("BORDERLINE", "FAST_TRACK") or t.status in (TeamStatus.P2_QUEUED, TeamStatus.P2_DONE, TeamStatus.SHORTLIST)
    ])
    p2_completed = len([t for t in teams if t.status in (TeamStatus.P2_DONE, TeamStatus.SHORTLIST) or t.p2_score is not None])
    p2_running = 0
    p2_queued = len([t for t in teams if t.status == TeamStatus.P2_QUEUED])

    # Shortlist size
    shortlist_size = run.shortlist_size if run.shortlist_size is not None else 50

    # Timing
    if run.created_at:
        created_utc = ensure_utc(run.created_at)
        elapsed_seconds = max(0, int((utcnow() - created_utc).total_seconds()))
    else:
        elapsed_seconds = 0

    # Costs & workers
    active_workers = 42 if run.status in (RunStatus.PASS1_RUNNING, RunStatus.PASS2_RUNNING) else 0
    total_workers = 50
    p1_cost = round(p1_completed * 0.04, 2)
    p2_cost = round(p2_completed * 0.04, 2)
    estimated_cost = round(p1_cost + p2_cost, 2)

    return RunState(
        id=f"run-{run.id}",
        status=map_run_status(run.status),
        startedAt=format_iso_utc(run.created_at) or "",
        elapsedSeconds=elapsed_seconds,
        totalTeams=total_teams,
        completeTeams=complete_teams,
        incompleteTeams=incomplete_teams,
        p1Completed=p1_completed,
        p1Queued=p1_queued,
        p2Promoted=p2_promoted,
        p2Completed=p2_completed,
        p2Running=p2_running,
        p2Queued=p2_queued,
        shortlistSize=shortlist_size,
        maxShortlistTarget=50,
        activeWorkers=active_workers,
        totalWorkers=total_workers,
        estimatedCost=estimated_cost,
        p1Cost=p1_cost,
        p2Cost=p2_cost,
        budgetLimit=20.00,
        isBudgetKillSwitchTriggered=False,
        frozenAt=format_iso_utc(run.frozen_at),
        frozenBy=run.frozen_by,
        currentStage=map_current_stage(run.status),
    )


# ============================================================================
# Endpoints (11 routes required by API_CONTRACT.md)
# ============================================================================

# 1. GET /runs/current (and /run)
@router.get("/runs/current", response_model=RunState)
@router.get("/run", response_model=RunState)
def get_current_run_frontend(session: Session = Depends(get_session)):
    """Live run state for telemetry and dashboard."""
    run = latest_run(session)
    if not run:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

    return build_run_state(session, run)


# 2. POST /runs/current/restart (and /restart)
@router.post("/runs/current/restart", response_model=RunState)
@router.post("/restart", response_model=RunState)
def restart_run_frontend(
    body: Optional[RestartPayload] = None,
    session: Session = Depends(get_session)
):
    """Reset the run and clear any freeze."""
    statement = select(Run).order_by(Run.id.desc()).limit(1).with_for_update()
    run = session.exec(statement).first()

    if not run:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)
        return build_run_state(session, run)

    auditor_id = (body.auditorId if body and body.auditorId else None) or "admin"

    # Reset freeze state
    old_frozen_by = run.frozen_by
    if run.status == RunStatus.FROZEN:
        run.status = RunStatus.PASS1_DONE
    run.frozen_at = None
    run.frozen_by = None
    run.shortlist_size = None
    session.add(run)

    # Log action
    audit = AuditLog(
        run_id=run.id,
        team_id=None,
        action="unfreeze",
        actor=auditor_id,
        detail=json.dumps({"old_frozen_by": old_frozen_by}),
    )
    session.add(audit)
    session.commit()
    session.refresh(run)

    return build_run_state(session, run)


# 3. GET /runs/current/freeze (and /freeze)
@router.get("/runs/current/freeze", response_model=FreezeStatus)
@router.get("/freeze", response_model=FreezeStatus)
def get_freeze_status(session: Session = Depends(get_session)):
    """Check if the shortlist is frozen."""
    run = latest_run(session)
    if not run or run.status != RunStatus.FROZEN or not run.frozen_at:
        return FreezeStatus(isFrozen=False, summary=None)

    # Query counts for summary
    teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    shortlist_count = len([
        t for t in teams
        if t.status == TeamStatus.SHORTLIST or (t.final_rank and t.final_rank <= (run.shortlist_size or 50))
    ])
    rejected_count = len([t for t in teams if t.status == TeamStatus.REJECT or t.p1_band == "REJECT"])
    overrides_applied = len([t for t in teams if t.status == TeamStatus.OVERRIDE or t.override_score is not None])

    frozen_utc = ensure_utc(run.frozen_at)
    summary = FreezeSummary(
        frozenAt=format_iso_utc(run.frozen_at) or "",
        frozenBy=run.frozen_by or "unknown",
        shortlistCount=min(run.shortlist_size or shortlist_count, shortlist_count if shortlist_count > 0 else run.shortlist_size or 50),
        rejectedCount=rejected_count,
        overridesApplied=overrides_applied,
        snapshotId=f"snap-{int(frozen_utc.timestamp() * 1000)}" if frozen_utc else "snap-0",
    )
    return FreezeStatus(isFrozen=True, summary=summary)


# 4. POST /runs/current/freeze (and /freeze)
@router.post("/runs/current/freeze", response_model=FreezeResponse)
@router.post("/freeze", response_model=FreezeResponse)
def freeze_shortlist_frontend(
    body: FreezePayload,
    session: Session = Depends(get_session)
):
    """Freeze the shortlist."""
    if not isinstance(body.shortlistSize, int) or body.shortlistSize < 1:
        raise HTTPException(status_code=400, detail="shortlistSize must be a positive integer")

    if not body.auditorId or not body.auditorId.strip():
        raise HTTPException(status_code=400, detail="auditorId is required")

    statement = select(Run).order_by(Run.id.desc()).limit(1).with_for_update()
    run = session.exec(statement).first()

    if not run:
        raise HTTPException(status_code=404, detail="No run found")

    if run.status == RunStatus.FROZEN:
        raise HTTPException(status_code=409, detail="The shortlist is frozen and can no longer be changed")

    # Freeze the run
    now = utcnow()
    run.status = RunStatus.FROZEN
    run.frozen_at = now
    run.frozen_by = body.auditorId.strip()
    run.shortlist_size = body.shortlistSize
    session.add(run)

    # Query counts for summary
    teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    shortlist_teams = [
        t for t in teams
        if t.status == TeamStatus.SHORTLIST or (t.final_rank and t.final_rank <= body.shortlistSize)
    ]
    rejected_count = len([t for t in teams if t.status == TeamStatus.REJECT or t.p1_band == "REJECT"])
    overrides_applied = len([t for t in teams if t.status == TeamStatus.OVERRIDE or t.override_score is not None])

    summary = FreezeSummary(
        frozenAt=format_iso_utc(now) or "",
        frozenBy=body.auditorId.strip(),
        shortlistCount=min(body.shortlistSize, len(shortlist_teams)) if shortlist_teams else body.shortlistSize,
        rejectedCount=rejected_count,
        overridesApplied=overrides_applied,
        snapshotId=f"snap-{int(now.timestamp() * 1000)}",
    )

    # Log action
    audit = AuditLog(
        run_id=run.id,
        team_id=None,
        action="freeze",
        actor=body.auditorId.strip(),
        detail=json.dumps({"shortlist_size": body.shortlistSize}),
    )
    session.add(audit)
    session.commit()
    session.refresh(run)

    return FreezeResponse(success=True, summary=summary)


# 5. GET /runs/current/pass1/stats (and /pass1/stats)
@router.get("/runs/current/pass1/stats", response_model=Pass1StatsResponse)
@router.get("/pass1/stats", response_model=Pass1StatsResponse)
def get_pass1_stats(session: Session = Depends(get_session)):
    """Pass-1 stats for dashboard charts and live monitor."""
    run = latest_run(session)
    if not run:
        return Pass1StatsResponse(
            totalComplete=0,
            scoredCount=0,
            remainingCount=0,
            activeWorkers=0,
            idleWorkers=50,
            estimatedCost=0.0,
            meanScore=0.0,
            medianScore=0.0,
            bands={"reject": 0, "borderline": 0, "fastTrack": 0},
            scoreBuckets=[Pass1Bucket(range=f"{i}-{i+1}", count=0) for i in range(10)],
            recentActivity=[],
        )

    teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    total_complete = len([t for t in teams if t.status != TeamStatus.INCOMPLETE])
    scored_teams = [t for t in teams if t.p1_composite is not None]
    scored_count = len(scored_teams)
    remaining_count = max(0, total_complete - scored_count)

    # Bands
    fast_track_count = len([t for t in scored_teams if t.p1_band == "FAST_TRACK"])
    borderline_count = len([t for t in scored_teams if t.p1_band == "BORDERLINE"])
    reject_count = len([t for t in scored_teams if t.p1_band == "REJECT"])

    # Score buckets 0-10 in 1-point intervals
    buckets = [0] * 10
    for t in scored_teams:
        val = t.p1_composite or 0.0
        idx = min(9, max(0, int(val)))
        buckets[idx] += 1
    score_buckets = [Pass1Bucket(range=f"{i}-{i+1}", count=buckets[i]) for i in range(10)]

    # Mean and median
    composites = sorted([t.p1_composite for t in scored_teams if t.p1_composite is not None])
    if composites:
        mean_score = round(sum(composites) / len(composites), 2)
        mid = len(composites) // 2
        median_score = round(
            composites[mid] if len(composites) % 2 == 1 else (composites[mid - 1] + composites[mid]) / 2,
            2
        )
    else:
        mean_score = 0.0
        median_score = 0.0

    # Recent activity
    recent_activity = []
    # Sort teams by updated_at descending
    teams_by_recency = sorted(teams, key=lambda t: t.updated_at or t.created_at, reverse=True)[:10]
    for t in teams_by_recency:
        if t.p1_composite is not None:
            recent_activity.append(Pass1RecentActivity(
                id=t.team_id,
                time="recently",
                status="scored",
                score=t.p1_composite,
                band=t.p1_band,
            ))
        elif t.status == TeamStatus.P1_QUEUED:
            recent_activity.append(Pass1RecentActivity(
                id=t.team_id,
                time="just now",
                status="processing",
            ))

    active_workers = 42 if run.status == RunStatus.PASS1_RUNNING else 0
    p1_cost = round(scored_count * 0.04, 2)

    return Pass1StatsResponse(
        totalComplete=total_complete,
        scoredCount=scored_count,
        remainingCount=remaining_count,
        activeWorkers=active_workers,
        idleWorkers=50 - active_workers,
        estimatedCost=p1_cost,
        meanScore=mean_score,
        medianScore=median_score,
        bands={
            "reject": reject_count,
            "borderline": borderline_count,
            "fastTrack": fast_track_count,
        },
        scoreBuckets=score_buckets,
        recentActivity=recent_activity,
    )


# 6. GET /runs/current/pass2/stats (and /pass2/stats)
@router.get("/runs/current/pass2/stats", response_model=Pass2StatsResponse)
@router.get("/pass2/stats", response_model=Pass2StatsResponse)
def get_pass2_stats(session: Session = Depends(get_session)):
    """Pass-2 stats monitor.

    Streams: ai/ processes teams sequentially (3 critic calls + 1 judge per team),
    so we only know when a team's FULL Pass-2 is done, not which critic finished.
    All 4 streams therefore report the same completed/total, which is an approximation
    of progress across the full batch.
    """
    run = latest_run(session)
    if not run:
        return Pass2StatsResponse(
            promotedTotal=0,
            completed=0,
            running=0,
            queued=0,
            estimatedCost=0.0,
            streams={
                "themeCritic": Pass2StreamProgress(completed=0, total=0, percentage=0),
                "builderCritic": Pass2StreamProgress(completed=0, total=0, percentage=0),
                "integrityChecker": Pass2StreamProgress(completed=0, total=0, percentage=0),
                "judgeSynthesizer": Pass2StreamProgress(completed=0, total=0, percentage=0),
            },
        )

    # Count promoted teams (FAST_TRACK or BORDERLINE)
    promoted_total = session.exec(
        select(func.count(Team.id)).where(
            Team.run_id == run.id,
            Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
        )
    ).one()

    # Count completed (with p2_score set)
    completed = session.exec(
        select(func.count(Team.id)).where(
            Team.run_id == run.id,
            Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
            Team.p2_score != None,
        )
    ).one()

    # Count failed (promoted but with error and no p2_score)
    failed = session.exec(
        select(func.count(Team.id)).where(
            Team.run_id == run.id,
            Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
            Team.p2_score == None,
            Team.error != None,
        )
    ).one()

    # Running: is the run currently pass2_running?
    running = 1 if run.status == RunStatus.PASS2_RUNNING else 0

    # Pending: promoted teams not yet scored and not failed
    pending = promoted_total - completed - failed

    # Estimated cost: $0.04 per team for Pass-2
    cost = round(completed * 0.04, 2)

    # Progress percentage
    pct = int((completed / promoted_total * 100)) if promoted_total > 0 else 0

    # All streams report the same progress (we only know when a team's full Pass-2 is done)
    stream_progress = Pass2StreamProgress(
        completed=completed,
        total=promoted_total,
        percentage=pct
    )

    return Pass2StatsResponse(
        promotedTotal=promoted_total,
        completed=completed,
        running=running,
        queued=pending,
        estimatedCost=cost,
        streams={
            "themeCritic": stream_progress,
            "builderCritic": stream_progress,
            "integrityChecker": stream_progress,
            "judgeSynthesizer": stream_progress,
        },
    )


# 7. GET /teams
@router.get("/teams", response_model=TeamsResponse)
def get_teams_frontend(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    band: Optional[str] = Query(None),
    track: Optional[str] = Query(None),
    minScore: Optional[float] = Query(None),
    maxScore: Optional[float] = Query(None),
    integrityOnly: Optional[bool] = Query(None),
    overriddenOnly: Optional[bool] = Query(None),
    sortBy: Optional[str] = Query("rank"),
    sortOrder: Optional[str] = Query("asc"),
    session: Session = Depends(get_session)
):
    """List teams with query filtering and sorting."""
    # Validation of query parameters
    valid_sort_by = ("rank", "composite", "p2Score", "name", "id")
    if sortBy and sortBy not in valid_sort_by:
        raise HTTPException(status_code=400, detail=f'Query param "sortBy" must be one of: {", ".join(valid_sort_by)}')

    if sortOrder and sortOrder not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail='Query param "sortOrder" must be "asc" or "desc"')

    run = latest_run(session)
    if not run:
        return TeamsResponse(teams=[], total=0, filtered=0)

    # Get all teams for this run
    all_teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    total_count = len(all_teams)

    # Filter in Python
    filtered_teams = all_teams

    if search:
        q = search.lower()
        filtered_teams = [
            t for t in filtered_teams
            if (t.team_id and q in t.team_id.lower())
            or (t.team_name and q in t.team_name.lower())
            or (t.theme and q in t.theme.lower())
            or (t.idea and q in t.idea.lower())
        ]

    if status and status != "ALL":
        filtered_teams = [t for t in filtered_teams if t.status == status]

    if band and band != "ALL":
        filtered_teams = [t for t in filtered_teams if t.p1_band == band]

    if track and track != "ALL":
        filtered_teams = [t for t in filtered_teams if t.track == track]

    if minScore is not None:
        filtered_teams = [t for t in filtered_teams if (t.p1_composite or 0.0) >= minScore]

    if maxScore is not None:
        filtered_teams = [t for t in filtered_teams if (t.p1_composite or 0.0) <= maxScore]

    if integrityOnly:
        filtered_teams = [t for t in filtered_teams if t.integrity_flags and t.integrity_flags != "[]"]

    if overriddenOnly:
        filtered_teams = [t for t in filtered_teams if t.override_score is not None or t.status == TeamStatus.OVERRIDE]

    # Sorting
    sort_key = sortBy or "rank"
    order = sortOrder or "asc"

    def get_sort_tuple(t: Team):
        if sort_key == "rank":
            rank_val = t.final_rank if t.final_rank is not None else 999999
            return (rank_val, t.team_id)
        elif sort_key == "composite":
            # For composite/p2Score: sortOrder=asc means HIGHEST first
            score_val = t.p1_composite if t.p1_composite is not None else -1.0
            return (score_val if order == "desc" else -score_val, t.team_id)
        elif sort_key == "p2Score":
            # For composite/p2Score: sortOrder=asc means HIGHEST first
            score_val = t.p2_score if t.p2_score is not None else -1.0
            return (score_val if order == "desc" else -score_val, t.team_id)
        elif sort_key == "name":
            name_val = (t.team_name or "").lower()
            return (name_val, t.team_id)
        else:  # id
            return (t.team_id, )

    if sort_key in ("composite", "p2Score"):
        # The comparator tuple already handled asc=highest, desc=lowest
        filtered_teams.sort(key=get_sort_tuple)
    else:
        filtered_teams.sort(key=get_sort_tuple, reverse=(order == "desc"))

    return TeamsResponse(
        teams=[team_to_frontend(t) for t in filtered_teams],
        total=total_count,
        filtered=len(filtered_teams),
    )


# 8. GET /teams/{id}
@router.get("/teams/{team_id}", response_model=TeamModel)
def get_team_frontend(team_id: str, session: Session = Depends(get_session)):
    """Get a single team by team_id."""
    run = latest_run(session)
    if not run:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    statement = select(Team).where(Team.run_id == run.id, Team.team_id == team_id)
    team = session.exec(statement).first()

    if not team:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    return team_to_frontend(team)


# 9. POST /teams/{id}/override
@router.post("/teams/{team_id}/override", response_model=OverrideResponse)
def override_team_score(
    team_id: str,
    body: OverridePayload,
    session: Session = Depends(get_session)
):
    """Apply an auditor score override."""
    # Validate score
    if not isinstance(body.overrideScore, (int, float)) or body.overrideScore < 0 or body.overrideScore > 10:
        raise HTTPException(status_code=400, detail="overrideScore must be a number between 0 and 10")

    # Validate note
    note = body.auditorNote.strip() if body.auditorNote else ""
    if len(note) < 15:
        raise HTTPException(status_code=400, detail="auditorNote must be at least 15 characters")

    # Validate auditorId
    auditor_id = body.auditorId.strip() if body.auditorId else ""
    if not auditor_id:
        raise HTTPException(status_code=400, detail="auditorId is required")

    run = latest_run(session)
    if not run:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    if run.status == RunStatus.FROZEN:
        raise HTTPException(status_code=409, detail="The shortlist is frozen and can no longer be changed")

    # Lock team
    statement = select(Team).where(Team.run_id == run.id, Team.team_id == team_id).with_for_update()
    team = session.exec(statement).first()

    if not team:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    # Apply override
    team.status = TeamStatus.OVERRIDE
    team.override_score = float(body.overrideScore)
    team.auditor_note = note
    team.auditor_id = auditor_id
    team.overridden_at = utcnow()
    team.updated_at = utcnow()
    session.add(team)

    # Log audit
    audit = AuditLog(
        run_id=run.id,
        team_id=team_id,
        action="override",
        actor=auditor_id,
        detail=json.dumps({
            "override_score": body.overrideScore,
            "auditor_note": note,
        }),
    )
    session.add(audit)
    session.commit()
    session.refresh(team)

    return OverrideResponse(
        success=True,
        team=team_to_frontend(team),
    )


# 10. GET /disputes
@router.get("/disputes", response_model=list[DisputeItem])
def get_disputes(session: Session = Depends(get_session)):
    """Get the dispute / auditor review queue."""
    run = latest_run(session)
    if not run:
        return []

    teams = session.exec(select(Team).where(Team.run_id == run.id)).all()
    disputes = []

    for idx, t in enumerate(teams):
        if t.status == TeamStatus.OVERRIDE or t.override_score is not None:
            disputes.append(DisputeItem(
                id=f"disp-{idx+1:03d}",
                teamId=t.team_id,
                teamName=t.team_name or "",
                projectTitle=t.theme or t.idea or "Project",
                currentScore=t.p1_composite or 0.0,
                proposedScore=t.override_score,
                band=t.p1_band or "BORDERLINE",
                reason=f"Auditor review: {t.auditor_note or 'Manual override'}",
                aiVerdictSummary=f"Score overridden to {t.override_score}",
                auditorId=t.auditor_id,
                auditorNote=t.auditor_note,
                status="RESOLVED",
                createdAt=format_iso_utc(t.overridden_at or t.created_at) or "",
            ))
        elif t.p1_band == "BORDERLINE":
            disputes.append(DisputeItem(
                id=f"disp-{idx+1:03d}",
                teamId=t.team_id,
                teamName=t.team_name or "",
                projectTitle=t.theme or t.idea or "Project",
                currentScore=t.p1_composite or 0.0,
                proposedScore=None,
                band="BORDERLINE",
                reason="Borderline score requires auditor review",
                aiVerdictSummary="Borderline band (score 6.0 - 7.5)",
                status="PENDING",
                createdAt=format_iso_utc(t.created_at) or "",
            ))

    return disputes


# 11. POST /ingest
@router.post("/ingest", response_model=IngestResult)
def start_ingest_frontend(
    dryRun: bool = Query(False),
    session: Session = Depends(get_session)
):
    """Validate or queue registrations from the Google Sheet."""
    try:
        rows = get_sheet_rows()
    except Exception as exc:
        logger.error("Failed to read Google Sheet: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Could not read the Google Sheet"
        ) from exc

    if not rows:
        raise HTTPException(status_code=400, detail="Sheet has no data rows")

    missing = missing_columns(rows)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Sheet is missing required columns: {', '.join(missing)}"
        )

    if dryRun:
        teams, metadata = parse_rows(rows)
        incomplete_teams = [t for t in teams if t["status"] != TeamStatus.P1_QUEUED]
        complete_teams = [t for t in teams if t["status"] == TeamStatus.P1_QUEUED]

        incomplete_list = [
            IncompleteTeamSummary(
                id=t["team_id"],
                name=t.get("team_name") or "",
                reasons=t.get("problems") or ["Missing required fields"]
            )
            for t in incomplete_teams
        ]

        return IngestResult(
            dryRun=True,
            rowsDetected=len(rows),
            complete=len(complete_teams),
            incomplete=len(incomplete_teams),
            queuedForP1=len(complete_teams),
            incompleteList=incomplete_list,
        )

    # Real ingest
    run = latest_run(session)
    if not run:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

    if run.status not in (RunStatus.CREATED, RunStatus.INGESTED, RunStatus.PASS1_INCOMPLETE):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot ingest: run is in status '{run.status}'"
        )

    summary = ingest_rows(session, run.id, rows)

    if summary["teams_in_sheet"] == 0:
        raise HTTPException(status_code=400, detail="No team_id could be read from any row")

    run.status = RunStatus.INGESTED
    session.add(run)
    session.commit()
    session.refresh(run)

    incomplete_list = [
        IncompleteTeamSummary(
            id=item["team_id"],
            name="",
            reasons=item.get("problems") or ["Missing required fields"]
        )
        for item in summary.get("incomplete_teams", [])
    ]

    return IngestResult(
        dryRun=False,
        rowsDetected=summary["rows_in_sheet"],
        complete=summary["complete"],
        incomplete=summary["incomplete"],
        queuedForP1=summary["complete"],
        incompleteList=incomplete_list,
    )


# 12. GET /audit-log
@router.get("/audit-log", response_model=list[dict])
def get_audit_log(
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session)
):
    """Get audit log entries for the current run."""
    run = latest_run(session)
    if not run:
        return []

    statement = (
        select(AuditLog)
        .where(AuditLog.run_id == run.id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    logs = session.exec(statement).all()

    return [
        {
            "id": log.id,
            "runId": log.run_id,
            "teamId": log.team_id,
            "action": log.action,
            "actor": log.actor,
            "detail": json.loads(log.detail) if log.detail else None,
            "createdAt": format_iso_utc(log.created_at),
        }
        for log in logs
    ]
