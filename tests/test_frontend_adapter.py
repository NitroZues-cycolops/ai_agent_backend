"""Comprehensive tests for the frontend API adapter under /api/v1."""
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.database import get_session
from app.models import Run, Team, AuditLog, RunStatus, TeamStatus, utcnow


def test_dual_error_format():
    """Verify that error responses contain both 'detail' and 'message' fields."""
    with TestClient(app) as client:
        # 404 error
        res = client.get("/api/v1/teams/non-existent-team-id")
        assert res.status_code == 404
        data = res.json()
        assert "detail" in data
        assert "message" in data
        assert data["detail"] == data["message"]
        assert "not found" in data["detail"].lower()

        # 400 error
        res = client.get("/api/v1/teams?sortBy=invalidSort")
        assert res.status_code == 400
        data = res.json()
        assert "detail" in data
        assert "message" in data
        assert data["detail"] == data["message"]


def test_get_run_state():
    """Test GET /api/v1/runs/current and /api/v1/run return full telemetry."""
    with TestClient(app) as client:
        # Create a run via legacy or frontend
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_RUNNING)
            session.add(run)
            session.commit()
            session.refresh(run)

            t1 = Team(
                run_id=run.id,
                team_id="T01",
                team_name="Alpha",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=8.5,
                p1_band="FAST_TRACK",
            )
            t2 = Team(
                run_id=run.id,
                team_id="T02",
                team_name="Beta",
                track="existing_project",
                status=TeamStatus.P1_QUEUED,
            )
            session.add(t1)
            session.add(t2)
            session.commit()
            run_id = run.id

        # Test both aliases
        for endpoint in ("/api/v1/runs/current", "/api/v1/run"):
            res = client.get(endpoint)
            assert res.status_code == 200
            data = res.json()
            assert data["id"] == f"run-{run_id}"
            assert data["status"] == "RUNNING"
            assert data["currentStage"] == "PASS_1"
            assert data["totalTeams"] == 2
            assert data["p1Completed"] == 1
            assert data["p1Queued"] == 1
            assert data["activeWorkers"] == 42
            assert "Z" in data["startedAt"]


def test_freeze_lifecycle_and_restart():
    """Test freeze validation, status check, conflicts, and restart unfreezing."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            t1 = Team(
                run_id=run.id,
                team_id="T10",
                team_name="Team 10",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=9.0,
                p1_band="FAST_TRACK",
                final_rank=1,
            )
            session.add(t1)
            session.commit()

        # Check initial freeze status -> false
        res = client.get("/api/v1/freeze")
        assert res.status_code == 200
        assert res.json()["isFrozen"] is False
        assert res.json()["summary"] is None

        # Invalid freeze payloads
        res = client.post("/api/v1/freeze", json={"shortlistSize": 0, "auditorId": "aud-1"})
        assert res.status_code == 400
        assert "shortlistSize must be a positive integer" in res.json()["detail"]

        res = client.post("/api/v1/freeze", json={"shortlistSize": 10, "auditorId": ""})
        assert res.status_code == 400
        assert "auditorId is required" in res.json()["detail"]

        # Valid freeze
        res = client.post("/api/v1/freeze", json={"shortlistSize": 25, "auditorId": "auditor-42"})
        assert res.status_code == 200
        freeze_data = res.json()
        assert freeze_data["success"] is True
        assert freeze_data["summary"]["frozenBy"] == "auditor-42"
        assert freeze_data["summary"]["shortlistCount"] == 1

        # Check freeze status -> true
        res = client.get("/api/v1/freeze")
        assert res.status_code == 200
        assert res.json()["isFrozen"] is True
        assert res.json()["summary"]["frozenBy"] == "auditor-42"

        # Freeze again -> 409 Conflict
        res = client.post("/api/v1/freeze", json={"shortlistSize": 25, "auditorId": "auditor-42"})
        assert res.status_code == 409
        assert "already" in res.json()["detail"].lower() or "frozen" in res.json()["detail"].lower()

        # Override on frozen team -> 409 Conflict
        res = client.post(
            "/api/v1/teams/T10/override",
            json={
                "overrideScore": 8.0,
                "auditorNote": "This is an auditor note longer than 15 chars",
                "auditorId": "auditor-42",
            }
        )
        assert res.status_code == 409

        # Restart run -> Unfreezes
        res = client.post("/api/v1/restart", json={"auditorId": "auditor-42"})
        assert res.status_code == 200
        assert res.json()["status"] == "COMPLETED"
        assert res.json()["frozenAt"] is None

        # Verify unfreeze status
        res = client.get("/api/v1/freeze")
        assert res.status_code == 200
        assert res.json()["isFrozen"] is False


def test_pass1_and_pass2_stats():
    """Test statistics endpoints for Pass-1 and Pass-2."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            t1 = Team(
                run_id=run.id,
                team_id="T01",
                team_name="Team A",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=8.2,
                p1_band="FAST_TRACK",
            )
            t2 = Team(
                run_id=run.id,
                team_id="T02",
                team_name="Team B",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=6.5,
                p1_band="BORDERLINE",
            )
            t3 = Team(
                run_id=run.id,
                team_id="T03",
                team_name="Team C",
                track="new_idea",
                status=TeamStatus.REJECT,
                p1_composite=3.4,
                p1_band="REJECT",
            )
            session.add(t1)
            session.add(t2)
            session.add(t3)
            session.commit()

        # Pass 1 stats
        res = client.get("/api/v1/runs/current/pass1/stats")
        assert res.status_code == 200
        p1_data = res.json()
        assert p1_data["totalComplete"] == 3
        assert p1_data["scoredCount"] == 3
        assert p1_data["remainingCount"] == 0
        assert p1_data["bands"]["fastTrack"] == 1
        assert p1_data["bands"]["borderline"] == 1
        assert p1_data["bands"]["reject"] == 1
        assert len(p1_data["scoreBuckets"]) == 10
        assert p1_data["scoreBuckets"][8]["count"] == 1  # 8.2 is in bucket "8-9"

        # Pass 2 stats
        res = client.get("/api/v1/runs/current/pass2/stats")
        assert res.status_code == 200
        p2_data = res.json()
        assert p2_data["promotedTotal"] == 2  # FAST_TRACK + BORDERLINE
        assert "streams" in p2_data
        assert "judgeSynthesizer" in p2_data["streams"]


def test_teams_filtering_and_sorting():
    """Test filtering parameters and sorting quirk (asc=highest for score fields)."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            t1 = Team(
                run_id=run.id,
                team_id="T_LOW",
                team_name="Zeta Team",
                theme="Healthcare",
                idea="AI Doctor",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=4.0,
                p1_band="REJECT",
                final_rank=3,
            )
            t2 = Team(
                run_id=run.id,
                team_id="T_MID",
                team_name="Beta Team",
                theme="FinTech",
                idea="AI Banking",
                track="existing_project",
                status=TeamStatus.OVERRIDE,
                override_score=7.0,
                p1_composite=6.5,
                p1_band="BORDERLINE",
                final_rank=2,
                integrity_flags=json.dumps(["flag1"]),
            )
            t3 = Team(
                run_id=run.id,
                team_id="T_HIGH",
                team_name="Alpha Team",
                theme="Climate",
                idea="AI Carbon",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=9.5,
                p1_band="FAST_TRACK",
                final_rank=1,
            )
            session.add(t1)
            session.add(t2)
            session.add(t3)
            session.commit()

        # 1. Search filter
        res = client.get("/api/v1/teams?search=carbon")
        assert res.status_code == 200
        data = res.json()
        assert data["filtered"] == 1
        assert data["teams"][0]["id"] == "T_HIGH"

        # 2. Track filter
        res = client.get("/api/v1/teams?track=existing_project")
        assert res.status_code == 200
        assert res.json()["filtered"] == 1
        assert res.json()["teams"][0]["id"] == "T_MID"

        # 3. Band filter
        res = client.get("/api/v1/teams?band=FAST_TRACK")
        assert res.status_code == 200
        assert res.json()["filtered"] == 1
        assert res.json()["teams"][0]["id"] == "T_HIGH"

        # 4. Integrity only filter
        res = client.get("/api/v1/teams?integrityOnly=true")
        assert res.status_code == 200
        assert res.json()["filtered"] == 1
        assert res.json()["teams"][0]["id"] == "T_MID"

        # 5. Overridden only filter
        res = client.get("/api/v1/teams?overriddenOnly=true")
        assert res.status_code == 200
        assert res.json()["filtered"] == 1
        assert res.json()["teams"][0]["id"] == "T_MID"

        # 6. Score range filter
        res = client.get("/api/v1/teams?minScore=6.0&maxScore=8.0")
        assert res.status_code == 200
        assert res.json()["filtered"] == 1
        assert res.json()["teams"][0]["id"] == "T_MID"

        # 7. Sorting: composite asc should sort HIGHEST first
        res = client.get("/api/v1/teams?sortBy=composite&sortOrder=asc")
        assert res.status_code == 200
        teams = res.json()["teams"]
        assert teams[0]["id"] == "T_HIGH"  # 9.5
        assert teams[1]["id"] == "T_MID"   # 6.5
        assert teams[2]["id"] == "T_LOW"   # 4.0

        # 8. Sorting: composite desc should sort LOWEST first
        res = client.get("/api/v1/teams?sortBy=composite&sortOrder=desc")
        assert res.status_code == 200
        teams = res.json()["teams"]
        assert teams[0]["id"] == "T_LOW"   # 4.0
        assert teams[1]["id"] == "T_MID"   # 6.5
        assert teams[2]["id"] == "T_HIGH"  # 9.5

        # 9. Sorting: name asc should be alphabetical
        res = client.get("/api/v1/teams?sortBy=name&sortOrder=asc")
        assert res.status_code == 200
        teams = res.json()["teams"]
        assert teams[0]["id"] == "T_HIGH"  # Alpha Team
        assert teams[1]["id"] == "T_MID"   # Beta Team
        assert teams[2]["id"] == "T_LOW"   # Zeta Team


def test_team_detail_and_reason_remapping():
    """Test single team lookup and clarity -> problemClarity reason remapping."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            reasons = {
                "clarity": "Problem is well articulated",
                "originality": "Highly innovative idea",
            }
            t = Team(
                run_id=run.id,
                team_id="T_DETAIL",
                team_name="Detail Team",
                track="new_idea",
                idea="Smart contracts",
                project_links="https://github.com/org/repo, https://demo.app",
                status=TeamStatus.P1_DONE,
                p1_problem_clarity=9.0,
                p1_originality=8.5,
                p1_execution=8.0,
                p1_feasibility=8.0,
                p1_articulation=8.0,
                p1_composite=8.3,
                p1_reasons=json.dumps(reasons),
                p1_band="FAST_TRACK",
            )
            session.add(t)
            session.commit()

        res = client.get("/api/v1/teams/T_DETAIL")
        assert res.status_code == 200
        team_data = res.json()
        assert team_data["id"] == "T_DETAIL"
        assert len(team_data["projectLinks"]) == 2
        assert team_data["projectLinks"][0] == "https://github.com/org/repo"
        assert team_data["pass1"]["reasons"]["problemClarity"] == "Problem is well articulated"
        assert "clarity" not in team_data["pass1"]["reasons"]


def test_override_score_endpoint():
    """Test auditor override validation and successful mutation."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            t = Team(
                run_id=run.id,
                team_id="T_OVERRIDE",
                team_name="Override Me",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=6.0,
            )
            session.add(t)
            session.commit()

        # Invalid score (< 0)
        res = client.post(
            "/api/v1/teams/T_OVERRIDE/override",
            json={"overrideScore": -1.0, "auditorNote": "Valid length note here", "auditorId": "aud-1"}
        )
        assert res.status_code == 400
        assert "between 0 and 10" in res.json()["detail"]

        # Note too short (< 15 chars)
        res = client.post(
            "/api/v1/teams/T_OVERRIDE/override",
            json={"overrideScore": 8.5, "auditorNote": "Too short", "auditorId": "aud-1"}
        )
        assert res.status_code == 400
        assert "at least 15 characters" in res.json()["detail"]

        # Missing auditorId
        res = client.post(
            "/api/v1/teams/T_OVERRIDE/override",
            json={"overrideScore": 8.5, "auditorNote": "Valid length note here", "auditorId": ""}
        )
        assert res.status_code == 400
        assert "auditorId is required" in res.json()["detail"]

        # Successful override
        res = client.post(
            "/api/v1/teams/T_OVERRIDE/override",
            json={
                "overrideScore": 8.5,
                "auditorNote": "Thoroughly verified project repo and demo",
                "auditorId": "auditor-99",
            }
        )
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["team"]["status"] == "OVERRIDE"
        assert body["team"]["overrideScore"] == 8.5
        assert body["team"]["auditorId"] == "auditor-99"

        # Check audit log
        res_audit = client.get("/api/v1/audit-log")
        assert res_audit.status_code == 200
        logs = res_audit.json()
        assert len(logs) > 0
        assert logs[0]["action"] == "override"
        assert logs[0]["actor"] == "auditor-99"


def test_disputes_endpoint():
    """Test disputes queue generation for borderline and overridden teams."""
    with TestClient(app) as client:
        with next(get_session()) as session:
            run = Run(status=RunStatus.PASS1_DONE)
            session.add(run)
            session.commit()
            session.refresh(run)

            t1 = Team(
                run_id=run.id,
                team_id="T_BORDER",
                team_name="Borderline Team",
                theme="EdTech",
                idea="AI Tutor",
                track="new_idea",
                status=TeamStatus.P1_DONE,
                p1_composite=6.8,
                p1_band="BORDERLINE",
            )
            t2 = Team(
                run_id=run.id,
                team_id="T_OVER",
                team_name="Overridden Team",
                theme="HealthTech",
                idea="AI Monitor",
                track="new_idea",
                status=TeamStatus.OVERRIDE,
                override_score=8.0,
                auditor_id="aud-1",
                auditor_note="Reviewed manually and approved",
            )
            session.add(t1)
            session.add(t2)
            session.commit()

        res = client.get("/api/v1/disputes")
        assert res.status_code == 200
        disputes = res.json()
        assert len(disputes) == 2
        statuses = {d["status"] for d in disputes}
        assert "PENDING" in statuses
        assert "RESOLVED" in statuses


def test_ingest_dry_run_vs_real():
    """Test POST /api/v1/ingest dry-run vs real ingestion."""
    mock_rows = [
        {
            "team_id": "T01",
            "team_name": "Team 1",
            "track": "new_idea",
            "theme": "AI",
            "idea": "Idea 1",
            "resume_urls": "https://resume.pdf",
            "project_links": "https://github.com",
        },
        {
            "team_id": "T02",
            "team_name": "Team 2",
            "track": "invalid_track",  # will be incomplete
            "theme": "AI",
            "idea": "Idea 2",
            "resume_urls": "https://resume.pdf",
            "project_links": "https://github.com",
        },
    ]

    with TestClient(app) as client:
        with patch("app.routers.frontend.get_sheet_rows", return_value=mock_rows):
            # 1. Dry run
            res_dry = client.post("/api/v1/ingest?dryRun=true")
            assert res_dry.status_code == 200
            dry_data = res_dry.json()
            assert dry_data["dryRun"] is True
            assert dry_data["rowsDetected"] == 2
            assert dry_data["complete"] == 1
            assert dry_data["incomplete"] == 1
            assert len(dry_data["incompleteList"]) == 1
            assert dry_data["incompleteList"][0]["id"] == "T02"

            # 2. Real ingest
            res_real = client.post("/api/v1/ingest?dryRun=false")
            assert res_real.status_code == 200
            real_data = res_real.json()
            assert real_data["dryRun"] is False
            assert real_data["rowsDetected"] == 2
            assert real_data["complete"] == 1
            assert real_data["incomplete"] == 1
