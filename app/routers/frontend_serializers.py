"""Serializers for converting database models to frontend schemas."""
import json
from datetime import datetime, timezone
from typing import Optional

from app.models import Team, Run, TeamStatus, RunStatus, utcnow
from app.routers.frontend_schemas import Pass1ScoreModel, TeamModel, RunState


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


def build_run_state(session, run: Run) -> RunState:
    """Build the frontend RunState response."""
    from sqlmodel import select
    from app.models import Team

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
