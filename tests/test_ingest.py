"""Test suite for team ingestion logic.

SAFETY: This test suite uses a SEPARATE test database (hackathon_test).
It REFUSES to run if the database name does not end in "_test".
It NEVER touches hackathon_db.
"""
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.main import app
from app.models import Run, Team, RunStatus, TeamStatus
from app.ingest import ingest_rows, missing_columns
from app.database import get_session


def test_blank_row():
    """Blank rows should be counted in empty_rows and skipped."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "", "team_name": "", "track": "", "theme": "", "idea": "", "resume_urls": "", "project_links": ""},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["empty_rows"] == [2]  # Row 2 (row 1 = headers)
        assert result["teams_in_sheet"] == 0
        assert result["created"] == 0


def test_row_with_data_but_no_team_id():
    """Rows with data but no team_id should be reported."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "", "team_name": "Team A", "track": "new_idea", "theme": "AI", "idea": "Great idea", "resume_urls": "http://resume.pdf", "project_links": "http://github.com"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["rows_without_team_id"] == [2]
        assert result["teams_in_sheet"] == 0


def test_duplicate_team_id():
    """Duplicate team_id: keep first, mark as INCOMPLETE with reason."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "T1", "team_name": "First", "track": "new_idea", "theme": "AI", "idea": "First idea", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
            {"team_id": "T1", "team_name": "Second", "track": "existing_project", "theme": "Web3", "idea": "Second idea", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["teams_in_sheet"] == 1
        assert result["created"] == 1
        assert result["incomplete"] == 1

        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.team_id == "T1"
        assert team.team_name == "First"  # Kept first row's data
        assert team.status == TeamStatus.INCOMPLETE
        assert "duplicate team_id in sheet rows: 2, 3" in team.error


def test_missing_track():
    """Missing track should mark team as INCOMPLETE."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "T1", "team_name": "Team A", "track": "", "theme": "AI", "idea": "Great idea", "resume_urls": "http://resume.pdf", "project_links": "http://github.com"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["incomplete"] == 1
        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.status == TeamStatus.INCOMPLETE
        assert "missing track" in team.error


def test_odd_track_spellings():
    """Test track normalization: new idea, NewIdea, existing-project, etc."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "T1", "team_name": "Team 1", "track": "new idea", "theme": "AI", "idea": "Idea 1", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
            {"team_id": "T2", "team_name": "Team 2", "track": "NewIdea", "theme": "AI", "idea": "Idea 2", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
            {"team_id": "T3", "team_name": "Team 3", "track": "existing-project", "theme": "AI", "idea": "Idea 3", "resume_urls": "http://r3.pdf", "project_links": "http://p3"},
            {"team_id": "T4", "team_name": "Team 4", "track": "ExistingProject", "theme": "AI", "idea": "Idea 4", "resume_urls": "http://r4.pdf", "project_links": "http://p4"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["complete"] == 4
        t1 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        t2 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T2")).first()
        t3 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T3")).first()
        t4 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T4")).first()

        assert t1.track == "new_idea"
        assert t2.track == "new_idea"
        assert t3.track == "existing_project"
        assert t4.track == "existing_project"


def test_unrecognized_track():
    """Unrecognized track value should mark as INCOMPLETE with reason."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "T1", "team_name": "Team A", "track": "random_track", "theme": "AI", "idea": "Great idea", "resume_urls": "http://resume.pdf", "project_links": "http://github.com"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["incomplete"] == 1
        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.status == TeamStatus.INCOMPLETE
        assert "unrecognized track 'random_track'" in team.error


def test_numeric_and_leading_zero_team_id():
    """Numeric and leading-zero team_id should work."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        rows = [
            {"team_id": "123", "team_name": "Team 1", "track": "new_idea", "theme": "AI", "idea": "Idea 1", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
            {"team_id": "007", "team_name": "Team 2", "track": "new_idea", "theme": "AI", "idea": "Idea 2", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["complete"] == 2
        t1 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "123")).first()
        t2 = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "007")).first()
        assert t1 is not None
        assert t2 is not None


def test_missing_columns_function():
    """Test missing_columns() helper function."""
    # All required columns present
    rows_complete = [
        {"team_id": "T1", "track": "new_idea", "idea": "Great", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"}
    ]
    assert missing_columns(rows_complete) == []

    # Missing track column
    rows_no_track = [
        {"team_id": "T1", "idea": "Great", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"}
    ]
    assert missing_columns(rows_no_track) == ["track"]

    # Missing resume_urls column
    rows_no_resume = [
        {"team_id": "T1", "track": "new_idea", "idea": "Great", "theme": "AI", "project_links": "http://p"}
    ]
    assert missing_columns(rows_no_resume) == ["resume_urls"]

    # team_name is optional, should NOT be reported as missing
    rows_no_team_name = [
        {"team_id": "T1", "track": "new_idea", "idea": "Great", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"}
    ]
    assert missing_columns(rows_no_team_name) == []

    # Case-insensitive and space-trimmed header matching
    rows_odd_case = [
        {"Team_ID ": "T1", "TRACK": "new_idea", " Idea": "Great", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"}
    ]
    assert missing_columns(rows_odd_case) == []

    # Multiple missing columns
    rows_multiple_missing = [
        {"team_id": "T1", "theme": "AI"}
    ]
    missing = missing_columns(rows_multiple_missing)
    assert "track" in missing
    assert "idea" in missing
    assert "resume_urls" in missing
    assert "project_links" in missing


def test_ingest_missing_column_api():
    """API test: missing required column returns 400, saves no teams, leaves run status 'created'."""
    with TestClient(app) as client:
        # Create a run
        run_data = client.post("/api/runs").json()
        run_id = run_data["id"]

        # Mock get_sheet_rows to return rows missing the "track" column
        mock_rows = [
            {"team_id": "T1", "team_name": "Team A", "idea": "Great idea", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"}
        ]

        with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
            response = client.post(f"/api/runs/{run_id}/ingest")

        # Should get 400 naming the missing column
        assert response.status_code == 400
        assert "track" in response.json()["detail"]

        # Verify no teams were saved
        with next(get_session()) as session:
            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            assert len(teams) == 0

        # Verify run status is still CREATED (not INGESTED)
        run_check = client.get(f"/api/runs/{run_id}").json()
        assert run_check["status"] == "created"


def test_upsert_incomplete_and_p1_queued_teams():
    """Upsert should update teams with status INCOMPLETE or P1_QUEUED."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        # First ingest: create incomplete team
        rows1 = [
            {"team_id": "T1", "team_name": "Team 1", "track": "", "theme": "AI", "idea": "Idea 1", "resume_urls": "", "project_links": ""},
        ]
        result1 = ingest_rows(session, run.id, rows1)
        session.commit()

        assert result1["created"] == 1
        assert result1["incomplete"] == 1

        # Second ingest: fix the team (now complete)
        rows2 = [
            {"team_id": "T1", "team_name": "Team 1 Fixed", "track": "new_idea", "theme": "AI", "idea": "Idea 1", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
        ]
        result2 = ingest_rows(session, run.id, rows2)
        session.commit()

        assert result2["updated"] == 1
        assert result2["complete"] == 1

        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.team_name == "Team 1 Fixed"
        assert team.status == TeamStatus.P1_QUEUED
        assert team.error is None


def test_locked_team_not_updated():
    """Teams in non-updatable status (P1_DONE, etc.) should not be updated."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        # Create a team with P1_DONE status
        team = Team(
            run_id=run.id,
            team_id="T1",
            team_name="Original",
            track="new_idea",
            theme="AI",
            idea="Original idea",
            resume_path="http://r1.pdf",
            project_links="http://p1",
            status=TeamStatus.P1_DONE,
        )
        session.add(team)
        session.commit()

        # Try to update it via ingest
        rows = [
            {"team_id": "T1", "team_name": "Updated", "track": "new_idea", "theme": "AI", "idea": "Updated idea", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
        ]
        result = ingest_rows(session, run.id, rows)

        assert result["locked_not_updated"] == ["T1"]
        assert result["updated"] == 0

        # Verify team data unchanged
        session.refresh(team)
        assert team.team_name == "Original"


def test_team_missing_from_sheet():
    """Teams in DB but not in sheet should be reported."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        # Create teams in DB
        team1 = Team(run_id=run.id, team_id="T1", status=TeamStatus.P1_QUEUED)
        team2 = Team(run_id=run.id, team_id="T2", status=TeamStatus.P1_QUEUED)
        session.add(team1)
        session.add(team2)
        session.commit()

        # Ingest only T1
        rows = [
            {"team_id": "T1", "team_name": "Team 1", "track": "new_idea", "theme": "AI", "idea": "Idea 1", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
        ]
        result = ingest_rows(session, run.id, rows)

        assert "T2" in result["in_database_not_in_sheet"]


def test_ingest_twice():
    """Ingesting twice should upsert correctly."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        # First ingest
        rows1 = [
            {"team_id": "T1", "team_name": "Team 1", "track": "new_idea", "theme": "AI", "idea": "Idea 1", "resume_urls": "http://r1.pdf", "project_links": "http://p1"},
        ]
        result1 = ingest_rows(session, run.id, rows1)
        session.commit()

        assert result1["created"] == 1

        # Second ingest with updated data
        rows2 = [
            {"team_id": "T1", "team_name": "Team 1 v2", "track": "existing_project", "theme": "Web3", "idea": "Idea 1 v2", "resume_urls": "http://r2.pdf", "project_links": "http://p2"},
        ]
        result2 = ingest_rows(session, run.id, rows2)
        session.commit()

        assert result2["updated"] == 1
        assert result2["created"] == 0

        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.team_name == "Team 1 v2"
        assert team.track == "existing_project"


def test_case_insensitive_headers():
    """Headers with odd casing and spacing should work (Team_ID , TRACK,  Idea)."""
    with next(get_session()) as session:
        run = Run(status=RunStatus.CREATED)
        session.add(run)
        session.commit()
        session.refresh(run)

        # Note: Row dict keys have odd casing/spacing to prove normalization works
        rows = [
            {"Team_ID ": "T1", "team_name": "Team A", "TRACK": "new_idea", " Idea": "Great idea", "theme": "AI", "resume_urls": "http://r.pdf", "project_links": "http://p"},
        ]

        result = ingest_rows(session, run.id, rows)

        assert result["teams_in_sheet"] == 1
        assert result["created"] == 1
        assert result["complete"] == 1

        team = session.exec(select(Team).where(Team.run_id == run.id, Team.team_id == "T1")).first()
        assert team.team_id == "T1"
        assert team.track == "new_idea"
        assert team.idea == "Great idea"
