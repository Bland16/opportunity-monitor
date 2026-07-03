"""Command-line interaction layer for the job/opportunity monitor.

A thin CLI alternative to :mod:`webui`. It orchestrates :mod:`config` and
:mod:`scraper` and contains no persistence or scraping logic of its own,
keeping the dependency graph acyclic.

It preserves the edit-then-confirm contract per category:

* Launch shows the ``Saved`` companies/keywords for each category.
* Editing shows an ``[UNSAVED DRAFT]`` banner; nothing persists until the
  explicit ``confirm`` command.
* ``cancel`` discards the draft and returns to the saved state.

For most users the web UI (``python webui.py``) is friendlier; this CLI exists
for headless/scripted use.
"""

from __future__ import annotations

import sys

import autogen
import config
import scraper

_SAVED_LABEL = "Saved"
_DRAFT_LABEL = "[UNSAVED DRAFT]"
_CATEGORIES = ("jobs", "programs", "leadership", "research")


def _prompt(message: str) -> str:
    """Read a line of input, stripped; returns ``"quit"`` on EOF.

    Args:
        message: Prompt text.

    Returns:
        The stripped user input, or ``"quit"`` when input is closed.
    """
    try:
        return input(message).strip()
    except EOFError:
        return "quit"


def _render_category(name: str, cat: config.CategoryConfig, label: str) -> str:
    """Format one category's companies and keywords under a labeled heading.

    Args:
        name: Category name.
        cat: The category configuration to display.
        label: Heading label (``"Saved"`` or ``"[UNSAVED DRAFT]"``).

    Returns:
        A human-readable multi-line string.
    """
    companies = ", ".join(cat.companies) or "(none)"
    keywords = ", ".join(cat.keywords) or "(none)"
    return (
        f"{label} · {name}\n"
        f"  companies: {companies}\n"
        f"  keywords:  {keywords}"
    )


def _edit_list(current: list[str], noun: str) -> list[str]:
    """Interactively add/remove entries in a draft list.

    Args:
        current: Starting values (copied; the caller's list is untouched).
        noun: Word describing the entries (e.g. ``"keyword"``) for prompts.

    Returns:
        The edited draft list.
    """
    draft = list(current)
    print(f"  Editing {noun}s. Commands: add <text> | remove <n> | done")
    while True:
        shown = ", ".join(f"[{i}] {v}" for i, v in enumerate(draft)) or "(none)"
        print(f"  {_DRAFT_LABEL} {noun}s: {shown}")
        raw = _prompt(f"  {noun}> ")
        command, _, argument = raw.partition(" ")
        argument = argument.strip()
        match command.lower():
            case "add":
                # Reuse the scraper's validation so drafts stay clean.
                try:
                    [clean] = scraper.ScraperBase.validate_search_terms([argument])
                except ValueError as exc:
                    print(f"    ! {exc}")
                    continue
                draft.append(clean)
            case "remove":
                if argument.isdigit() and 0 <= int(argument) < len(draft):
                    draft.pop(int(argument))
                else:
                    print("    ! Usage: remove <valid index>")
            case "done" | "":
                return draft
            case _:
                print("    ! Unknown command.")


def _run_scrape(name: str, cat: config.CategoryConfig) -> None:
    """Run the scraper for one category and print results or a clear error.

    Args:
        name: Category name (selects the scraper).
        cat: The saved category configuration.
    """
    if name == "jobs" and not cat.companies:
        print("  ! 'jobs' needs at least one company. Edit companies first.")
        return

    active = scraper.build_scraper(name, cat.companies)
    print(f"\n  Running '{name}'…")
    try:
        results = active.fetch(cat.keywords)
    except scraper.ScraperError as exc:
        print(f"  ! Scrape failed: {exc}")
        return
    finally:
        errors = list(active.errors)
        active.close()

    for note in errors:
        print(f"  · note: {note}")
    if not results:
        print("  No opportunities found.")
        return
    print(f"  Found {len(results)}:")
    for opp in results[:50]:
        posted = opp.date_posted or "—"
        print(f"   - {opp.title} ({opp.source}, {posted})\n     {opp.url}")


def _edit_category(app_config: config.AppConfig, name: str) -> None:
    """Run the edit-then-confirm flow for one category.

    Args:
        app_config: The live configuration (mutated only on confirm).
        name: Category to edit.
    """
    saved = app_config.category(name)
    draft = config.CategoryConfig(companies=list(saved.companies), keywords=list(saved.keywords))
    print("\nEntering edit mode. Nothing is saved until you 'confirm'.")
    while True:
        print("\n" + _render_category(name, draft, _DRAFT_LABEL))
        print("  Commands: companies | keywords | confirm | cancel")
        match _prompt("edit> ").lower():
            case "companies" | "c":
                draft.companies = _edit_list(draft.companies, "company")
            case "keywords" | "k":
                draft.keywords = _edit_list(draft.keywords, "keyword")
            case "confirm":
                saved.companies = draft.companies
                saved.keywords = draft.keywords
                try:
                    config.save_config(app_config)
                except (config.ConfigError, ValueError) as exc:
                    print(f"  ! Failed to save: {exc}")
                    return
                print(f"{_SAVED_LABEL}: '{name}' confirmed and persisted.")
                return
            case "cancel":
                print("Draft discarded.")
                return
            case _:
                print("  ! Unknown command.")


def _autogen_category(app_config: config.AppConfig, name: str) -> None:
    """Generate a draft from a description, then confirm before saving.

    Args:
        app_config: The live configuration (mutated only on confirm).
        name: Category to populate.
    """
    description = _prompt("Describe what you're looking for> ")
    if not description:
        print("  ! No description given.")
        return
    print("  Generating with Claude…")
    try:
        result = autogen.generate(description, name)
    except autogen.AutogenError as exc:
        print(f"  ! {exc}")
        return

    draft = config.CategoryConfig(companies=result["companies"], keywords=result["keywords"])
    print("\n" + _render_category(name, draft, _DRAFT_LABEL))
    if _prompt("Confirm and save this draft? (yes/no)> ").lower() in ("y", "yes"):
        cat = app_config.category(name)
        cat.companies = draft.companies
        cat.keywords = draft.keywords
        try:
            config.save_config(app_config)
        except (config.ConfigError, ValueError) as exc:
            print(f"  ! Failed to save: {exc}")
            return
        print(f"{_SAVED_LABEL}: '{name}' confirmed and persisted.")
    else:
        print("Draft discarded.")


def main() -> int:
    """Program entry point: load config, run the CLI menu.

    Returns:
        Process exit code (0 on success, 1 on unrecoverable config error).
    """
    print("=== Opportunity Monitor (CLI · read-only) ===")
    try:
        app_config = config.load_config()
    except config.ConfigError as exc:
        print(f"Could not load configuration: {exc}", file=sys.stderr)
        return 1

    while True:
        print()
        for name in _CATEGORIES:
            print(_render_category(name, app_config.category(name), _SAVED_LABEL))
        print("\nCommands: edit <category> | autogen <category> | run <category> | quit")
        raw = _prompt("menu> ")
        command, _, argument = raw.partition(" ")
        argument = argument.strip().lower()
        match command.lower():
            case "edit" if argument in _CATEGORIES:
                _edit_category(app_config, argument)
            case "autogen" if argument in _CATEGORIES:
                _autogen_category(app_config, argument)
            case "run" if argument in _CATEGORIES:
                _run_scrape(argument, app_config.category(argument))
            case "quit" | "q" | "exit":
                print("Goodbye.")
                return 0
            case "edit" | "run" | "autogen":
                print(f"  ! Specify a category: {', '.join(_CATEGORIES)}")
            case "":
                continue
            case _:
                print("  ! Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
