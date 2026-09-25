"""Schemas and serializers for the frontend API."""
from typing import Optional, Any
from pydantic import BaseModel, Field


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
