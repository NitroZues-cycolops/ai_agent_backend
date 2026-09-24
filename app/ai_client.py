import logging

import httpx

from app.config import AI_SERVICE_URL, AI_SERVICE_TIMEOUT

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """The ai/ service is down, slow, or returned an error.
    The message is short and safe to send to the frontend; the raw
    details go to the log only.

    Attributes:
        kind: "unreachable" | "timeout" | "no_llm_key" | "http_error"
        status_code: HTTP status code if applicable, else None
    """
    def __init__(self, message: str, kind: str, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


def _request(method: str, path: str, json: dict | None = None) -> dict:
    url = f"{AI_SERVICE_URL}{path}"
    try:
        with httpx.Client(timeout=AI_SERVICE_TIMEOUT) as client:
            response = client.request(method, url, json=json)
    except httpx.TimeoutException as exc:
        logger.error("ai/ timeout on %s %s", method, path)
        raise AIServiceError("AI service timed out", kind="timeout") from exc
    except httpx.RequestError as exc:
        logger.error("ai/ unreachable on %s %s: %s", method, path, exc)
        raise AIServiceError("AI service is unreachable", kind="unreachable") from exc

    if response.status_code >= 400:
        logger.error(
            "ai/ error on %s %s -> %s: %s",
            method, path, response.status_code, response.text[:500],
        )
        if response.status_code == 503:
            raise AIServiceError(
                "AI service has no LLM key configured",
                kind="no_llm_key",
                status_code=503
            )
        raise AIServiceError(
            f"AI service returned an error ({response.status_code})",
            kind="http_error",
            status_code=response.status_code
        )

    return response.json()


def health() -> dict:
    """GET /health -> {status, service, version, llm_configured}"""
    return _request("GET", "/health")


def check_ready() -> None:
    """Call before starting a scoring run. Fails early with a clear message
    instead of failing on the first team."""
    info = health()
    if not info.get("llm_configured"):
        raise AIServiceError(
            "AI service is running but has no LLM key configured",
            kind="no_llm_key"
        )


def score_batch(teams: list[dict]) -> list[dict]:
    """POST /v1/score/batch. Takes 1 to 50 TeamInput dicts, returns P1Score dicts."""
    if not 1 <= len(teams) <= 50:
        raise ValueError("score_batch takes between 1 and 50 teams")
    data = _request("POST", "/v1/score/batch", {"teams": teams})
    return data["scores"]


def score_one(team: dict) -> dict:
    """POST /v1/score. Takes one TeamInput dict, returns a P1Score dict."""
    data = _request("POST", "/v1/score", {"team": team})
    return data


def pass2_batch(teams: list[dict]) -> list[dict]:
    """POST /v1/pass2. Takes Pass2TeamInput dicts, returns Pass2RankedTeam dicts
    (each has team, critiques, judge, rank)."""
    data = _request("POST", "/v1/pass2", {"teams": teams})
    return data["teams"]


def pass2_one(team: dict) -> dict:
    """POST /v1/pass2 with a single team. Takes one Pass2TeamInput dict,
    returns a Pass2RankedTeam dict (has team, critiques, judge, rank)."""
    data = _request("POST", "/v1/pass2", {"teams": [team]})
    return data["teams"][0]