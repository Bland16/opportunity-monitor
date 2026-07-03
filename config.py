"""Configuration persistence for the job/opportunity monitor.

Stores the user's saved search configuration -- organized into named
*categories* (``jobs``, ``programs``, ``leadership``), each holding its own
list of ``companies`` and ``keywords`` -- in a local JSON file. It has no
knowledge of scraping or the UI and can be imported independently.

Public API:

* :func:`load_config` -> :class:`AppConfig`
* :func:`save_config` -> ``None``

Storage layout (``~/.job_monitor/config.json``)::

    {
        "version": 2,
        "categories": {
            "jobs":       {"companies": ["stripe"], "keywords": ["backend"]},
            "programs":   {"companies": [], "keywords": ["fellowship"]},
            "leadership": {"companies": [], "keywords": ["leadership"]}
        }
    }

Legacy ``{"search_terms": [...]}`` files (schema v1) are migrated
automatically into ``categories.jobs.keywords`` on load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# --- Storage location ------------------------------------------------------

#: Directory that holds the config file (created on demand).
CONFIG_DIR: Final[Path] = Path.home() / ".job_monitor"

#: Full path to the JSON config file.
CONFIG_PATH: Final[Path] = CONFIG_DIR / "config.json"

#: Categories seeded into a fresh config and always guaranteed to exist.
DEFAULT_CATEGORIES: Final[tuple[str, ...]] = ("jobs", "programs", "leadership", "research")

#: Current on-disk schema version.
SCHEMA_VERSION: Final[int] = 2


class ConfigError(Exception):
    """Raised when the config file exists but cannot be read or parsed."""


# --- Data model ------------------------------------------------------------


@dataclass(slots=True)
class CategoryConfig:
    """Saved configuration for a single category.

    Attributes:
        companies: Company/board identifiers to query (used by ATS scrapers).
        keywords: Search terms used to filter matching opportunities.
    """

    companies: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    """Top-level application configuration: a set of named categories.

    Attributes:
        categories: Mapping of category name to its :class:`CategoryConfig`.
    """

    categories: dict[str, CategoryConfig] = field(default_factory=dict)

    def category(self, name: str) -> CategoryConfig:
        """Return the category, creating an empty one if it does not exist.

        Args:
            name: Category name (e.g. ``"jobs"``).

        Returns:
            The existing or newly-created :class:`CategoryConfig`.
        """
        return self.categories.setdefault(name, CategoryConfig())


# --- (De)serialization -----------------------------------------------------


def _empty_config() -> AppConfig:
    """Build an :class:`AppConfig` with every default category present, empty.

    Returns:
        A fresh config with :data:`DEFAULT_CATEGORIES` seeded.
    """
    return AppConfig(categories={name: CategoryConfig() for name in DEFAULT_CATEGORIES})


def _config_to_dict(config: AppConfig) -> dict:
    """Serialize an :class:`AppConfig` to a JSON-ready dict.

    Args:
        config: Configuration to serialize.

    Returns:
        A plain dict matching the on-disk schema.
    """
    return {
        "version": SCHEMA_VERSION,
        "categories": {
            name: {"companies": cat.companies, "keywords": cat.keywords}
            for name, cat in config.categories.items()
        },
    }


def _config_from_dict(data: object) -> AppConfig:
    """Parse a loaded JSON object into an :class:`AppConfig`.

    Handles both the current schema and the legacy v1 ``search_terms`` format.

    Args:
        data: The object produced by :func:`json.loads`.

    Returns:
        The parsed configuration, with all default categories guaranteed present.

    Raises:
        ConfigError: If the structure matches neither the current nor legacy schema.
    """
    match data:
        # --- Current schema (v2) -------------------------------------------
        case {"categories": dict() as raw_categories}:
            categories: dict[str, CategoryConfig] = {}
            for name, raw in raw_categories.items():
                match raw:
                    case {"companies": list() as companies, "keywords": list() as keywords}:
                        categories[str(name)] = CategoryConfig(
                            companies=[str(c) for c in companies],
                            keywords=[str(k) for k in keywords],
                        )
                    case _:
                        raise ConfigError(
                            f"Category {name!r} is malformed; expected "
                            '{"companies": [...], "keywords": [...]}.'
                        )
            # Guarantee the default categories always exist for the UI.
            for name in DEFAULT_CATEGORIES:
                categories.setdefault(name, CategoryConfig())
            return AppConfig(categories=categories)

        # --- Legacy schema (v1): migrate flat term list into 'jobs' --------
        case {"search_terms": list() as terms}:
            migrated = _empty_config()
            migrated.category("jobs").keywords = [str(t) for t in terms]
            return migrated

        case _:
            raise ConfigError(
                "Config has an unexpected structure; expected a JSON object "
                'with a "categories" mapping.'
            )


# --- Public API ------------------------------------------------------------


def _ensure_config_file(path: Path) -> None:
    """Create the config directory and a fresh, empty config file if missing.

    A missing file is never an error: it is seeded with the default categories.

    Args:
        path: Target path of the config file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            json.dumps(_config_to_dict(_empty_config()), indent=2),
            encoding="utf-8",
        )


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load the saved configuration from disk.

    On first run the file is created with empty default categories -- a missing
    config is never treated as an error. Legacy v1 files are migrated in memory
    (the migrated form is written back on the next :func:`save_config`).

    Args:
        path: Location of the config file. Defaults to :data:`CONFIG_PATH`.

    Returns:
        The parsed :class:`AppConfig`.

    Raises:
        ConfigError: If the file exists but contains invalid JSON or an
            unrecognized structure.
    """
    _ensure_config_file(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}.") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Config file {path} contains invalid JSON: {exc}."
        ) from exc

    return _config_from_dict(data)


def save_config(config: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Persist configuration to disk, overwriting any previous values.

    Args:
        config: Configuration to save.
        path: Location of the config file. Defaults to :data:`CONFIG_PATH`.

    Raises:
        ValueError: If ``config`` is not an :class:`AppConfig`.
        ConfigError: If the file cannot be written.
    """
    if not isinstance(config, AppConfig):
        raise ValueError("config must be an AppConfig instance.")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_config_to_dict(config), indent=2)

    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not write config file {path}: {exc}.") from exc
