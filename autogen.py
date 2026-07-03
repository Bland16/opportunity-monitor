"""Natural-language config generation via the Claude API.

Turns a free-text description ("CS junior looking for backend fintech
internships at startups") into a structured ``{companies, keywords}`` draft
that the UI fills into a category's config for review. The user still confirms
before anything is saved -- this only drafts.

Implementation note: the project constraint is "no third-party dependencies
beyond ``requests``", so this calls the Claude Messages API over raw HTTP with
``requests`` rather than adding the official ``anthropic`` SDK. If that
constraint is relaxed, the SDK (``pip install anthropic`` →
``anthropic.Anthropic().messages.create(...)``) is the recommended path.

Auth: reads the API key from the ``ANTHROPIC_API_KEY`` environment variable.
Model defaults to ``claude-opus-4-8`` (override via ``OPPORTUNITY_LLM_MODEL``).
"""

from __future__ import annotations

import json
import os
from typing import Final

import requests

# --- Configuration ---------------------------------------------------------

#: Claude Messages API endpoint.
ANTHROPIC_URL: Final[str] = "https://api.anthropic.com/v1/messages"

#: Required API version header value.
ANTHROPIC_VERSION: Final[str] = "2023-06-01"

#: Default model (structured outputs supported). Override via env.
DEFAULT_MODEL: Final[str] = "claude-opus-4-8"

#: Environment variables.
ENV_API_KEY: Final[str] = "ANTHROPIC_API_KEY"
ENV_MODEL: Final[str] = "OPPORTUNITY_LLM_MODEL"

#: Request timeout (seconds) and generation cap.
_TIMEOUT: Final[float] = 30.0
_MAX_TOKENS: Final[int] = 1024

#: Maximum accepted description length.
_MAX_DESCRIPTION: Final[int] = 2000

#: Output caps so a generated draft stays manageable.
_MAX_COMPANIES: Final[int] = 40
_MAX_KEYWORDS: Final[int] = 12

#: JSON schema the model must fill (structured outputs → validated JSON back).
_OUTPUT_SCHEMA: Final[dict] = {
    "type": "object",
    "properties": {
        "companies": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["companies", "keywords"],
    "additionalProperties": False,
}

#: Per-category system prompt guiding what to produce.
_CATEGORY_SYSTEM: Final[dict[str, str]] = {
    "jobs": (
        "You configure a read-only job-discovery tool that queries company "
        "applicant-tracking boards (Greenhouse, Lever, Ashby) by company slug. "
        "From the user's description produce:\n"
        "- companies: 20-40 lowercase ATS board slugs for real companies that fit "
        "(the slug is the identifier in the board URL, e.g. stripe, figma, ramp, "
        "notion, databricks). Prefer companies likely to use Greenhouse/Lever/Ashby.\n"
        "- keywords: 2-6 short role keywords to filter postings (e.g. backend, "
        "data, intern, new grad)."
    ),
    "programs": (
        "You configure a tool that scans curated GitHub lists of student programs, "
        "fellowships, and scholarships. From the user's description produce 4-10 "
        "keywords that would match relevant entries (e.g. fellowship, research, "
        "scholars, diversity, mentorship). Return companies as an empty list."
    ),
    "leadership": (
        "You configure a tool that scans curated lists for leadership, rotational, "
        "and early-talent development programs. From the description produce 4-10 "
        "keywords that would match relevant entries (e.g. leadership, rotational, "
        "LDP, bold, explore, insight). Return companies as an empty list."
    ),
    "research": (
        "You configure a tool that scans an undergraduate research (REU) database. "
        "From the description produce 4-10 keywords that would match relevant "
        "programs (fields of study or program types, e.g. biology, physics, data, "
        "summer, computational). Return companies as an empty list."
    ),
}


class AutogenError(Exception):
    """Raised when config generation cannot complete (surfaced to the user)."""


# --- Cleaning helpers ------------------------------------------------------


def _clean(values: object, *, lower: bool, cap: int) -> list[str]:
    """Strip, optionally lowercase, dedupe, and cap a list of strings.

    Args:
        values: Candidate list from the model (validated by the schema).
        lower: Whether to lowercase entries (used for company slugs).
        cap: Maximum number of entries to keep.

    Returns:
        A cleaned list of unique non-empty strings.
    """
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return out
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip().lower() if lower else value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= cap:
            break
    return out


# --- Public API ------------------------------------------------------------


def generate(description: str, category: str, timeout: float = _TIMEOUT) -> dict[str, list[str]]:
    """Generate a ``{companies, keywords}`` draft from a description.

    Args:
        description: Free-text description of what the user is looking for.
        category: One of ``"jobs"``, ``"programs"``, ``"leadership"``,
            ``"research"`` (selects the guidance prompt).
        timeout: Network timeout in seconds.

    Returns:
        A dict with ``"companies"`` and ``"keywords"`` string lists.

    Raises:
        AutogenError: On invalid input, a missing API key, network/API failure,
            a model refusal, or an unparseable response.
    """
    text = description.strip()
    if not text:
        raise AutogenError("Description is empty; describe what you're looking for.")
    if len(text) > _MAX_DESCRIPTION:
        raise AutogenError(
            f"Description is {len(text)} characters; the maximum is {_MAX_DESCRIPTION}."
        )

    system = _CATEGORY_SYSTEM.get(category)
    if system is None:
        raise AutogenError(f"Unknown category: {category!r}.")

    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise AutogenError(
            f"Auto-generate needs a Claude API key. Set the {ENV_API_KEY} "
            "environment variable and restart."
        )

    model = os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL
    payload = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": f"Description: {text}"}],
        # Structured outputs: the first text block is guaranteed valid JSON
        # matching the schema, so no brittle parsing of prose is needed.
        "output_config": {"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        response = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise AutogenError(f"Claude API request timed out after {timeout}s.") from exc
    except requests.exceptions.RequestException as exc:
        raise AutogenError(f"Could not reach the Claude API: {exc}.") from exc

    if not response.ok:
        raise AutogenError(_api_error_message(response))

    try:
        data = response.json()
    except ValueError as exc:
        raise AutogenError(f"Claude API returned invalid JSON: {exc}.") from exc

    if data.get("stop_reason") == "refusal":
        raise AutogenError("The model declined this request; try rephrasing the description.")

    raw_text = _first_text_block(data)
    if raw_text is None:
        raise AutogenError("Claude API response contained no text to parse.")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AutogenError(f"Could not parse the generated config: {exc}.") from exc

    return {
        "companies": _clean(parsed.get("companies"), lower=True, cap=_MAX_COMPANIES),
        "keywords": _clean(parsed.get("keywords"), lower=False, cap=_MAX_KEYWORDS),
    }


# --- Response helpers ------------------------------------------------------


def _first_text_block(data: dict) -> str | None:
    """Return the text of the first ``text`` content block, if any.

    Args:
        data: Decoded Messages API response.

    Returns:
        The block text, or ``None`` if no text block is present.
    """
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text")
    return None


def _api_error_message(response: requests.Response) -> str:
    """Build a clear message from a non-2xx Claude API response.

    Args:
        response: The failed response.

    Returns:
        A human-readable error string, mapping common statuses to hints.
    """
    detail = ""
    try:
        body = response.json()
        detail = body.get("error", {}).get("message", "")
    except ValueError:
        detail = response.text[:200]

    match response.status_code:
        case 401:
            return f"Claude API rejected the key (401). Check {ENV_API_KEY}. {detail}".strip()
        case 429:
            return f"Claude API rate limit hit (429). Wait and retry. {detail}".strip()
        case status if status >= 500:
            return f"Claude API server error ({status}). Retry shortly. {detail}".strip()
        case status:
            return f"Claude API error ({status}). {detail}".strip()
