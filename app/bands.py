"""Band computation logic for Pass 1 scoring.

Band assignment rules:
- Bottom 40% = REJECT
- Next 35% = BORDERLINE
- Top 25% = FAST_TRACK

TIE RULE: Teams with identical composite scores must get the same band.
If a tie straddles a boundary, ALL tied teams get the better band.
"""


def compute_bands(teams: list[dict]) -> dict[str, str]:
    """Compute p1_band for scored teams based on their composite scores.

    Args:
        teams: List of dicts with keys {team_id: str, p1_composite: float}.
               Only complete teams with valid p1_composite scores.

    Returns:
        Dict mapping team_id -> band ("REJECT" | "BORDERLINE" | "FAST_TRACK")

    Rules:
        - Sort by p1_composite descending, tie-break by team_id ascending
        - Bottom 40% = REJECT, next 35% = BORDERLINE, top 25% = FAST_TRACK
        - Teams with identical composites get the same band
        - If a tie straddles a boundary, all tied teams get the better band
    """
    if not teams:
        return {}

    # Sort by composite descending, then team_id ascending for deterministic ordering
    sorted_teams = sorted(
        teams,
        key=lambda t: (-t["p1_composite"], t["team_id"])
    )

    n = len(sorted_teams)

    # Compute boundary indices (round as specified)
    fast_track_count = round(0.25 * n)
    reject_count = round(0.40 * n)

    # Boundary scores: team at index gets that band or better
    # fast_track_cutoff_idx: last team in FAST_TRACK zone (0-indexed)
    # reject_cutoff_idx: first team in REJECT zone (0-indexed)
    fast_track_cutoff_idx = fast_track_count - 1 if fast_track_count > 0 else -1
    reject_cutoff_idx = n - reject_count if reject_count > 0 else n

    result = {}

    for idx, team in enumerate(sorted_teams):
        team_id = team["team_id"]
        score = team["p1_composite"]

        # Determine initial band based on position
        if fast_track_cutoff_idx >= 0 and idx <= fast_track_cutoff_idx:
            band = "FAST_TRACK"
        elif idx >= reject_cutoff_idx:
            band = "REJECT"
        else:
            band = "BORDERLINE"

        # Apply tie rule: if this score equals a score in a better band, promote
        # Check if tied with anyone in FAST_TRACK
        if band != "FAST_TRACK" and fast_track_cutoff_idx >= 0:
            if score == sorted_teams[fast_track_cutoff_idx]["p1_composite"]:
                band = "FAST_TRACK"

        # Check if tied with anyone in BORDERLINE (when we're in REJECT)
        if band == "REJECT" and reject_cutoff_idx > 0:
            # First BORDERLINE team is at reject_cutoff_idx - 1
            borderline_idx = reject_cutoff_idx - 1
            if borderline_idx >= 0 and score == sorted_teams[borderline_idx]["p1_composite"]:
                band = "BORDERLINE"

        result[team_id] = band

    return result
