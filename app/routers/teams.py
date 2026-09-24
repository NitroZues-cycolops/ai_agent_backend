"""Routes for querying teams."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.database import get_session
from app.models import Team
from app.schemas import TeamRead
from app.routers.runs import latest_run

router = APIRouter()


@router.get("/teams", response_model=list[TeamRead])
def list_teams(
    run_id: int | None = Query(None, description="Run ID to filter by. Defaults to the latest run."),
    session: Session = Depends(get_session)
):
    """List all teams, optionally filtered by run_id.

    If run_id is not provided, defaults to the latest run.
    Returns [] if there are no runs.

    Teams are ordered by team_id.
    """
    if run_id is None:
        # Default to the latest run
        run = latest_run(session)
        if not run:
            # No runs exist yet
            return []
        run_id = run.id

    statement = select(Team).where(Team.run_id == run_id).order_by(Team.team_id)
    teams = session.exec(statement).all()

    return [TeamRead.from_model(team) for team in teams]


@router.get("/teams/{team_pk}", response_model=TeamRead)
def get_team(team_pk: int, session: Session = Depends(get_session)):
    """Get a specific team by its database primary key (NOT the sheet team_id).

    Args:
        team_pk: The database id (Team.id), not the sheet team_id.

    Returns:
        The team record.

    Raises:
        404: Team not found.
    """
    team = session.get(Team, team_pk)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team with id={team_pk} not found")

    return TeamRead.from_model(team)
