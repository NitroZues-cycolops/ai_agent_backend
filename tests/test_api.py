"""API-level tests for run and ingest endpoints."""
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.main import app
from app.database import get_session
from app.models import Run, Team, RunStatus, TeamStatus


def test_runs_current_vs_runs_by_id():
    """Test that /runs/current returns latest run and doesn't conflict with /runs/{run_id}."""
    with TestClient(app) as client:
        # Create two runs
        r1 = client.post("/api/runs").json()
        r2 = client.post("/api/runs").json()

        # /runs/current should return the latest (r2)
        current = client.get("/api/runs/current").json()
        assert current["id"] == r2["id"]

        # /runs/{id} should return specific runs
        get_r1 = client.get(f"/api/runs/{r1['id']}").json()
        assert get_r1["id"] == r1["id"]

        get_r2 = client.get(f"/api/runs/{r2['id']}").json()
        assert get_r2["id"] == r2["id"]


def test_ingest_409_on_wrong_status():
    """Ingest should return 409 if run is not in created/ingested status."""
    with TestClient(app) as client:
        # Create a run
        run_data = client.post("/api/runs").json()
        run_id = run_data["id"]

        # Manually update status to pass1_running in DB
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            run.status = RunStatus.PASS1_RUNNING
            session.add(run)
            session.commit()

        # Mock get_sheet_rows with valid rows so it reaches the status validation check
        mock_rows = [
            {"team_id": "T1", "team_name": "Team A", "track": "new_idea", "theme": "AI", "idea": "Idea", "resume_urls": "http://r.pdf", "project_links": "http://p"}
        ]

        # Try to ingest -> should get 409
        with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
            response = client.post(f"/api/runs/{run_id}/ingest")
        assert response.status_code == 409
        assert "Cannot ingest: run is in status 'pass1_running'" in response.json()["detail"]


def test_ingest_no_readable_team_id():
    """Sheet with rows but no readable team_id should return 400 and not change run status."""
    with TestClient(app) as client:
        # Create a run
        run_data = client.post("/api/runs").json()
        run_id = run_data["id"]

        # Mock get_sheet_rows to return rows with no team_id
        mock_rows = [
            {"team_id": "", "team_name": "Team A", "track": "new_idea", "theme": "AI", "idea": "Idea", "resume_urls": "http://r.pdf", "project_links": "http://p"},
            {"team_id": "   ", "team_name": "Team B", "track": "new_idea", "theme": "AI", "idea": "Idea 2", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
        ]

        with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
            response = client.post(f"/api/runs/{run_id}/ingest")

        # Should get 400
        assert response.status_code == 400
        assert "No team_id could be read from any row" in response.json()["detail"]

        # Verify run status is still CREATED (not INGESTED)
        run_check = client.get(f"/api/runs/{run_id}").json()
        assert run_check["status"] == "created"


def test_ingest_case_insensitive_headers_api():
    """API-level test: headers with odd casing and spacing should work."""
    with TestClient(app) as client:
        # Create a run
        run_data = client.post("/api/runs").json()
        run_id = run_data["id"]

        # Mock get_sheet_rows with odd-cased headers
        mock_rows = [
            {
                "Team_ID ": "API-T1",
                "team_name": "API Team A",
                "TRACK": "new_idea",
                " Idea": "Great API idea",
                "theme": "AI",
                "resume_urls": "http://resume-api.pdf",
                "project_links": "http://project-api.com"
            },
        ]

        with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
            ingest_response = client.post(f"/api/runs/{run_id}/ingest")

        # Should succeed
        assert ingest_response.status_code == 200
        ingest_data = ingest_response.json()
        assert ingest_data["teams_in_sheet"] == 1
        assert ingest_data["complete"] == 1

        # Verify team exists via GET /api/teams
        teams_response = client.get(f"/api/teams?run_id={run_id}")
        assert teams_response.status_code == 200
        teams = teams_response.json()
        assert len(teams) == 1
        assert teams[0]["team_id"] == "API-T1"
        assert teams[0]["track"] == "new_idea"
        assert teams[0]["idea"] == "Great API idea"
        assert teams[0]["team_name"] == "API Team A"
        assert teams[0]["status"] == "P1_QUEUED"

        # Also verify directly in database
        with next(get_session()) as session:
            team = session.exec(
                select(Team).where(Team.run_id == run_id, Team.team_id == "API-T1")
            ).first()
            assert team is not None
            assert team.team_id == "API-T1"
            assert team.track == "new_idea"
            assert team.idea == "Great API idea"
            assert team.team_name == "API Team A"

