"""Scraping logic for the job/opportunity monitor.

Defines the source-agnostic contract (:class:`Opportunity`,
:class:`ScraperBase`) plus concrete, read-only scrapers:

* :class:`GreenhouseScraper`, :class:`LeverScraper`, :class:`AshbyScraper` --
  query a company's public applicant-tracking (ATS) JSON board.
* :class:`CompanyJobsScraper` -- tries all three ATS boards per company and
  merges/deduplicates the results (used by the ``jobs`` category).
* :class:`GitHubListScraper` -- parses curated GitHub list READMEs (raw
  markdown) for programs/fellowships/leadership listings (used by the
  ``programs`` and ``leadership`` categories).

Everything issues HTTP GET only -- never submits or interacts with forms.
This module has no dependency on ``config`` or ``ui``.
"""

from __future__ import annotations

import abc
import html
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Final

import requests

# --- Module-level constants ------------------------------------------------

#: Maximum allowed length of a single search term, in characters.
MAX_TERM_LENGTH: Final[int] = 200

#: Default network timeout (seconds) for all outbound HTTP requests.
DEFAULT_TIMEOUT: Final[float] = 15.0

#: A polite, identifying User-Agent so target sites can attribute traffic.
DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": "job-monitor/1.0 (read-only discovery bot)",
}

#: Curated GitHub list READMEs (raw markdown) used for programs/leadership.
#: Full raw URLs so we don't have to guess each repo's default branch.
DEFAULT_PROGRAM_REPOS: Final[tuple[str, ...]] = (
    "https://raw.githubusercontent.com/Julian048/CS-Everything-but-Internships/main/README.md",
    "https://raw.githubusercontent.com/LuisaE/opportunities/master/README.md",
    "https://raw.githubusercontent.com/zapplyjobs/underclassmen-internships/main/README.md",
)

#: Safety cap on rows returned from a GitHub list when no keywords are set,
#: so an unfiltered run cannot return thousands of rows.
_UNFILTERED_ROW_CAP: Final[int] = 200

#: PathwaysToScience listing endpoint (keyless) for undergraduate REU/research.
PATHWAYS_URL: Final[str] = "https://www.pathwaystoscience.org/programs.aspx"

#: Query parameters selecting the undergraduate "Summer Research (REU)" listing.
PATHWAYS_PARAMS: Final[dict[str, str]] = {
    "sort": "undergrad",
    "descr": "Summer Research Opportunities (REU)",
}

#: Environment variables that enable the optional web-search fallback.
#: OPPORTUNITY_SEARCH_KEY is the API key; OPPORTUNITY_SEARCH_PROVIDER is
#: "brave" (default) or "serpapi".
ENV_SEARCH_KEY: Final[str] = "OPPORTUNITY_SEARCH_KEY"
ENV_SEARCH_PROVIDER: Final[str] = "OPPORTUNITY_SEARCH_PROVIDER"

#: Max web-search results requested per keyword.
_SEARCH_RESULTS_PER_TERM: Final[int] = 8


# --- Error types -----------------------------------------------------------


class ScraperError(Exception):
    """Base class for all recoverable scraping errors."""


class NetworkError(ScraperError):
    """Raised when a request fails to reach the server or times out."""


class ResponseError(ScraperError):
    """Raised when a response is received but is malformed or unexpected."""


# --- Data model ------------------------------------------------------------


@dataclass(slots=True)
class Opportunity:
    """A single job or opportunity listing discovered by a scraper.

    Attributes:
        title: Human-readable title of the listing.
        url: Canonical URL where the listing can be viewed.
        source: Identifier of the scraper/site that produced the record
            (e.g. ``"greenhouse:stripe"`` or ``"github:LuisaE/opportunities"``).
        date_posted: Posting date as a string, or ``None`` if unknown.
        description: Free-text summary (e.g. company, location).
    """

    title: str
    url: str
    source: str
    date_posted: str | None
    description: str = ""

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable dict of this opportunity.

        Returns:
            A plain dict with the five dataclass fields.
        """
        return asdict(self)


# --- Matching helper -------------------------------------------------------


def _matches(text: str, keywords: list[str]) -> bool:
    """Return whether ``text`` contains any keyword (case-insensitive).

    Args:
        text: Text to test (e.g. a job title plus location).
        keywords: Keywords to look for. An empty list matches everything.

    Returns:
        ``True`` if no keywords were given, or any keyword is a substring.
    """
    if not keywords:
        return True
    low = text.lower()
    return any(keyword.lower() in low for keyword in keywords)


# --- Scraper contract ------------------------------------------------------


class ScraperBase(abc.ABC):
    """Abstract base class defining the read-only scraper contract.

    Subclasses implement :meth:`fetch`. Shared concerns -- input validation,
    error-wrapped HTTP, and per-run error collection -- live here.

    Attributes:
        source_name: Short identifier for records produced by this scraper.
        timeout: Per-request timeout in seconds.
        errors: Human-readable messages for sources that failed during the
            last :meth:`fetch` (soft failures the UI can surface).
    """

    source_name: str = "base"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize the scraper.

        Args:
            timeout: Network timeout in seconds applied to every request.
        """
        self.timeout: float = timeout
        self.errors: list[str] = []
        self._session: requests.Session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)

    # -- Validation ---------------------------------------------------------

    @staticmethod
    def validate_search_terms(search_terms: list[str]) -> list[str]:
        """Validate and normalize a list of search terms.

        A term is valid when, after stripping surrounding whitespace, it is
        non-empty and no longer than :data:`MAX_TERM_LENGTH` characters.

        Args:
            search_terms: Raw search terms.

        Returns:
            The list of stripped, validated terms.

        Raises:
            ValueError: If ``search_terms`` is not a non-empty list, or any
                term is not a string, is empty/whitespace-only, or exceeds
                :data:`MAX_TERM_LENGTH` characters.
        """
        if not isinstance(search_terms, list) or not search_terms:
            raise ValueError("search_terms must be a non-empty list of strings.")

        cleaned: list[str] = []
        for index, term in enumerate(search_terms):
            if not isinstance(term, str):
                raise ValueError(
                    f"Search term at position {index} is not a string: {term!r}."
                )
            stripped = term.strip()
            if not stripped:
                raise ValueError(
                    f"Search term at position {index} is empty or "
                    "whitespace-only; every term must contain visible text."
                )
            if len(stripped) > MAX_TERM_LENGTH:
                raise ValueError(
                    f"Search term at position {index} is {len(stripped)} "
                    f"characters long; the maximum is {MAX_TERM_LENGTH}."
                )
            cleaned.append(stripped)
        return cleaned

    # -- Networking ---------------------------------------------------------

    def http_get(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        """Perform a read-only HTTP GET with uniform error handling.

        Args:
            url: Absolute URL to request.
            params: Optional query-string parameters.

        Returns:
            The successful :class:`requests.Response` (status code 2xx).

        Raises:
            NetworkError: On timeouts or connection-level failures.
            ResponseError: On a non-2xx status code.
        """
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise NetworkError(f"Request to {url} timed out after {self.timeout}s.") from exc
        except requests.exceptions.ConnectionError as exc:
            raise NetworkError(f"Could not connect to {url}: {exc}.") from exc
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Request to {url} failed: {exc}.") from exc

        if not response.ok:
            raise ResponseError(
                f"Unexpected HTTP status {response.status_code} "
                f"({response.reason}) from {url}."
            )
        return response

    # -- Contract -----------------------------------------------------------

    @abc.abstractmethod
    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Query the target source and return matching opportunities.

        Args:
            search_terms: Keywords to filter by (may be empty to match all,
                depending on the concrete scraper).

        Returns:
            A list of :class:`Opportunity` records (possibly empty).

        Raises:
            NetworkError: On network-level failures.
            ResponseError: On malformed or unexpected responses.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release the underlying HTTP session (safe to call repeatedly)."""
        self._session.close()

    def __enter__(self) -> "ScraperBase":
        """Enter a context manager, returning ``self``."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Exit the context manager, closing the session."""
        self.close()


# --- ATS company-board scrapers -------------------------------------------


class _CompanyBoardScraper(ScraperBase):
    """Shared logic for scrapers that query one ATS board per company.

    A 404 (company does not use this board) is an expected, silent skip. Only
    genuine network failures are recorded in :attr:`errors`.

    Attributes:
        companies: Company/board slugs to query.
    """

    def __init__(self, companies: list[str], timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize with the companies to query.

        Args:
            companies: Company/board slugs (e.g. ``["stripe", "figma"]``).
            timeout: Network timeout in seconds.
        """
        super().__init__(timeout)
        self.companies: list[str] = companies

    def _endpoint(self, company: str) -> str:
        """Return the board API URL for ``company`` (implemented by subclasses)."""
        raise NotImplementedError

    def _params(self) -> dict[str, str] | None:
        """Return optional query params for the board request."""
        return None

    def _parse(self, company: str, data: object) -> list[Opportunity]:
        """Map a decoded board response into opportunities (subclass-specific)."""
        raise NotImplementedError

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Query each company's board and return keyword-matching openings.

        Args:
            search_terms: Keywords to filter titles by (empty matches all).

        Returns:
            Matching :class:`Opportunity` records across all companies.
        """
        keywords = [t.strip() for t in search_terms if t.strip()]
        self.errors = []
        results: list[Opportunity] = []

        for company in self.companies:
            slug = company.strip().lower()
            if not slug:
                continue
            try:
                response = self.http_get(self._endpoint(slug), params=self._params())
                data = response.json()
            except ResponseError:
                # Almost always a 404 -> company doesn't use this ATS. Skip quietly.
                continue
            except NetworkError as exc:
                self.errors.append(f"{self.source_name}:{slug}: {exc}")
                continue
            except ValueError as exc:
                # Body was not valid JSON.
                self.errors.append(f"{self.source_name}:{slug}: invalid JSON ({exc}).")
                continue

            for opp in self._parse(slug, data):
                if _matches(f"{opp.title} {opp.description}", keywords):
                    results.append(opp)
        return results


class GreenhouseScraper(_CompanyBoardScraper):
    """Scraper for Greenhouse public job boards."""

    source_name = "greenhouse"

    def _endpoint(self, company: str) -> str:
        """Return the Greenhouse jobs endpoint for ``company``."""
        return f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"

    def _parse(self, company: str, data: object) -> list[Opportunity]:
        """Map a Greenhouse ``jobs`` payload into opportunities.

        Args:
            company: The board slug queried.
            data: Decoded JSON response.

        Returns:
            One :class:`Opportunity` per posting.
        """
        match data:
            case {"jobs": list() as jobs}:
                pass
            case _:
                return []

        out: list[Opportunity] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            location = ""
            match job.get("location"):
                case {"name": str() as name}:
                    location = name
            out.append(
                Opportunity(
                    title=str(job.get("title", "")).strip(),
                    url=str(job.get("absolute_url", "")),
                    source=f"{self.source_name}:{company}",
                    date_posted=job.get("updated_at"),
                    description=f"{company} — {location}".strip(" —"),
                )
            )
        return out


class LeverScraper(_CompanyBoardScraper):
    """Scraper for Lever (jobs.lever.co) public postings."""

    source_name = "lever"

    def _endpoint(self, company: str) -> str:
        """Return the Lever postings endpoint for ``company``."""
        return f"https://api.lever.co/v0/postings/{company}"

    def _params(self) -> dict[str, str] | None:
        """Request JSON mode from Lever."""
        return {"mode": "json"}

    def _parse(self, company: str, data: object) -> list[Opportunity]:
        """Map a Lever postings array into opportunities.

        Args:
            company: The board slug queried.
            data: Decoded JSON response (a list of postings).

        Returns:
            One :class:`Opportunity` per posting.
        """
        if not isinstance(data, list):
            return []

        out: list[Opportunity] = []
        for post in data:
            if not isinstance(post, dict):
                continue
            categories = post.get("categories") or {}
            location = categories.get("location", "") if isinstance(categories, dict) else ""
            out.append(
                Opportunity(
                    title=str(post.get("text", "")).strip(),
                    url=str(post.get("hostedUrl", "")),
                    source=f"{self.source_name}:{company}",
                    date_posted=_ms_to_date(post.get("createdAt")),
                    description=f"{company} — {location}".strip(" —"),
                )
            )
        return out


class AshbyScraper(_CompanyBoardScraper):
    """Scraper for Ashby (jobs.ashbyhq.com) public job boards."""

    source_name = "ashby"

    def _endpoint(self, company: str) -> str:
        """Return the Ashby job-board endpoint for ``company``."""
        return f"https://api.ashbyhq.com/posting-api/job-board/{company}"

    def _parse(self, company: str, data: object) -> list[Opportunity]:
        """Map an Ashby ``jobs`` payload into opportunities.

        Args:
            company: The board slug queried.
            data: Decoded JSON response.

        Returns:
            One :class:`Opportunity` per posting.
        """
        match data:
            case {"jobs": list() as jobs}:
                pass
            case _:
                return []

        out: list[Opportunity] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            out.append(
                Opportunity(
                    title=str(job.get("title", "")).strip(),
                    # jobUrl is the public listing; fall back to applyUrl.
                    url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                    source=f"{self.source_name}:{company}",
                    date_posted=job.get("publishedAt"),
                    description=f"{company} — {job.get('location', '')}".strip(" —"),
                )
            )
        return out


class CompanyJobsScraper(ScraperBase):
    """Aggregate scraper: tries Greenhouse, Lever, and Ashby per company.

    Results are merged and deduplicated by URL, so a company hosted on one
    ATS contributes once even though all three boards are attempted.

    Attributes:
        companies: Company slugs to query across all supported ATS boards.
    """

    source_name = "company-jobs"

    def __init__(self, companies: list[str], timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize with the companies to query.

        Args:
            companies: Company/board slugs.
            timeout: Network timeout in seconds.
        """
        super().__init__(timeout)
        self.companies: list[str] = companies
        self._scrapers: list[_CompanyBoardScraper] = [
            GreenhouseScraper(companies, timeout),
            LeverScraper(companies, timeout),
            AshbyScraper(companies, timeout),
        ]

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Run every ATS scraper and return deduplicated matches.

        Args:
            search_terms: Keywords to filter titles by (empty matches all).

        Returns:
            Deduplicated :class:`Opportunity` records.
        """
        self.errors = []
        seen: set[str] = set()
        results: list[Opportunity] = []

        for scraper in self._scrapers:
            for opp in scraper.fetch(search_terms):
                key = opp.url or f"{opp.source}|{opp.title}"
                if key in seen:
                    continue
                seen.add(key)
                results.append(opp)
            self.errors.extend(scraper.errors)
        return results

    def close(self) -> None:
        """Close this scraper and every underlying ATS scraper."""
        super().close()
        for scraper in self._scrapers:
            scraper.close()


# --- Curated GitHub list scraper ------------------------------------------

#: Matches an inline markdown link: [text](url)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

#: Strips residual markdown/HTML emphasis and tags from a cell's text.
_CLEAN = re.compile(r"</?[^>]+>|[*`]")


class GitHubListScraper(ScraperBase):
    """Scraper for curated GitHub list READMEs (raw markdown tables).

    Fetches each repo's raw README and scans its markdown table rows. A row is
    returned when any keyword appears anywhere in the row (case-insensitive);
    the first markdown link in the row provides the title and apply URL. This
    row-level matching is deliberately format-tolerant, since these lists vary
    in exact column layout, use nested sub-rows, and embed HTML.

    Attributes:
        repos: Raw README URLs (or ``owner/repo``) to scan.
    """

    source_name = "github"

    def __init__(
        self,
        repos: list[str] | tuple[str, ...] = DEFAULT_PROGRAM_REPOS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize with the curated lists to scan.

        Args:
            repos: Raw README URLs, or ``owner/repo`` shorthands (resolved
                against the ``main`` branch).
            timeout: Network timeout in seconds.
        """
        super().__init__(timeout)
        self.repos: list[str] = list(repos)

    @staticmethod
    def _raw_url(repo: str) -> str:
        """Resolve a repo reference to a raw README URL.

        Args:
            repo: Either a full ``https://`` raw URL or an ``owner/repo`` slug.

        Returns:
            A raw.githubusercontent.com URL.
        """
        if repo.startswith("http://") or repo.startswith("https://"):
            return repo
        return f"https://raw.githubusercontent.com/{repo}/main/README.md"

    @staticmethod
    def _label(repo: str) -> str:
        """Return a compact ``owner/repo`` label for the ``source`` field."""
        cleaned = repo.replace("https://raw.githubusercontent.com/", "")
        parts = cleaned.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else cleaned

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Scan each curated list and return keyword-matching rows.

        Args:
            search_terms: Keywords to filter rows by. If empty, rows are
                returned up to :data:`_UNFILTERED_ROW_CAP`.

        Returns:
            Matching :class:`Opportunity` records across all repos.
        """
        keywords = [t.strip() for t in search_terms if t.strip()]
        self.errors = []
        results: list[Opportunity] = []
        seen: set[str] = set()

        for repo in self.repos:
            label = self._label(repo)
            try:
                response = self.http_get(self._raw_url(repo))
            except ScraperError as exc:
                self.errors.append(f"github:{label}: {exc}")
                continue

            for opp in self._parse_markdown(response.text, label):
                if not _matches(f"{opp.title} {opp.description}", keywords):
                    continue
                key = opp.url or opp.title
                if key in seen:
                    continue
                seen.add(key)
                results.append(opp)
                if not keywords and len(results) >= _UNFILTERED_ROW_CAP:
                    return results
        return results

    def _parse_markdown(self, text: str, label: str) -> list[Opportunity]:
        """Extract opportunities from a README's markdown table rows.

        Args:
            text: Raw README markdown.
            label: ``owner/repo`` label for the ``source`` field.

        Returns:
            One :class:`Opportunity` per table row that contains a link.
        """
        out: list[Opportunity] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            # Only consider table rows: start with '|' and contain a link.
            if not line.startswith("|"):
                continue
            # Skip header separator rows like |---|:--:|
            if set(line) <= set("|-: "):
                continue

            link = _MD_LINK.search(line)
            if link is None:
                continue

            title = _CLEAN.sub("", link.group(1)).strip()
            url = link.group(2).strip()
            # Build a short description from the remaining cell text.
            cells = [c.strip() for c in line.strip("|").split("|")]
            description = _CLEAN.sub("", " · ".join(c for c in cells if c))
            description = _MD_LINK.sub(r"\1", description).strip()

            if not title:
                continue
            out.append(
                Opportunity(
                    title=title,
                    url=url,
                    source=f"github:{label}",
                    date_posted=None,
                    description=description[:300],
                )
            )
        return out


# --- Helpers ---------------------------------------------------------------


def _ms_to_date(value: object) -> str | None:
    """Convert a millisecond epoch timestamp to an ISO date string.

    Args:
        value: Milliseconds since the Unix epoch (int/float), or anything else.

    Returns:
        An ``YYYY-MM-DD`` string, or ``None`` if ``value`` is not numeric.
    """
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


# --- PathwaysToScience (REU / research) scraper ---------------------------

#: Matches a PathwaysToScience program anchor: <a href='programhub.aspx?...'>Title</a>
_PTS_ANCHOR = re.compile(r"<a href='(programhub\.aspx\?[^']+)'>(.*?)</a>", re.IGNORECASE | re.DOTALL)

#: Anchor texts that are navigation, not real program titles.
_PTS_SKIP = frozenset({"", "...read more", "read more"})


class ReuScraper(ScraperBase):
    """Scraper for PathwaysToScience.org undergraduate REU/research listings.

    PathwaysToScience is a free, keyless, searchable database of 600+ REUs and
    related funded research opportunities. This scraper reads the undergraduate
    "Summer Research (REU)" listing page (GET only) and returns its programs.

    Note: listing titles are program/institution names, so keyword filtering is
    best-effort; leaving keywords empty browses the current REU listing.
    """

    source_name = "pathwaystoscience"

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Fetch the REU listing and return keyword-matching programs.

        Args:
            search_terms: Keywords to filter titles by (empty browses all).

        Returns:
            Deduplicated :class:`Opportunity` records for REU/research programs.
        """
        keywords = [t.strip() for t in search_terms if t.strip()]
        self.errors = []
        results: list[Opportunity] = []
        seen: set[str] = set()

        try:
            response = self.http_get(PATHWAYS_URL, params=PATHWAYS_PARAMS)
        except ScraperError as exc:
            self.errors.append(f"{self.source_name}: {exc}")
            return results

        for href, raw_title in _PTS_ANCHOR.findall(response.text):
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            if title.lower() in _PTS_SKIP:
                continue
            url = f"https://www.pathwaystoscience.org/{href}"
            if url in seen:
                continue
            seen.add(url)
            if not _matches(title, keywords):
                continue
            results.append(
                Opportunity(
                    title=title,
                    url=url,
                    source=self.source_name,
                    date_posted=None,
                    description="REU / undergraduate research (PathwaysToScience)",
                )
            )
        return results


# --- Optional web-search fallback -----------------------------------------


class WebSearchScraper(ScraperBase):
    """Web-search fallback for the long tail of niche programs.

    Uses a keyed search API (there is no reliable keyless web search -- the
    common HTML endpoints bot-block automated requests). Configure via env:

    * ``OPPORTUNITY_SEARCH_KEY``      -- the API key (required to enable).
    * ``OPPORTUNITY_SEARCH_PROVIDER`` -- ``"brave"`` (default) or ``"serpapi"``.

    When no key is set, :meth:`fetch` is a no-op that records a single note
    explaining how to enable it, so the rest of a run still succeeds.

    Attributes:
        query_suffix: Appended to each keyword to focus the search
            (e.g. ``"fellowship program"``).
        provider: Resolved provider name.
        api_key: Resolved API key (or empty string when disabled).
    """

    source_name = "websearch"

    def __init__(self, query_suffix: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize the web-search fallback.

        Args:
            query_suffix: Text appended to each keyword to focus the query.
            timeout: Network timeout in seconds.
        """
        super().__init__(timeout)
        self.query_suffix: str = query_suffix
        self.provider: str = os.environ.get(ENV_SEARCH_PROVIDER, "brave").strip().lower()
        self.api_key: str = os.environ.get(ENV_SEARCH_KEY, "").strip()

    @property
    def enabled(self) -> bool:
        """Whether a usable API key is configured."""
        return bool(self.api_key)

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Search each keyword and return web results as opportunities.

        Args:
            search_terms: Keywords to search (each combined with the suffix).

        Returns:
            Deduplicated web results, or an empty list when disabled.
        """
        keywords = [t.strip() for t in search_terms if t.strip()]
        self.errors = []
        if not self.enabled:
            self.errors.append(
                f"web-search disabled: set {ENV_SEARCH_KEY} "
                f"(and {ENV_SEARCH_PROVIDER}=brave|serpapi) to enable."
            )
            return []
        if not keywords:
            return []

        results: list[Opportunity] = []
        seen: set[str] = set()
        for keyword in keywords:
            query = f"{keyword} {self.query_suffix}".strip()
            try:
                hits = self._search(query)
            except ScraperError as exc:
                self.errors.append(f"{self.provider}: {exc}")
                continue
            for opp in hits:
                if opp.url in seen:
                    continue
                seen.add(opp.url)
                results.append(opp)
        return results

    def _search(self, query: str) -> list[Opportunity]:
        """Dispatch a single query to the configured provider.

        Args:
            query: The full search query.

        Returns:
            Opportunities parsed from the provider response.

        Raises:
            ScraperError: On network/response failures.
            ValueError: On an unknown provider.
        """
        match self.provider:
            case "brave":
                return self._search_brave(query)
            case "serpapi":
                return self._search_serpapi(query)
            case other:
                raise ValueError(f"Unknown search provider: {other!r}.")

    def _search_brave(self, query: str) -> list[Opportunity]:
        """Query the Brave Search API and map results to opportunities."""
        # Brave requires the key in a header; keep it off the URL/query string.
        self._session.headers.update(
            {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        )
        response = self.http_get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": str(_SEARCH_RESULTS_PER_TERM)},
        )
        data = response.json()
        match data:
            case {"web": {"results": list() as items}}:
                pass
            case _:
                return []
        return [
            Opportunity(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("url", "")),
                source=f"{self.source_name}:brave",
                date_posted=item.get("age"),
                description=str(item.get("description", ""))[:300],
            )
            for item in items
            if isinstance(item, dict) and item.get("url")
        ]

    def _search_serpapi(self, query: str) -> list[Opportunity]:
        """Query SerpAPI (Google engine) and map results to opportunities."""
        response = self.http_get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "engine": "google",
                "num": str(_SEARCH_RESULTS_PER_TERM),
                "api_key": self.api_key,
            },
        )
        data = response.json()
        match data:
            case {"organic_results": list() as items}:
                pass
            case _:
                return []
        return [
            Opportunity(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("link", "")),
                source=f"{self.source_name}:serpapi",
                date_posted=item.get("date"),
                description=str(item.get("snippet", ""))[:300],
            )
            for item in items
            if isinstance(item, dict) and item.get("link")
        ]


# --- Composite scraper -----------------------------------------------------


class CompositeScraper(ScraperBase):
    """Runs several scrapers and merges their deduplicated results.

    Used to pair a primary source (curated lists, REU database) with the
    web-search fallback for the long tail.

    Attributes:
        scrapers: The child scrapers to run, in order.
    """

    source_name = "composite"

    def __init__(self, scrapers: list[ScraperBase], timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize with the child scrapers.

        Args:
            scrapers: Scrapers to run and merge.
            timeout: Unused here; children keep their own timeouts.
        """
        super().__init__(timeout)
        self.scrapers: list[ScraperBase] = scrapers

    def fetch(self, search_terms: list[str]) -> list[Opportunity]:
        """Run every child scraper and return deduplicated results.

        Args:
            search_terms: Keywords passed to each child.

        Returns:
            Deduplicated :class:`Opportunity` records across all children.
        """
        self.errors = []
        seen: set[str] = set()
        results: list[Opportunity] = []
        for child in self.scrapers:
            for opp in child.fetch(search_terms):
                key = opp.url or f"{opp.source}|{opp.title}"
                if key in seen:
                    continue
                seen.add(key)
                results.append(opp)
            self.errors.extend(child.errors)
        return results

    def close(self) -> None:
        """Close this scraper and every child."""
        super().close()
        for child in self.scrapers:
            child.close()


# --- Category → scraper factory -------------------------------------------


def build_scraper(category: str, companies: list[str]) -> ScraperBase:
    """Return the appropriate scraper for a configuration category.

    Program-style categories pair their primary source with the optional
    web-search fallback (active only when an API key is configured).

    Args:
        category: One of ``"jobs"``, ``"programs"``, ``"leadership"``,
            ``"research"``.
        companies: Company slugs (used only by the ``jobs`` category).

    Returns:
        A ready-to-use :class:`ScraperBase`.
    """
    match category:
        case "jobs":
            return CompanyJobsScraper(companies)
        case "programs":
            return CompositeScraper([GitHubListScraper(), WebSearchScraper("fellowship program apply")])
        case "leadership":
            return CompositeScraper(
                [GitHubListScraper(), WebSearchScraper("student leadership development program")]
            )
        case "research":
            return CompositeScraper(
                [ReuScraper(), WebSearchScraper("undergraduate research REU program")]
            )
        case _:
            return GitHubListScraper()
