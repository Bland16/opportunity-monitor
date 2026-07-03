# Opportunity Monitor

A read-only web-scraping app that helps a student discover **jobs**, **programs**
(fellowships/scholarships/early-talent), and **leadership** programs based on
configurable search terms. Discovery only — it issues HTTP GET requests and
never submits or interacts with any form.

## What it scrapes (and why)

Targets were chosen from a research pass on how students actually find opportunities:

| Category | Source | How |
|---|---|---|
| **jobs** | Company ATS boards — Greenhouse, Lever, Ashby | Keyless public JSON per company slug; filtered by your keywords |
| **programs** | Curated GitHub lists + web-search fallback | Row-level keyword match on community lists of fellowships/programs |
| **leadership** | Curated GitHub lists + web-search fallback | Leadership/rotational keywords (Google BOLD, Microsoft Explore, LDPs, …) |
| **research** | PathwaysToScience REU database + web-search fallback | Keyless REU/undergrad-research listing; leave keywords empty to browse |

Each program-style category also runs an optional **web-search fallback** for the
long tail of niche programs — active only when you set a search API key (below).

Handshake, LinkedIn, and Discord are the highest-traffic student channels but are
login-gated and **not** scrapable — this tool complements them, it doesn't replace them.

## Auto-generate config from a description (Claude API)

Instead of typing companies and keywords by hand, describe what you want in
plain language and let Claude draft them. In each category card, use the
**✨ Auto-generate** box (e.g. *"backend internships at fintech startups"*),
click generate, and the draft fields fill in — **review, then Confirm & Save**.
For `jobs` it produces ATS company slugs + role keywords; for the other
categories it produces keywords. Nothing is saved until you confirm.

This calls the Claude Messages API (`claude-opus-4-8`, structured outputs) over
raw HTTP with `requests` — deliberately, to honor the "no deps beyond
`requests`" constraint. (If that constraint is relaxed, the official `anthropic`
SDK is the cleaner path.) It needs an API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # bash
# PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
# optional model override:  OPPORTUNITY_LLM_MODEL=claude-sonnet-5
```

Without the key, auto-generate shows a clear inline message and everything else
still works. In the CLI: `autogen <category>`.

## Optional: web-search fallback

There is no reliable *keyless* web search (DuckDuckGo's HTML endpoints bot-block
automated requests), so the fallback uses a search API. It's off by default and
each run prints a note telling you it's disabled — everything else still works.
To enable, set two environment variables:

```bash
# Brave Search API (default provider; free tier available)
export OPPORTUNITY_SEARCH_KEY=your_brave_key
# or SerpAPI:
export OPPORTUNITY_SEARCH_PROVIDER=serpapi
export OPPORTUNITY_SEARCH_KEY=your_serpapi_key
```

On Windows (PowerShell): `$env:OPPORTUNITY_SEARCH_KEY = "your_key"` before `python webui.py`.

## Two ways to run it

**A. Browser version — `index.html` (no Python, no server, no API key).**
Double-click `index.html`; it opens in your browser and runs entirely client-side.
Works for **jobs, programs, and leadership** — it fetches the same live sources
directly from the browser (those APIs allow cross-origin requests). Config saves
to your browser's `localStorage`. Keyword/company **suggestion chips** replace the
LLM auto-generate. Two limits, both by design:
- **Research (REU / PathwaysToScience)** is not available here — that site blocks
  browser requests (no CORS headers). Use the Python app for research.
- It works as a **local file**, not as a hosted Claude Artifact (Artifacts block
  all external network calls, so live fetching can't run inside one).

**B. Python app — full version, also no API key needed.** All four categories
**including research**, plus *optional* LLM auto-generate. **Double-click
`start.bat`** (Windows) to launch it — it starts the app and opens your browser,
no terminal required. Only the ✨ Auto-generate button needs a key; everything
else (all scraping, all four categories) runs keyless. To enable Auto-generate
later, put your key in a file named `apikey.txt` next to `start.bat`.

## Requirements

- Python 3.11+ (tested on 3.12)
- Third-party dependency: **`requests`** only. The web UI uses the standard
  library (`http.server`) — no web framework, no `tkinter`.

## Install

```bash
python -m pip install requests
```

## Run

**Web UI (recommended):**
```bash
python webui.py
```
Opens `http://127.0.0.1:8765` — a clean white/blue interface with two tabs:

- **Configure** — per category, set keywords (or use ✨ Auto-generate), then
  **Confirm & Save** (an `[UNSAVED DRAFT]` badge shows until you do); **Cancel**
  discards the draft. Targeting specific companies (jobs only) lives under a
  collapsed **Advanced** section, so the common case is just keywords.
- **Results** — press **Run ▶** for a category to fetch live results.

**CLI (headless/scripted):**
```bash
python ui.py
# menu> edit jobs        (companies | keywords | confirm | cancel)
# menu> run jobs
```

The app ships with sensible starter config (`jobs`: stripe/spotify/ramp +
"engineer"; `programs`: fellowship/research; `leadership`: leadership/rotational)
so you can Run immediately — edit it to taste.

## Configuration storage

Saved to `~/.job_monitor/config.json`, created automatically on first run:

```json
{
  "version": 2,
  "categories": {
    "jobs":       {"companies": ["stripe", "spotify"], "keywords": ["engineer"]},
    "programs":   {"companies": [], "keywords": ["fellowship", "research"]},
    "leadership": {"companies": [], "keywords": ["leadership", "rotational"]}
  }
}
```

Legacy `{"search_terms": [...]}` files are migrated into `categories.jobs.keywords`.

## Directory structure

```
opprotunity_scrapper/
├── README.md
├── start.bat     # Windows double-click launcher for the Python app (no key needed)
├── index.html    # standalone browser version (no Python / server / API key)
├── scraper.py    # Opportunity, ScraperBase + Greenhouse/Lever/Ashby/CompanyJobs/
│                 #   GitHubList/Reu/WebSearch/Composite scrapers
├── config.py     # AppConfig/CategoryConfig persistence (load_config/save_config)
├── autogen.py    # NL description → {companies, keywords} via the Claude API
├── ui.py         # CLI interaction layer
└── webui.py      # localhost web UI (stdlib http.server) + JSON API

~/.job_monitor/
└── config.json   # persisted per-category companies + keywords (auto-created)
```

## Finding company slugs (for the `jobs` category)

A company's slug is the last path segment of its board URL:
`boards.greenhouse.io/**stripe**`, `jobs.lever.co/**spotify**`,
`jobs.ashbyhq.com/**ramp**`. The scraper tries all three ATS boards per slug,
so you only enter the name once.

## Extension points (designed in, no refactor needed)

- **New job sources:** subclass `ScraperBase` (or `_CompanyBoardScraper`) and
  add it to `CompanyJobsScraper._scrapers`.
- **New program lists:** add raw README URLs to `DEFAULT_PROGRAM_REPOS`.
- **Web-search fallback** for the long tail of niche programs: wire a search API
  into a new scraper and register it in `scraper.build_scraper`. (The app can't
  call an LLM web search itself — it needs a search API key.)
- **Scheduling / dedup / notifications:** wrap `scraper.fetch`; results already
  carry a stable `url` for dedup.

## Error handling

Network timeouts/connection failures (`NetworkError`), malformed/unexpected HTTP
responses (`ResponseError`), a missing config on first run (handled silently),
and invalid search-term input (`ValueError`, >200 chars or empty) are all caught
and surfaced with clear messages. A company that doesn't use a given ATS is a
silent skip; genuine failures appear under "Source notes" in the results.
