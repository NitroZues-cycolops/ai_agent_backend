"""Pass 1 background scoring runner."""
import json
import logging
import time
from urllib.parse import urlparse

from sqlmodel import Session, select

from app import ai_client
from app.ai_client import AIServiceError
from app.bands import compute_bands
from app.config import PASS1_BATCH_SIZE
from app.database import engine
from app.models import Run, RunStatus, Team, TeamStatus, utcnow

logger = logging.getLogger(__name__)


def extract_github_url(project_links: str | None) -> str | None:
    """Extract the first GitHub URL from project_links."""
    if not project_links:
        return None

    links = [link.strip() for link in project_links.split(",")]
    for link in links:
        if not link:
            continue
        try:
            parsed = urlparse(link)
            if parsed.hostname and "github.com" in parsed.hostname.lower():
                return link
        except Exception:
            continue
    return None


def extract_portfolio_url(project_links: str | None, github_url: str | None) -> str | None:
    """Extract the first non-GitHub URL from project_links."""
    if not project_links:
        return None

    links = [link.strip() for link in project_links.split(",")]
    for link in links:
        if not link or link == github_url:
            continue
        try:
            parsed = urlparse(link)
            if parsed.hostname and "github.com" not in parsed.hostname.lower():
                return link
        except Exception:
            continue
    return None


def build_team_input(team: Team) -> dict:
    """Build TeamInput dict for the AI service from a Team model."""
    github_url = extract_github_url(team.project_links)
    portfolio_url = extract_portfolio_url(team.project_links, github_url)

    return {
        "team_id": team.team_id,
        "track": team.track,
        "idea_text": team.idea,
        "team_name": team.team_name,
        "github_url": github_url,
        "portfolio_url": portfolio_url,
    }


def validate_score_response(sent_teams: list[dict], scores: list[dict]) -> tuple[bool, str | None]:
    """Validate AI service response against sent teams.

    Returns:
        (is_valid, error_message)
    """
    sent_ids = {t["team_id"] for t in sent_teams}

    # Check we got exactly the teams we sent
    if len(scores) != len(sent_teams):
        return False, f"Expected {len(sent_teams)} scores, got {len(scores)}"

    score_ids = []
    for score in scores:
        team_id = score.get("team_id")
        if not team_id:
            return False, "Score missing team_id"
        score_ids.append(team_id)

    # Check for duplicates
    if len(score_ids) != len(set(score_ids)):
        return False, "Duplicate team_ids in response"

    # Check all sent teams are present
    score_id_set = set(score_ids)
    if score_id_set != sent_ids:
        missing = sent_ids - score_id_set
        extra = score_id_set - sent_ids
        msg_parts = []
        if missing:
            msg_parts.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            msg_parts.append(f"unexpected {', '.join(sorted(extra))}")
        return False, f"Team ID mismatch: {'; '.join(msg_parts)}"

    # Validate each score's dimensions
    for score in scores:
        team_id = score["team_id"]

        # Check all dimensions present and in range
        for dim in ["problem_clarity", "originality", "execution", "feasibility", "articulation"]:
            val = score.get(dim)
            if val is None:
                return False, f"{team_id}: missing dimension '{dim}'"
            if not isinstance(val, (int, float)):
                return False, f"{team_id}: '{dim}' is not numeric"
            if not (0 <= val <= 10):
                return False, f"{team_id}: '{dim}' = {val} not in [0, 10]"

        # Check composite
        composite = score.get("composite")
        if composite is None:
            return False, f"{team_id}: missing composite"
        if not isinstance(composite, (int, float)):
            return False, f"{team_id}: composite is not numeric"

        # Validate composite formula: 0.20*clarity + 0.25*originality + 0.30*execution + 0.10*feasibility + 0.15*articulation
        expected = (
            0.20 * score["problem_clarity"]
            + 0.25 * score["originality"]
            + 0.30 * score["execution"]
            + 0.10 * score["feasibility"]
            + 0.15 * score["articulation"]
        )
        if abs(composite - expected) > 0.011:
            return False, f"{team_id}: composite {composite} != expected {expected:.3f} (diff {abs(composite - expected):.4f})"

    return True, None


def score_teams_batch(session: Session, teams: list[Team]) -> tuple[list[str], list[str]]:
    """Score a batch of teams using the AI service.

    Returns:
        (scored_ids, failed_ids)
    """
    team_inputs = [build_team_input(t) for t in teams]

    start = time.time()
    try:
        scores = ai_client.score_batch(team_inputs)
        duration = time.time() - start
        logger.info("score_batch(%d teams) completed in %.2fs", len(teams), duration)
    except AIServiceError as exc:
        duration = time.time() - start
        logger.error("score_batch(%d teams) failed after %.2fs: %s", len(teams), duration, exc)
        raise

    # Validate response
    is_valid, error_msg = validate_score_response(team_inputs, scores)
    if not is_valid:
        logger.error("Invalid AI response: %s", error_msg)
        # Mark all teams as failed
        failed_ids = []
        for team in teams:
            team.error = f"AI response validation failed: {error_msg}"
            session.add(team)
            failed_ids.append(team.team_id)
        session.commit()
        return [], failed_ids

    # Save scores
    scores_by_id = {s["team_id"]: s for s in scores}
    scored_ids = []

    for team in teams:
        score = scores_by_id[team.team_id]
        team.p1_problem_clarity = score["problem_clarity"]
        team.p1_originality = score["originality"]
        team.p1_execution = score["execution"]
        team.p1_feasibility = score["feasibility"]
        team.p1_articulation = score["articulation"]
        team.p1_composite = score["composite"]
        team.p1_reasons = json.dumps(score.get("reasons", {}))
        team.p1_reason_codes = json.dumps(score.get("reason_codes", []))
        team.status = TeamStatus.P1_DONE
        team.error = None
        team.updated_at = utcnow()
        session.add(team)
        scored_ids.append(team.team_id)

    session.commit()
    return scored_ids, []


def score_team_individually(session: Session, team: Team) -> bool:
    """Score one team individually using score_one.

    Returns:
        True if scored successfully, False if failed
    """
    team_input = build_team_input(team)

    start = time.time()
    try:
        score = ai_client.score_one(team_input)
        duration = time.time() - start
        logger.info("score_one(%s) completed in %.2fs", team.team_id, duration)
    except AIServiceError as exc:
        duration = time.time() - start
        logger.error("score_one(%s) failed after %.2fs: %s", team.team_id, duration, exc)
        team.error = f"AI service error: {str(exc)}"
        session.add(team)
        session.commit()
        return False

    # Validate (treat as single-item batch)
    is_valid, error_msg = validate_score_response([team_input], [score])
    if not is_valid:
        logger.error("Invalid AI response for %s: %s", team.team_id, error_msg)
        team.error = f"AI response validation failed: {error_msg}"
        session.add(team)
        session.commit()
        return False

    # Save score
    team.p1_problem_clarity = score["problem_clarity"]
    team.p1_originality = score["originality"]
    team.p1_execution = score["execution"]
    team.p1_feasibility = score["feasibility"]
    team.p1_articulation = score["articulation"]
    team.p1_composite = score["composite"]
    team.p1_reasons = json.dumps(score.get("reasons", {}))
    team.p1_reason_codes = json.dumps(score.get("reason_codes", []))
    team.status = TeamStatus.P1_DONE
    team.error = None
    team.updated_at = utcnow()
    session.add(team)
    session.commit()
    return True


def run_pass1(run_id: int) -> None:
    """Background task: score all pending teams for a run."""
    logger.info("Pass 1 runner started for run_id=%d", run_id)

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error("Run %d not found", run_id)
            return

        try:
            # Reset any prior errors on unscored teams for this run (e.g. when resuming)
            unscored_teams = session.exec(
                select(Team).where(
                    Team.run_id == run_id,
                    Team.p1_composite == None,
                )
            ).all()
            for t in unscored_teams:
                t.error = None
                session.add(t)
            session.commit()

            # Process batches of unscored teams
            while True:
                pending_teams = session.exec(
                    select(Team)
                    .where(
                        Team.run_id == run_id,
                        Team.status == TeamStatus.P1_QUEUED,
                        Team.p1_composite == None,
                        Team.error == None,
                    )
                    .limit(PASS1_BATCH_SIZE)
                ).all()

                if not pending_teams:
                    break

                logger.info("Scoring batch of %d teams", len(pending_teams))

                try:
                    scored, failed = score_teams_batch(session, pending_teams)
                    logger.info("Batch result: %d scored, %d failed", len(scored), len(failed))
                except AIServiceError as exc:
                    # Batch-level failure: retry individually if timeout/http_error
                    if exc.kind in ("timeout", "http_error"):
                        logger.info("Batch failed with %s, retrying teams individually", exc.kind)
                        for team in pending_teams:
                            success = score_team_individually(session, team)
                            logger.info("Team %s: %s", team.team_id, "scored" if success else "failed")
                    else:
                        # unreachable or no_llm_key: abort the run
                        logger.error("Fatal AI error (%s), stopping run", exc.kind)
                        run.status = RunStatus.INTERRUPTED
                        run.error = f"AI service error: {str(exc)}"
                        session.add(run)
                        session.commit()
                        return

            # Check if any teams failed
            failed_teams = session.exec(
                select(Team)
                .where(
                    Team.run_id == run_id,
                    Team.status == TeamStatus.P1_QUEUED,
                    Team.error != None,
                )
            ).all()

            if failed_teams:
                logger.warning("Pass 1 incomplete: %d teams failed", len(failed_teams))
                run.status = RunStatus.PASS1_INCOMPLETE
                run.error = f"{len(failed_teams)} team(s) failed scoring"
                session.add(run)
                session.commit()
                return

            # All teams scored: compute bands
            logger.info("All teams scored, computing bands")
            scored_complete_teams = session.exec(
                select(Team)
                .where(
                    Team.run_id == run_id,
                    Team.status != TeamStatus.INCOMPLETE,
                    Team.p1_composite != None,
                )
            ).all()

            team_scores = [
                {"team_id": t.team_id, "p1_composite": t.p1_composite}
                for t in scored_complete_teams
            ]
            bands = compute_bands(team_scores)

            for team in scored_complete_teams:
                team.p1_band = bands[team.team_id]
                if team.p1_band == "REJECT":
                    team.status = TeamStatus.REJECT
                # else stays P1_DONE
                session.add(team)

            run.status = RunStatus.PASS1_DONE
            run.error = None
            session.add(run)
            session.commit()
            logger.info("Pass 1 completed for run_id=%d", run_id)

        except Exception as exc:
            logger.exception("Pass 1 runner crashed for run_id=%d", run_id)
            run.status = RunStatus.INTERRUPTED
            run.error = f"Internal error: {type(exc).__name__}"
            session.add(run)
            session.commit()
