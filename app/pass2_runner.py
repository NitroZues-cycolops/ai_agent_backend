"""Pass 2 background runner for deep evaluation."""
import json
import logging
import time
import re
from urllib.parse import urlparse

from sqlmodel import Session, select

from app import ai_client
from app.ai_client import AIServiceError
from app.config import PASS2_BATCH_SIZE, PASS2_TIMEOUT
from app.database import engine
from app.models import Run, RunStatus, Team, TeamStatus, utcnow
from app.pass1_runner import extract_github_url, extract_portfolio_url

logger = logging.getLogger(__name__)


def build_p1_score_dict(team: Team) -> dict | None:
    """Build a P1Score dict from team's p1_* fields, or None if not scored.

    P1Score format matches ai/app/schemas/pass1.py:
    {team_id, track, clarity, originality, execution, feasibility, articulation,
     composite, reasons: {dict}, band, reason_codes: [list]}
    """
    if team.p1_composite is None:
        return None

    # Parse p1_reasons and p1_reason_codes from JSON, with fallback
    reasons = {}
    if team.p1_reasons:
        try:
            reasons = json.loads(team.p1_reasons)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Team %s: invalid p1_reasons JSON, using {}", team.team_id)
            reasons = {}

    reason_codes = []
    if team.p1_reason_codes:
        try:
            reason_codes = json.loads(team.p1_reason_codes)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Team %s: invalid p1_reason_codes JSON, using []", team.team_id)
            reason_codes = []

    return {
        "team_id": team.team_id,
        "track": team.track,
        "clarity": team.p1_problem_clarity,
        "originality": team.p1_originality,
        "execution": team.p1_execution,
        "feasibility": team.p1_feasibility,
        "articulation": team.p1_articulation,
        "composite": team.p1_composite,
        "reasons": reasons,
        "band": team.p1_band,
        "reason_codes": reason_codes,
    }


def build_peer_summaries(promoted_teams: list[Team], exclude_team_id: str) -> list[dict]:
    """Build peer_summaries list from promoted teams (excluding this team).

    Format: [{team_id, idea_summary}, ...]

    NOTE: we truncate idea to 250 chars as our own design choice.
    The ai/'s exact expectations for summary length/quality were not confirmed
    and should be validated against actual responses from the LLM.
    """
    summaries = []
    for team in promoted_teams:
        if team.team_id == exclude_team_id:
            continue
        idea_text = (team.idea or "")[:250]
        summaries.append({
            "team_id": team.team_id,
            "idea_summary": idea_text,
        })
    return summaries


def build_pass2_input(team: Team, peer_summaries: list[dict]) -> dict:
    """Build a Pass2TeamInput dict for the AI service from a Team model.

    Pass2TeamInput schema from ai/app/schemas/pass2.py:
    team_id, track, idea_text, resume_text, github_url, portfolio_url,
    team_name, theme, p1_score, peer_summaries
    """
    github_url = extract_github_url(team.project_links)
    portfolio_url = extract_portfolio_url(team.project_links, github_url)

    return {
        "team_id": team.team_id,
        "track": team.track,
        "idea_text": team.idea or "",
        "resume_text": team.resume_text,
        "github_url": github_url,
        "portfolio_url": portfolio_url,
        "team_name": team.team_name,
        "theme": team.theme,
        "p1_score": build_p1_score_dict(team),
        "peer_summaries": peer_summaries,
    }


def validate_pass2_response(sent_teams: list[dict], results: list[dict]) -> tuple[bool, str | None]:
    """Validate AI service response against sent teams.

    Returns:
        (is_valid, error_message)
    """
    # Handle None or non-list results
    if not isinstance(results, list):
        return False, f"Results is not a list: {type(results).__name__}"

    sent_ids = {t["team_id"] for t in sent_teams}

    # Check we got exactly the teams we sent
    if len(results) != len(sent_teams):
        return False, f"Expected {len(sent_teams)} results, got {len(results)}"

    result_ids = []
    for result in results:
        team_id = result.get("team", {}).get("team_id")
        if not team_id:
            return False, "Result missing team.team_id"
        result_ids.append(team_id)

    # Check for duplicates
    if len(result_ids) != len(set(result_ids)):
        return False, "Duplicate team_ids in response"

    # Check all sent teams are present
    result_id_set = set(result_ids)
    if result_id_set != sent_ids:
        missing = sent_ids - result_id_set
        extra = result_id_set - sent_ids
        msg_parts = []
        if missing:
            msg_parts.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            msg_parts.append(f"unexpected {', '.join(sorted(extra))}")
        return False, f"Team ID mismatch: {'; '.join(msg_parts)}"

    # Validate each result's judge field
    for result in results:
        team_id = result.get("team", {}).get("team_id")
        judge = result.get("judge", {})

        # Check judge.score in [0, 10]
        score = judge.get("score")
        if score is None:
            return False, f"{team_id}: missing judge.score"
        if not isinstance(score, (int, float)):
            return False, f"{team_id}: judge.score is not numeric"
        if not (0 <= score <= 10):
            return False, f"{team_id}: judge.score = {score} not in [0, 10]"

        # Check judge.recommendation in allowed values
        rec = judge.get("recommendation")
        if rec not in ("strong_yes", "yes", "borderline", "no"):
            return False, f"{team_id}: invalid recommendation '{rec}'"

    return True, None


def extract_evidence_links(critiques: dict) -> list[str]:
    """Extract URLs from critiques.builder findings.

    Known gap: ai/ response may not have a structured links list.
    We parse builder.github_findings and builder.portfolio_findings
    looking for HTTP(S) URLs.

    Returns: list of unique URL strings found, or empty list if none.
    """
    links = set()
    builder = critiques.get("builder", {})

    # Check github_findings and portfolio_findings for URLs
    for field in ("github_findings", "portfolio_findings"):
        text = builder.get(field, "")
        if not text:
            continue
        # Simple regex to find http(s) URLs
        urls = re.findall(r'https?://[^\s\]"\'<>]+', text)
        links.update(urls)

    return sorted(list(links))


def save_pass2_result(session: Session, team: Team, result: dict) -> None:
    """Save Pass-2 result from AI service to a Team record."""
    judge = result["judge"]
    critiques = result["critiques"]

    team.p2_score = judge["score"]
    team.p2_recommendation = judge["recommendation"]
    team.p2_verdict = judge["summary"]
    team.p2_critiques = json.dumps(critiques)

    # Extract evidence links from critiques
    evidence_links = extract_evidence_links(critiques)
    team.evidence_links = json.dumps(evidence_links)

    # Extract integrity flags
    integrity_flags = critiques.get("integrity", {}).get("flags", [])
    team.integrity_flags = json.dumps(integrity_flags)

    team.status = TeamStatus.P2_DONE
    team.error = None
    team.updated_at = utcnow()
    session.add(team)


def run_pass2(run_id: int) -> None:
    """Background task: evaluate all promoted teams for a run using Pass-2.

    Process:
    1. Load all teams with p1_band in (FAST_TRACK, BORDERLINE), status not P2_DONE
    2. Reset .error on unscored teams for resume support
    3. Loop in chunks: build inputs, call pass2_batch, validate, save, commit
    4. On timeout/http_error: retry individual teams via pass2_one
    5. On unreachable/no_llm_key: stop entirely
    6. Compute final_rank by p2_score (desc), tie-break by p1_composite (desc)
    7. Set run status to PASS2_DONE or PASS2_INCOMPLETE
    """
    logger.info("Pass 2 runner started for run_id=%d", run_id)

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error("Run %d not found", run_id)
            return

        try:
            # Find all promoted teams (FAST_TRACK or BORDERLINE)
            promoted_teams = session.exec(
                select(Team).where(
                    Team.run_id == run_id,
                    Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
                    Team.status != TeamStatus.P2_DONE,
                )
            ).all()

            if not promoted_teams:
                logger.info("No promoted teams to score for Pass-2")
                run.status = RunStatus.PASS2_DONE
                run.error = None
                session.add(run)
                session.commit()
                return

            logger.info("Pass 2: found %d promoted teams", len(promoted_teams))

            # Reset errors on unscored teams for resume support
            for team in promoted_teams:
                if team.p2_score is None:
                    team.error = None
                    session.add(team)
            session.commit()

            # Process teams in chunks
            total_chunks = (len(promoted_teams) + PASS2_BATCH_SIZE - 1) // PASS2_BATCH_SIZE

            for chunk_idx in range(total_chunks):
                start_idx = chunk_idx * PASS2_BATCH_SIZE
                end_idx = start_idx + PASS2_BATCH_SIZE
                chunk = promoted_teams[start_idx:end_idx]

                # Skip teams that are already scored (for resume)
                unscored = [t for t in chunk if t.p2_score is None]
                if not unscored:
                    logger.info("Chunk %d: all teams already scored, skipping", chunk_idx + 1)
                    continue

                logger.info(
                    "Processing Pass-2 chunk %d/%d (%d teams)",
                    chunk_idx + 1,
                    total_chunks,
                    len(unscored),
                )

                # Build inputs with peer summaries from FULL promoted set
                peer_summaries_full = build_peer_summaries(promoted_teams, exclude_team_id="")
                inputs = [
                    build_pass2_input(t, build_peer_summaries(promoted_teams, t.team_id))
                    for t in unscored
                ]

                start = time.time()
                try:
                    results = ai_client.pass2_batch(inputs)
                    duration = time.time() - start
                    logger.info(
                        "pass2_batch(%d teams) completed in %.2fs",
                        len(unscored),
                        duration,
                    )
                except AIServiceError as exc:
                    duration = time.time() - start
                    logger.error(
                        "pass2_batch(%d teams) failed after %.2fs: %s",
                        len(unscored),
                        duration,
                        exc,
                    )

                    # Retry individually on timeout/http_error
                    if exc.kind in ("timeout", "http_error"):
                        logger.info("Batch failed with %s, retrying teams individually", exc.kind)
                        for team in unscored:
                            try:
                                input_dict = build_pass2_input(
                                    team,
                                    build_peer_summaries(promoted_teams, team.team_id),
                                )
                                result = ai_client.pass2_one(input_dict)
                                save_pass2_result(session, team, result)
                                logger.info("Team %s: scored individually", team.team_id)
                            except AIServiceError as e:
                                logger.error(
                                    "pass2_one(%s) failed: %s",
                                    team.team_id,
                                    e,
                                )
                                team.error = f"AI service error: {str(e)}"
                                session.add(team)
                        session.commit()
                        logger.info("Chunk %d: completed individual retry", chunk_idx + 1)
                        continue
                    else:
                        # unreachable or no_llm_key: abort entirely
                        logger.error("Fatal AI error (%s), stopping run", exc.kind)
                        run.status = RunStatus.INTERRUPTED
                        run.error = f"AI service error: {str(exc)}"
                        session.add(run)
                        session.commit()
                        return

                # Validate response
                is_valid, error_msg = validate_pass2_response(inputs, results)
                if not is_valid:
                    logger.error("Invalid Pass-2 response: %s", error_msg)
                    # Mark all teams in chunk as failed
                    for team in unscored:
                        team.error = f"AI response validation failed: {error_msg}"
                        session.add(team)
                    session.commit()
                    continue

                # Save results
                results_by_id = {r["team"]["team_id"]: r for r in results}
                for team in unscored:
                    result = results_by_id[team.team_id]
                    save_pass2_result(session, team, result)

                session.commit()
                logger.info("Chunk %d: saved %d results", chunk_idx + 1, len(unscored))

            # Check if any promoted team still lacks p2_score
            failed_teams = session.exec(
                select(Team).where(
                    Team.run_id == run_id,
                    Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
                    Team.p2_score == None,
                )
            ).all()

            if failed_teams:
                logger.warning("Pass 2 incomplete: %d promoted teams failed", len(failed_teams))
                run.status = RunStatus.PASS2_INCOMPLETE
                run.error = f"{len(failed_teams)} promoted team(s) failed Pass-2 scoring"
                session.add(run)
                session.commit()
                return

            # All promoted teams scored: compute final_rank
            logger.info("All promoted teams scored, computing final_rank")
            scored_promoted = session.exec(
                select(Team).where(
                    Team.run_id == run_id,
                    Team.p1_band.in_(["FAST_TRACK", "BORDERLINE"]),
                    Team.p2_score != None,
                )
            ).all()

            # Sort by p2_score desc, then p1_composite desc
            ranked = sorted(
                scored_promoted,
                key=lambda t: (-t.p2_score, -(t.p1_composite or 0)),
            )

            for rank, team in enumerate(ranked, start=1):
                team.final_rank = rank
                team.status = TeamStatus.P2_DONE
                session.add(team)

            run.status = RunStatus.PASS2_DONE
            run.error = None
            session.add(run)
            session.commit()

            logger.info("Pass 2 completed for run_id=%d (%d teams ranked)", run_id, len(ranked))

        except Exception as exc:
            logger.exception("Pass 2 runner crashed for run_id=%d", run_id)
            run.status = RunStatus.INTERRUPTED
            run.error = f"Internal error: {type(exc).__name__}"
            session.add(run)
            session.commit()
