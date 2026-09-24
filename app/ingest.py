"""Ingest teams from Google Sheet rows into the database."""
from sqlmodel import Session, select
from app.models import Team, TeamStatus, utcnow


# Map our database field names to the Google Sheet column headers.
# Header matching is case-insensitive with surrounding spaces stripped.
COLUMN_MAP = {
    "team_id": "team_id",
    "team_name": "team_name",
    "track": "track",
    "theme": "theme",
    "idea": "idea",
    "resume": "resume_urls",
    "project_links": "project_links",
}

# Fields that MUST be present (beyond team_id, track, idea which are always required)
REQUIRED_FIELDS = ["theme", "resume", "project_links"]


def clean(value) -> str | None:
    """Clean a cell value: strip whitespace and return None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def normalize_track(value: str | None) -> str | None:
    """Normalize track values to 'new_idea' or 'existing_project'.

    Returns None if the value is missing or unrecognized.
    """
    if not value:
        return None
    val = value.strip().lower().replace(" ", "_").replace("-", "_")
    if val in ("new_idea", "newidea"):
        return "new_idea"
    if val in ("existing_project", "existingproject"):
        return "existing_project"
    return None  # unrecognized


def missing_columns(rows: list[dict]) -> list[str]:
    """Return list of required column headers that are missing from the sheet.

    Only requires team_id, track, idea, and the columns for REQUIRED_FIELDS.
    team_name is OPTIONAL.
    Case-insensitive, ignoring surrounding whitespace.
    """
    if not rows:
        return []

    # Normalize the sheet headers (lowercase, stripped)
    sheet_headers = {str(h).strip().lower() for h in rows[0].keys()}

    # Required field keys: team_id, track, idea, plus REQUIRED_FIELDS
    required_field_keys = ["team_id", "track", "idea"] + [
        f for f in REQUIRED_FIELDS if f not in ("team_id", "track", "idea")
    ]

    missing = []
    for field_key in required_field_keys:
        expected_header = COLUMN_MAP[field_key]
        if expected_header.strip().lower() not in sheet_headers:
            missing.append(expected_header)

    return missing


def parse_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    """Parse sheet rows into team data dictionaries.

    Returns:
        (teams, metadata) where:
        - teams: list of dicts with keys: team_id, team_name, track, theme, idea,
                 resume, project_links, status, error, sheet_row
        - metadata: dict with diagnostic info (empty_rows, rows_without_team_id, etc.)

    Rules:
    - Fully blank rows are counted (empty_rows).
    - Rows with data but no team_id are reported by row number (rows_without_team_id).
    - Duplicate team_id in the sheet: keep first row's data, mark as INCOMPLETE with
      a reason listing all duplicate row numbers.
    - A team is INCOMPLETE if any required field is missing or track is missing/unrecognized.
    - Otherwise P1_QUEUED.
    """
    teams_by_id = {}  # team_id -> team dict
    duplicates = {}   # team_id -> list of sheet row numbers

    empty_rows = []
    rows_without_team_id = []

    for idx, row in enumerate(rows):
        sheet_row = idx + 2  # Sheet row number (row 1 = headers, data starts at row 2)

        # Normalize row keys (strip, lowercase) for case-insensitive lookup
        normalized_row = {str(k).strip().lower(): v for k, v in row.items()}

        # Check if row is fully blank
        if all(not clean(v) for v in normalized_row.values()):
            empty_rows.append(sheet_row)
            continue

        # Extract and clean team_id
        team_id = clean(normalized_row.get(COLUMN_MAP["team_id"]))

        if not team_id:
            rows_without_team_id.append(sheet_row)
            continue

        # Track duplicates
        if team_id in teams_by_id:
            if team_id not in duplicates:
                duplicates[team_id] = [teams_by_id[team_id]["sheet_row"]]
            duplicates[team_id].append(sheet_row)
            continue  # skip; we keep the first occurrence

        # Extract and normalize fields
        team_name = clean(normalized_row.get(COLUMN_MAP["team_name"]))
        raw_track = clean(normalized_row.get(COLUMN_MAP["track"]))
        track = normalize_track(raw_track)
        theme = clean(normalized_row.get(COLUMN_MAP["theme"]))
        idea = clean(normalized_row.get(COLUMN_MAP["idea"]))
        resume = clean(normalized_row.get(COLUMN_MAP["resume"]))
        project_links = clean(normalized_row.get(COLUMN_MAP["project_links"]))

        # Determine status and error
        problems = []
        if not track:
            if raw_track:
                problems.append(f"unrecognized track '{raw_track}'")
            else:
                problems.append("missing track")
        if not theme:
            problems.append("missing theme")
        if not idea:
            problems.append("missing idea")
        if not resume:
            problems.append("missing resume")
        if not project_links:
            problems.append("missing project_links")

        status = TeamStatus.INCOMPLETE if problems else TeamStatus.P1_QUEUED
        error = "; ".join(problems) if problems else None

        teams_by_id[team_id] = {
            "team_id": team_id,
            "team_name": team_name,
            "track": track,
            "theme": theme,
            "idea": idea,
            "resume": resume,
            "project_links": project_links,
            "status": status,
            "error": error,
            "problems": problems,
            "sheet_row": sheet_row,
        }

    # Mark duplicates as INCOMPLETE
    for team_id, row_numbers in duplicates.items():
        team = teams_by_id[team_id]
        dup_msg = f"duplicate team_id in sheet rows: {', '.join(map(str, row_numbers))}"
        team["problems"].append(dup_msg)
        if team["error"]:
            team["error"] += f"; {dup_msg}"
        else:
            team["error"] = dup_msg
        team["status"] = TeamStatus.INCOMPLETE

    metadata = {
        "empty_rows": empty_rows,
        "rows_without_team_id": rows_without_team_id,
        "duplicate_ids": duplicates,
    }

    return list(teams_by_id.values()), metadata


def ingest_rows(session: Session, run_id: int, rows: list[dict]) -> dict:
    """Ingest parsed team data into the database.

    Does NOT commit the session. The caller is responsible for committing.

    Upsert by (run_id, team_id):
    - Only update teams whose status is INCOMPLETE or P1_QUEUED.
    - Other teams are reported as locked_not_updated.

    Returns a summary dict with:
    - rows_in_sheet: number of data rows (header row not counted)
    - teams_in_sheet: number of teams parsed from the sheet
    - created: number of new teams added
    - updated: number of existing teams updated
    - complete: number of teams with status P1_QUEUED
    - incomplete: number of teams with status INCOMPLETE
    - incomplete_teams: list of {team_id, sheet_row, problems}
    - empty_rows: list of sheet row numbers that were fully blank
    - rows_without_team_id: list of sheet row numbers with data but no team_id
    - in_database_not_in_sheet: list of team_ids in the DB but not in the sheet
    - locked_not_updated: list of team_ids that exist but are in a non-updatable status
    """
    teams, metadata = parse_rows(rows)

    # Preload existing teams for this run (one query)
    existing_teams = session.exec(
        select(Team).where(Team.run_id == run_id)
    ).all()
    existing_by_id = {t.team_id: t for t in existing_teams}

    sheet_team_ids = {t["team_id"] for t in teams}
    in_db_not_in_sheet = [
        tid for tid in existing_by_id.keys() if tid not in sheet_team_ids
    ]

    created = 0
    updated = 0
    complete = 0
    incomplete = 0
    incomplete_teams = []
    locked_not_updated = []

    for team_data in teams:
        team_id = team_data["team_id"]
        existing = existing_by_id.get(team_id)

        if existing:
            # Only update if status is INCOMPLETE or P1_QUEUED
            if existing.status not in (TeamStatus.INCOMPLETE, TeamStatus.P1_QUEUED):
                locked_not_updated.append(team_id)
                continue

            # Update existing team
            existing.team_name = team_data["team_name"]
            existing.track = team_data["track"]
            existing.theme = team_data["theme"]
            existing.idea = team_data["idea"]
            existing.resume_path = team_data["resume"]
            existing.project_links = team_data["project_links"]
            existing.status = team_data["status"]
            existing.error = team_data["error"]
            existing.updated_at = utcnow()
            updated += 1
        else:
            # Create new team
            new_team = Team(
                run_id=run_id,
                team_id=team_id,
                team_name=team_data["team_name"],
                track=team_data["track"],
                theme=team_data["theme"],
                idea=team_data["idea"],
                resume_path=team_data["resume"],
                project_links=team_data["project_links"],
                status=team_data["status"],
                error=team_data["error"],
            )
            session.add(new_team)
            created += 1

        if team_data["status"] == TeamStatus.P1_QUEUED:
            complete += 1
        else:
            incomplete += 1
            incomplete_teams.append({
                "team_id": team_id,
                "sheet_row": team_data["sheet_row"],
                "problems": team_data["problems"],
            })

    return {
        "rows_in_sheet": len(rows),  # data rows only (no header)
        "teams_in_sheet": len(teams),
        "created": created,
        "updated": updated,
        "complete": complete,
        "incomplete": incomplete,
        "incomplete_teams": incomplete_teams,
        "empty_rows": metadata["empty_rows"],
        "rows_without_team_id": metadata["rows_without_team_id"],
        "in_database_not_in_sheet": in_db_not_in_sheet,
        "locked_not_updated": locked_not_updated,
    }
