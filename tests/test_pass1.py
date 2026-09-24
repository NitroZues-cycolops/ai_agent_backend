"""Comprehensive tests for Pass 1 scoring functionality."""
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.database import get_session
from app.models import Run, Team, RunStatus, TeamStatus
from app.ai_client import AIServiceError


def create_run_with_teams(client, team_ids: list[str]) -> int:
    """Helper: create a run and ingest teams with the given team_ids."""
    run_data = client.post("/api/runs").json()
    run_id = run_data["id"]

    # Mock sheet rows for these teams
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

    with patch("app.routers.runs.get_sheet_rows", return_value=mock_rows):
        client.post(f"/api/runs/{run_id}/ingest")

    return run_id


def mock_score_batch_success(teams: list[dict]) -> list[dict]:
    """Mock score_batch that returns valid scores."""
    return [
        {
            "team_id": t["team_id"],
            "problem_clarity": 7.0,
            "originality": 8.0,
            "execution": 6.0,
            "feasibility": 7.5,
            "articulation": 7.0,
            "composite": 0.20*7.0 + 0.25*8.0 + 0.30*6.0 + 0.10*7.5 + 0.15*7.0,
            "reasons": {"clarity": "Good problem definition"},
            "reason_codes": ["well_defined"]
        }
        for t in teams
    ]


def mock_score_one_success(team: dict) -> dict:
    """Mock score_one that returns a valid score."""
    return {
        "team_id": team["team_id"],
        "problem_clarity": 5.0,
        "originality": 6.0,
        "execution": 5.5,
        "feasibility": 6.0,
        "articulation": 5.0,
        "composite": 0.20*5.0 + 0.25*6.0 + 0.30*5.5 + 0.10*6.0 + 0.15*5.0,
        "reasons": {"clarity": "Average problem definition"},
        "reason_codes": ["adequate"]
    }


def test_pass1_post_returns_immediately():
    """POST /runs/{id}/pass1 should return immediately with teams_to_score count."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2", "T3"])

        # Mock AI service health check
        with patch("app.routers.runs.ai_client.check_ready"):
            response = client.post(f"/api/runs/{run_id}/pass1")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["status"] == "pass1_running"
        assert data["teams_to_score"] == 3


def test_pass1_post_503_when_ai_down():
    """POST /runs/{id}/pass1 should return 503 if AI service has no LLM key."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1"])

        # Mock AI service down
        with patch("app.routers.runs.ai_client.check_ready", side_effect=AIServiceError("No LLM key", kind="no_llm_key")):
            response = client.post(f"/api/runs/{run_id}/pass1")

        assert response.status_code == 503
        assert "No LLM key" in response.json()["detail"]


def test_pass1_post_409_on_wrong_status():
    """POST /runs/{id}/pass1 should return 409 if run is not in allowed status."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1"])

        # Manually set run to pass1_done
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            run.status = RunStatus.PASS1_DONE
            session.add(run)
            session.commit()

        with patch("app.routers.runs.ai_client.check_ready"):
            response = client.post(f"/api/runs/{run_id}/pass1")

        assert response.status_code == 409
        assert "Cannot start Pass 1" in response.json()["detail"]


def test_pass1_post_double_post_409():
    """Second POST while pass1_running should return 409."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1"])

        # Prevent background task from completing immediately by mocking run_pass1
        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.pass1_runner.run_pass1"):
                response1 = client.post(f"/api/runs/{run_id}/pass1")
                assert response1.status_code == 200

                # Second POST while run is still in pass1_running state
                response2 = client.post(f"/api/runs/{run_id}/pass1")
                assert response2.status_code == 409
                assert "Cannot start Pass 1: run is in status 'pass1_running'" in response2.json()["detail"]


def test_pass1_batch_success():
    """End-to-end: successful batch scoring sets status to pass1_done."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Mock successful AI calls
        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=mock_score_batch_success):
                response = client.post(f"/api/runs/{run_id}/pass1")
                assert response.status_code == 200

        # Wait for background task by checking run status
        import time
        for _ in range(50):  # 5 seconds max
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify final state
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS1_DONE

            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            assert all(t.status in (TeamStatus.P1_DONE, TeamStatus.REJECT) for t in teams)
            assert all(t.p1_composite is not None for t in teams)
            assert all(t.p1_band is not None for t in teams)


def test_pass1_batch_failure_retries_individually():
    """When batch fails with timeout, teams should be retried individually."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Mock: batch times out, individual calls succeed
        batch_call_count = [0]
        def mock_batch_timeout(teams):
            batch_call_count[0] += 1
            raise AIServiceError("Timeout", kind="timeout")

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=mock_batch_timeout):
                with patch("app.ai_client.score_one", side_effect=mock_score_one_success):
                    response = client.post(f"/api/runs/{run_id}/pass1")
                    assert response.status_code == 200

        # Wait for completion
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify: batch was attempted, then individual scores succeeded
        assert batch_call_count[0] == 1

        with next(get_session()) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS1_DONE

            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            assert all(t.p1_composite is not None for t in teams)


def test_pass1_unreachable_ai_stops_run():
    """When AI is unreachable, run should stop with status interrupted."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Mock: AI service unreachable
        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=AIServiceError("Unreachable", kind="unreachable")):
                response = client.post(f"/api/runs/{run_id}/pass1")
                assert response.status_code == 200

        # Wait for runner to stop
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify: run stopped with interrupted status
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.INTERRUPTED
            assert "AI service error" in run.error


def test_pass1_invalid_response_marks_teams_failed():
    """Invalid AI response should mark all teams in batch as failed."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Mock: batch returns invalid response (missing team_id)
        def mock_invalid_batch(teams):
            return [{"problem_clarity": 7.0}]  # Missing team_id and other fields

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=mock_invalid_batch):
                response = client.post(f"/api/runs/{run_id}/pass1")
                assert response.status_code == 200

        # Wait for completion
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify: teams marked as failed (validation failure doesn't retry individually)
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            # Invalid response marks batch as failed, leads to PASS1_INCOMPLETE
            assert run.status == RunStatus.PASS1_INCOMPLETE

            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            # All teams should have error set
            assert all(t.error is not None for t in teams)
            assert "AI response validation failed" in teams[0].error


def test_pass1_interrupted_resume():
    """A run in interrupted status can be resumed with another POST."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Manually set to interrupted
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            run.status = RunStatus.INTERRUPTED
            session.add(run)
            session.commit()

        # Resume with another POST
        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=mock_score_batch_success):
                response = client.post(f"/api/runs/{run_id}/pass1")
                assert response.status_code == 200

        # Wait for completion
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        with next(get_session()) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS1_DONE


def test_pass1_tie_handling_in_bands():
    """Teams with identical composite scores should get the same band."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2", "T3", "T4", "T5"])

        # Mock: return specific scores to create ties at boundaries
        def mock_batch_with_ties(teams):
            scores = {
                "T1": 9.0,
                "T2": 7.0,
                "T3": 7.0,  # Tie with T2
                "T4": 5.0,
                "T5": 3.0
            }
            return [
                {
                    "team_id": t["team_id"],
                    "problem_clarity": scores[t["team_id"]],
                    "originality": scores[t["team_id"]],
                    "execution": scores[t["team_id"]],
                    "feasibility": scores[t["team_id"]],
                    "articulation": scores[t["team_id"]],
                    "composite": scores[t["team_id"]],
                    "reasons": {},
                    "reason_codes": []
                }
                for t in teams
            ]

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=mock_batch_with_ties):
                client.post(f"/api/runs/{run_id}/pass1")

        # Wait for completion
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify: T2 and T3 have same band
        with next(get_session()) as session:
            t2 = session.exec(select(Team).where(Team.run_id == run_id, Team.team_id == "T2")).first()
            t3 = session.exec(select(Team).where(Team.run_id == run_id, Team.team_id == "T3")).first()
            assert t2.p1_band == t3.p1_band


def test_pass1_bands_not_set_with_failures():
    """Bands should not be computed if any team has failed."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2"])

        # Mock: first team succeeds, second fails
        call_count = [0]
        def mock_one_with_failure(team):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_score_one_success(team)
            else:
                raise AIServiceError("Failed", kind="http_error", status_code=500)

        with patch("app.routers.runs.ai_client.check_ready"):
            with patch("app.ai_client.score_batch", side_effect=AIServiceError("Timeout", kind="timeout")):
                with patch("app.ai_client.score_one", side_effect=mock_one_with_failure):
                    client.post(f"/api/runs/{run_id}/pass1")

        # Wait for completion
        import time
        for _ in range(50):
            with next(get_session()) as session:
                run = session.get(Run, run_id)
                if run.status != RunStatus.PASS1_RUNNING:
                    break
            time.sleep(0.1)

        # Verify: run is pass1_incomplete, no bands set
        with next(get_session()) as session:
            run = session.get(Run, run_id)
            assert run.status == RunStatus.PASS1_INCOMPLETE

            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            assert any(t.p1_band is None for t in teams)


def test_pass1_get_returns_progress():
    """GET /runs/{id}/pass1 should return progress and team details."""
    with TestClient(app) as client:
        run_id = create_run_with_teams(client, ["T1", "T2", "T3"])

        # Score some teams manually
        with next(get_session()) as session:
            teams = session.exec(select(Team).where(Team.run_id == run_id)).all()
            teams[0].p1_composite = 8.0
            teams[0].p1_band = "FAST_TRACK"
            teams[0].status = TeamStatus.P1_DONE
            teams[1].p1_composite = 6.0
            teams[1].p1_band = "BORDERLINE"
            teams[1].status = TeamStatus.P1_DONE
            # teams[2] left pending
            session.add_all(teams)
            session.commit()

        # GET status
        response = client.get(f"/api/runs/{run_id}/pass1")
        assert response.status_code == 200

        data = response.json()
        assert data["run_id"] == run_id
        assert data["progress"]["total"] == 3
        assert data["progress"]["scored"] == 2
        assert data["progress"]["pending"] == 1
        assert data["progress"]["failed"] == 0
        assert data["bands_final"] is False  # Still have pending
        assert data["band_counts"]["FAST_TRACK"] == 1
        assert data["band_counts"]["BORDERLINE"] == 1
        assert data["band_counts"]["REJECT"] == 0
        assert len(data["teams"]) == 2  # Only scored teams
