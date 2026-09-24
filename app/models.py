from datetime import datetime, timezone
from typing import Optional
 
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field
 
 
def utcnow() -> datetime:
    return datetime.now(timezone.utc)
 
 
class RunStatus:
    """Allowed values for Run.status. Plain strings, not an Enum, so the
    database column stays a simple text column (easier migrations later)."""
    CREATED = "created"
    INGESTED = "ingested"
    PASS1_RUNNING = "pass1_running"
    PASS1_DONE = "pass1_done"
    PASS1_INCOMPLETE = "pass1_incomplete"
    PASS2_RUNNING = "pass2_running"
    PASS2_DONE = "pass2_done"
    PASS2_INCOMPLETE = "pass2_incomplete"
    FROZEN = "frozen"
    INTERRUPTED = "interrupted"
 
 
class TeamStatus:
    """Allowed values for Team.status (from the spec's lifecycle labels)."""
    INCOMPLETE = "INCOMPLETE"
    P1_QUEUED = "P1_QUEUED"
    P1_DONE = "P1_DONE"
    REJECT = "REJECT"
    P2_QUEUED = "P2_QUEUED"
    P2_DONE = "P2_DONE"
    SHORTLIST = "SHORTLIST"
    OVERRIDE = "OVERRIDE"
 
 
class Run(SQLModel, table=True):
    """One full shortlisting attempt. Team counts are NOT stored here;
    we count Team rows when asked, so the numbers can never drift."""
    id: Optional[int] = Field(default=None, primary_key=True)
    status: str = Field(default=RunStatus.CREATED, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    frozen_at: Optional[datetime] = None
    frozen_by: Optional[str] = None       # auditor id who froze the run
    shortlist_size: Optional[int] = None  # "N", set at freeze time
    error: Optional[str] = None           # last background-task failure, if any
 
 
class Team(SQLModel, table=True):
    """One team inside one run. The same team_id can appear in many runs,
    but only once per run (see the unique constraint below)."""
    __table_args__ = (UniqueConstraint("run_id", "team_id", name="uq_team_per_run"),)
 
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    team_id: str = Field(index=True)
 
    # Source data (from the sheet)
    team_name: Optional[str] = None
    theme: Optional[str] = None
    idea: Optional[str] = None
    track: Optional[str] = None            # "new_idea" | "existing_project"
    resume_path: Optional[str] = None
    resume_text: Optional[str] = None      # text extracted from the PDF
    project_links: Optional[str] = None
 
    status: str = Field(default=TeamStatus.INCOMPLETE, index=True)
    error: Optional[str] = None            # why scoring failed for this team
 
    # Pass-1
    p1_problem_clarity: Optional[float] = None
    p1_originality: Optional[float] = None
    p1_execution: Optional[float] = None
    p1_feasibility: Optional[float] = None
    p1_articulation: Optional[float] = None
    p1_composite: Optional[float] = None
    p1_reasons: Optional[str] = None       # JSON text
    p1_reason_codes: Optional[str] = None  # JSON text
    p1_band: Optional[str] = None          # REJECT | BORDERLINE | FAST_TRACK
 
    # Pass-2
    p2_score: Optional[float] = None
    p2_recommendation: Optional[str] = None  # strong_yes | yes | borderline | no
    p2_verdict: Optional[str] = None         # judge summary text
    p2_critiques: Optional[str] = None       # full critique JSON from ai/
    evidence_links: Optional[str] = None     # JSON text
    integrity_flags: Optional[str] = None    # JSON text
 
    # Human override (only set through the override endpoint)
    override_score: Optional[float] = None
    auditor_id: Optional[str] = None
    auditor_note: Optional[str] = None
    overridden_at: Optional[datetime] = None
 
    final_rank: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
 
 
class AuditLog(SQLModel, table=True):
    """Append-only history of human actions (override, freeze, unfreeze).
    Rows are only ever added, never edited or deleted."""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    team_id: Optional[str] = None
    action: str                             # "override" | "freeze" | "unfreeze"
    actor: str                              # auditor id
    detail: Optional[str] = None            # JSON text: old/new score, note
    created_at: datetime = Field(default_factory=utcnow)