"""Comprehensive tests for Pass 2 evaluation functionality."""
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.database import get_session
from app.models import Run, Team, RunStatus, TeamStatus
from app.ai_client import AIServiceError


def create_run_with_promoted_teams(client, team_ids: list[str]) -> int:
    """Helper: create a run, ingest teams, run Pass-1, and promote them."""
    run_data = client.post("/api/runs").json()
    run_id = run_data["id"]

    # Mock sheet rows
    mock_rows = [
        {
            "team_id": tid,
            "team_name": f"Team {tid}",
            "track": "new_idea",
            "theme": "AI",
            "idea": f"Idea for {tid}",
            "resume_urls": f"http://{tid}.pdf",
            "project_links": f"http://{tid}.com"
        }
        for tid in team_ids
    ]

    # Ingest teams
    with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
        client.post(f"/api/runs/{run_id}/ingest")

    # Mock Pass-1 scoring to promote all teams
    def mock_score_batch(teams):
        return [
            {
                "team_id": t["team_id"],
                "problem_clarity": 8.0,
                "originality": 8.0,
                "execution": 8.0,
                "feasibility": 8.0,
                "articulation": 8.0,
                "composite": 0.20*8.0 + 0.25*8.0 + 0.30*8.0 + 0.10*8.0 + 0.15*8.0,
                "reasons": {},
                "reason_codes": []
            }
            for t in teams
        ]

    # Run Pass-1 to completion
    with patch("app.routers.runs.ai_client.check_ready"):
        with patch("app.pass1_runner.ai_client.score_batch", side_effect=mock_score_batch):
            client.post(f"/api/runs/{run_id}/pass1")

    # Wait for Pass-1 to complete by querying directly
    from app.database import engine
    with Session(engine) as session:
        # Set teams to P1_DONE with FAST_TRACK band
        teams = session.exec(
            select(Team).where(Team.run_id == run_id)
        ).all()
        for team in teams:
            team.p1_composite = 8.0
            team.p1_band = "FAST_TRACK"
            team.p1_problem_clarity = 8.0
            team.p1_originality = 8.0
            team.p1_execution = 8.0
            team.p1_feasibility = 8.0
            team.p1_articulation = 8.0
            team.p1_reasons = "{}"
            team.p1_reason_codes = "[]"
            team.status = TeamStatus.P1_DONE
            session.add(team)
        run = session.get(Run, run_id)
        run.status = RunStatus.PASS1_DONE
        session.add(run)
        session.commit()

    return run_id


def mock_pass2_batch_success(teams: list[dict]) -> list[dict]:
    """Mock pass2_batch that returns valid Pass2RankedTeam results."""
    return [
        {
            "team": t,
            "critiques": {
                "theme": {
                    "team_id": t["team_id"],
                    "crowding_risk": 3.0,
                    "clone_risk": 2.0,
                    "theme_fit_notes": "Good theme fit",
                    "flags": [],
                    "summary": "Theme looks good"
                },
                "builder": {
                    "team_id": t["team_id"],
                    "evidence_score": 8.0,
                    "github_findings": "Strong GitHub presence at https://github.com/team",
                    "portfolio_findings": "Good portfolio at https://portfolio.com",
                    "flags": [],
                    "summary": "Strong builder credentials"
                },
                "integrity": {
                    "team_id": t["team_id"],
                    "flags": [],
                    "similar_teams": [],
                    "summary": "No integrity concerns"
                }
            },
            "judge": {
                "team_id": t["team_id"],
                "recommendation": "strong_yes",
                "score": 9.0,
                "summary": "Excellent team and idea",
                "flags": []
            },
            "rank": 1
        }
        for t in teams
    ]


def mock_pass2_one_success(team: dict) -> dict:
    """Mock pass2_one for individual retries."""
    return mock_pass2_batch_success([team])[0]


def test_pass2_happy_path():
    """Happy path: promoted teams scored and saved correctly, final_rank computed."""
    with TestClient(app) as client:
        run_id = create_run_with_promoted_teams(client, ["T1", "T2", "T3"])

        # Mock AI service and start Pass-2
        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.pass2_runner.ai_client.pass2_batch", side_effect=mock_pass2_batch_success):
                response = client.post(f"/api/runs/{run_id}/pass2")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["status"] == "pass2_running"
        assert data["teams_to_score"] == 3

        # Query database to verify results were saved
        from app.database import engine
        with Session(engine) as session:
            teams = session.exec(
                select(Team).where(Team.run_id == run_id)
            ).all()

            # All teams should have p2_score set
            for team in teams:
                assert team.p2_score == 9.0
                assert team.p2_recommendation == "strong_yes"
                assert team.p2_verdict == "Excellent team and idea"
                assert team.p2_critiques is not None
                assert team.status == TeamStatus.P2_DONE

            # Check final_rank was computed (tie-break by p1_composite desc)
            ranked = sorted(teams, key=lambda t: t.final_rank or 999)
            assert len(ranked) == 3
            assert ranked[0].final_rank == 1
            assert ranked[1].final_rank == 2
            assert ranked[2].final_rank == 3

            # Run should be PASS2_DONE
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS2_DONE


def test_pass2_batch_failure_retries_individually():
    """Batch fails with timeout, pass2_one retries: T1 and T3 succeed, T2 fails.

    Setup: 3 promoted teams T1, T2, T3. pass2_batch ALWAYS raises timeout.
    pass2_one succeeds for T1 and T3, raises http_error for T2.
    Assert: T1 and T3 have p2_score set, T2 has p2_score None and error set,
    run.status == PASS2_INCOMPLETE.
    """
    from app.database import engine

    with TestClient(app) as client:
        run_id = create_run_with_promoted_teams(client, ["T1", "T2", "T3"])

        def mock_batch_always_fails(teams):
            # Batch call always fails with timeout
            raise AIServiceError("Timeout", kind="timeout")

        def mock_individual_selective(team):
            # T1 succeeds, T2 fails with http_error, T3 succeeds
            if team["team_id"] == "T2":
                raise AIServiceError("HTTP error", kind="http_error")
            return mock_pass2_one_success(team)

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.pass2_runner.ai_client.pass2_batch", side_effect=mock_batch_always_fails):
                with patch("app.pass2_runner.ai_client.pass2_one", side_effect=mock_individual_selective):
                    response = client.post(f"/api/runs/{run_id}/pass2")

        assert response.status_code == 200

        # Query to verify selective scoring
        with Session(engine) as session:
            teams = session.exec(
                select(Team).where(Team.run_id == run_id).order_by(Team.team_id)
            ).all()

            assert len(teams) == 3

            # T1 should be scored
            t1 = next(t for t in teams if t.team_id == "T1")
            assert t1.p2_score == 9.0
            assert t1.error is None

            # T2 should have error, no score
            t2 = next(t for t in teams if t.team_id == "T2")
            assert t2.p2_score is None
            assert t2.error is not None
            assert "AI service error" in t2.error

            # T3 should be scored
            t3 = next(t for t in teams if t.team_id == "T3")
            assert t3.p2_score == 9.0
            assert t3.error is None

            # Run should be PASS2_INCOMPLETE (1 team failed)
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS2_INCOMPLETE
            assert "1 promoted team(s) failed" in run.error


def test_pass2_double_post_returns_409():
    """Double POST /pass2 while already running returns 409."""
    from app.database import engine

    with TestClient(app) as client:
        run_id = create_run_with_promoted_teams(client, ["T1"])

        # Manually set run to pass2_running to simulate an active run
        with Session(engine) as session:
            run = session.get(Run, run_id)
            run.status = RunStatus.PASS2_RUNNING
            session.add(run)
            session.commit()

        # Second POST should return 409
        with patch("app.routers.runs.ai_client.check_ready"):
            response = client.post(f"/api/runs/{run_id}/pass2")
        assert response.status_code == 409
        assert "pass2_running" in response.json()["detail"]


def test_pass2_unreachable_ai_stops_run():
    """Unreachable AI stops the run (status INTERRUPTED)."""
    with TestClient(app) as client:
        run_id = create_run_with_promoted_teams(client, ["T1"])

        def mock_batch_unreachable(teams):
            raise AIServiceError("Unreachable", kind="unreachable")

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.pass2_runner.ai_client.pass2_batch", side_effect=mock_batch_unreachable):
                response = client.post(f"/api/runs/{run_id}/pass2")

        assert response.status_code == 200

        # Query to verify run status
        from app.database import engine
        with Session(engine) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.INTERRUPTED
            assert "unreachable" in run.error.lower() or "AI service error" in run.error


def test_pass2_interrupted_resume():
    """A run left pass2_running becomes interrupted on startup, resuming only scores unscored teams.

    This test verifies TWO things:
    1. At startup (lifespan), a run in pass2_running state is marked INTERRUPTED
    2. When resumed, only teams still missing p2_score are re-scored
    """
    from app.database import engine
    from app.main import app as test_app

    # Create a run that's stuck in pass2_running
    with Session(engine) as session:
        run = Run(status=RunStatus.PASS2_RUNNING)
        session.add(run)
        session.commit()
        run_id = run.id

        # Add teams: 3 promoted, 1 already scored
        for i, team_id in enumerate(["T1", "T2", "T3"]):
            team = Team(
                run_id=run_id,
                team_id=team_id,
                team_name=f"Team {team_id}",
                track="new_idea",
                idea=f"Idea {i}",
                p1_composite=8.0,
                p1_problem_clarity=8.0,
                p1_originality=8.0,
                p1_execution=8.0,
                p1_feasibility=8.0,
                p1_articulation=8.0,
                p1_reasons="{}",
                p1_reason_codes="[]",
                p1_band="FAST_TRACK",
                status=TeamStatus.P1_DONE,
            )
            # T1 is already scored
            if team_id == "T1":
                team.p2_score = 7.0
                team.p2_recommendation = "yes"
                team.p2_verdict = "Good"
                team.p2_critiques = '{"theme": {}}'
                team.status = TeamStatus.P2_DONE
            session.add(team)
        session.commit()

    # Verify pre-condition: run is in pass2_running
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.PASS2_RUNNING

    # Simulate server startup: lifespan recovery should mark pass2_running as INTERRUPTED
    with TestClient(test_app) as client:
        # After TestClient initialization, lifespan has run and should have marked the run
        with Session(engine) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.INTERRUPTED
            assert "Pass-2" in run.error

    # Now resume: manually call run_pass2 to simulate resuming from interrupted state
    # It should only score teams still missing p2_score (T2, T3)
    call_count = [0]

    def mock_pass2_batch(teams):
        call_count[0] += 1
        # Should only be called with 2 teams (T2, T3), not T1
        assert len(teams) == 2, f"Expected 2 teams, got {len(teams)}"
        team_ids = {t["team_id"] for t in teams}
        assert "T1" not in team_ids, "T1 should not be re-scored"
        assert team_ids == {"T2", "T3"}
        return mock_pass2_batch_success(teams)

    with patch("app.pass2_runner.ai_client.pass2_batch", side_effect=mock_pass2_batch):
        from app.pass2_runner import run_pass2
        # Manually call run_pass2 to simulate resuming
        run_pass2(run_id)

    # Verify results: only unscored teams were processed
    with Session(engine) as session:
        teams = session.exec(
            select(Team).where(Team.run_id == run_id).order_by(Team.team_id)
        ).all()

        # T1 should still have original score (not re-scored)
        t1 = next(t for t in teams if t.team_id == "T1")
        assert t1.p2_score == 7.0
        assert t1.error is None

        # T2 and T3 should be newly scored
        t2 = next(t for t in teams if t.team_id == "T2")
        t3 = next(t for t in teams if t.team_id == "T3")
        assert t2.p2_score == 9.0
        assert t3.p2_score == 9.0

        # Run should now be PASS2_DONE (all promoted teams scored)
        run = session.get(Run, run_id)
        assert run.status == RunStatus.PASS2_DONE

    # Verify pass2_batch was called exactly once with correct teams
    assert call_count[0] == 1
