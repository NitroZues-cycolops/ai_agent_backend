"""Response schemas for the API."""
import json
import logging
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RunRead(BaseModel):
    """Response schema for a Run, with computed team counts."""
    id: int
    status: str
    created_at: datetime
    frozen_at: datetime | None
    shortlist_size: int | None
    error: str | None
    total_teams: int
    counts_by_status: dict[str, int]


class IncompleteTeamDetail(BaseModel):
    """Details about one incomplete team in the ingest report."""
    team_id: str
    sheet_row: int
    problems: list[str]


class IngestReport(BaseModel):
    """Response schema for the /runs/{run_id}/ingest endpoint."""
    run_id: int
    status: str
    rows_in_sheet: int
    teams_in_sheet: int
    created: int
    updated: int
    complete: int
    incomplete: int
    incomplete_teams: list[IncompleteTeamDetail] = Field(default_factory=list)
    empty_rows: list[int] = Field(default_factory=list)
    rows_without_team_id: list[int] = Field(default_factory=list)
    in_database_not_in_sheet: list[str] = Field(default_factory=list)
    locked_not_updated: list[str] = Field(default_factory=list)


def parse_json_field(val: str | None, default: Any = None) -> Any:
    """Helper to safely parse JSON text fields, returning default on error."""
    if not val:
        return default
    try:
        return json.loads(val)
    except Exception as exc:
        logger.error("Failed to parse JSON field: %s", exc)
        return default


class TeamRead(BaseModel):
    """Response schema for a Team with parsed JSON fields."""
    id: int | None
    run_id: int
    team_id: str
    team_name: str | None
    theme: str | None
    idea: str | None
    track: str | None
    resume_path: str | None
    resume_text: str | None
    project_links: str | None
    status: str
    error: str | None

    # Pass-1 fields
    p1_problem_clarity: float | None
    p1_originality: float | None
    p1_execution: float | None
    p1_feasibility: float | None
    p1_articulation: float | None
    p1_composite: float | None
    p1_reasons: dict | None = None
    p1_reason_codes: list | None = None
    p1_band: str | None

    # Pass-2 fields
    p2_score: float | None
    p2_recommendation: str | None
    p2_verdict: str | None
    p2_critiques: list | dict | None = None
    evidence_links: list | None = None
    integrity_flags: list | None = None

    # Human override
    override_score: float | None
    auditor_id: str | None
    auditor_note: str | None
    overridden_at: datetime | None
    final_rank: int | None

    # Computed fields
    effective_score: float | None = None

    @classmethod
    def from_model(cls, team) -> "TeamRead":
        """Construct a TeamRead schema from a Team database model instance."""
        effective_score = team.override_score if team.override_score is not None else team.p2_score

        return cls(
            id=team.id,
            run_id=team.run_id,
            team_id=team.team_id,
            team_name=team.team_name,
            theme=team.theme,
            idea=team.idea,
            track=team.track,
            resume_path=team.resume_path,
            resume_text=team.resume_text,
            project_links=team.project_links,
            status=team.status,
            error=team.error,
            p1_problem_clarity=team.p1_problem_clarity,
            p1_originality=team.p1_originality,
            p1_execution=team.p1_execution,
            p1_feasibility=team.p1_feasibility,
            p1_articulation=team.p1_articulation,
            p1_composite=team.p1_composite,
            p1_reasons=parse_json_field(team.p1_reasons, None),
            p1_reason_codes=parse_json_field(team.p1_reason_codes, None),
            p1_band=team.p1_band,
            p2_score=team.p2_score,
            p2_recommendation=team.p2_recommendation,
            p2_verdict=team.p2_verdict,
            p2_critiques=parse_json_field(team.p2_critiques, None),
            evidence_links=parse_json_field(team.evidence_links, None),
            integrity_flags=parse_json_field(team.integrity_flags, None),
            override_score=team.override_score,
            auditor_id=team.auditor_id,
            auditor_note=team.auditor_note,
            overridden_at=team.overridden_at,
            final_rank=team.final_rank,
            effective_score=effective_score,
        )


class Pass1Progress(BaseModel):
    """Progress statistics for Pass 1."""
    total: int
    scored: int
    failed: int
    pending: int


class Pass1BandCounts(BaseModel):
    """Counts of teams in each band."""
    REJECT: int
    BORDERLINE: int
    FAST_TRACK: int


class Pass1Status(BaseModel):
    """Response schema for GET /runs/{id}/pass1."""
    run_id: int
    run_status: str
    progress: Pass1Progress
    bands_final: bool
    band_counts: Pass1BandCounts
    teams: list[TeamRead]
