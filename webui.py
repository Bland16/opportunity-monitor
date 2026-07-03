"""Local web UI for the job/opportunity monitor.

Serves a single-page interface on ``http://127.0.0.1:8765`` using only the
Python standard library (``http.server``) -- no third-party web framework. It
is the presentation/interaction layer only: it orchestrates :mod:`config`,
:mod:`scraper`, and :mod:`autogen` and adds no persistence or scraping logic
of its own.

The interface has two pages (tabs): **Configure** (write the config) and
**Results** (run scrapers and view output). It preserves the required
edit-then-confirm contract:

* Saved terms load on launch, labeled ``Saved``.
* Editing a category shows an ``[UNSAVED DRAFT]`` badge; nothing is written
  until the explicit **Confirm & Save** action (``POST /api/config``).
* **Cancel** discards the draft and reloads the saved state.

Company slugs (only relevant to the ``jobs`` category) live under a collapsed
**Advanced** section, so the common case is just keywords.

Run:
    python webui.py
"""

from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import autogen
import config
import scraper

# --- Server configuration --------------------------------------------------

HOST: str = "127.0.0.1"
PORT: int = 8765

#: Category metadata driving the UI (order + which fields/hints to show).
CATEGORY_META: tuple[dict[str, object], ...] = (
    {
        "key": "jobs",
        "title": "Jobs",
        "hasCompanies": True,
        "hint": "Live company job boards (Greenhouse / Lever / Ashby). "
        "Add role keywords; pick specific companies under Advanced.",
    },
    {
        "key": "programs",
        "title": "Programs",
        "hasCompanies": False,
        "hint": "Curated lists of fellowships & programs. Keywords, e.g. fellowship, research, scholars.",
    },
    {
        "key": "leadership",
        "title": "Leadership",
        "hasCompanies": False,
        "hint": "Leadership / rotational / early-talent programs. Keywords, e.g. leadership, rotational, LDP, bold.",
    },
    {
        "key": "research",
        "title": "Research (REU)",
        "hasCompanies": False,
        "hint": "PathwaysToScience REU database. Leave keywords empty to browse, or filter, e.g. biology, data.",
    },
)


# --- Config <-> JSON -------------------------------------------------------


def _config_to_json(app_config: config.AppConfig) -> dict[str, dict[str, list[str]]]:
    """Convert an :class:`config.AppConfig` to a JSON-ready dict for the client.

    Args:
        app_config: Loaded configuration.

    Returns:
        Mapping of category -> ``{"companies": [...], "keywords": [...]}``.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for meta in CATEGORY_META:
        name = str(meta["key"])
        cat = app_config.category(name)
        out[name] = {"companies": cat.companies, "keywords": cat.keywords}
    return out


def _validate_terms(values: list[str], label: str, max_len: int) -> list[str]:
    """Strip, drop blanks, and length-check a list of user-entered strings.

    Args:
        values: Raw strings from the client.
        label: Field label used in error messages.
        max_len: Maximum allowed length per entry.

    Returns:
        The cleaned list.

    Raises:
        ValueError: If any entry exceeds ``max_len`` characters.
    """
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped:
            continue
        if len(stripped) > max_len:
            raise ValueError(
                f"{label} entry is {len(stripped)} characters; max is {max_len}."
            )
        cleaned.append(stripped)
    return cleaned


# --- Request handler -------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """HTTP handler serving the SPA and the small JSON API."""

    # -- helpers ------------------------------------------------------------

    def _send(self, code: int, body: str | bytes, ctype: str = "application/json") -> None:
        """Write a full HTTP response.

        Args:
            code: HTTP status code.
            body: Response body (str is UTF-8 encoded).
            ctype: Content-Type header value.
        """
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code: int, payload: object) -> None:
        """Serialize ``payload`` to JSON and send it."""
        self._send(code, json.dumps(payload))

    def _read_json(self) -> dict:
        """Read and parse a JSON request body.

        Returns:
            The decoded object, or an empty dict on empty/invalid bodies.
        """
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    def log_message(self, *_args: object) -> None:  # noqa: D401 - silence default logging
        """Suppress the default per-request stderr logging."""

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required name
        """Route GET requests."""
        path = urlparse(self.path).path
        match path:
            case "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            case "/api/config":
                try:
                    self._send_json(200, _config_to_json(config.load_config()))
                except config.ConfigError as exc:
                    self._send_json(500, {"error": str(exc)})
            case _:
                self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - required name
        """Route POST requests."""
        path = urlparse(self.path).path
        match path:
            case "/api/config":
                self._handle_save(self._read_json())
            case "/api/run":
                self._handle_run(self._read_json())
            case "/api/autogen":
                self._handle_autogen(self._read_json())
            case _:
                self._send_json(404, {"error": "not found"})

    # -- actions ------------------------------------------------------------

    def _handle_save(self, payload: dict) -> None:
        """Persist a single category's draft (the Confirm action).

        Args:
            payload: ``{"category": str, "companies": [...], "keywords": [...]}``.
        """
        category = str(payload.get("category", "")).strip()
        if category not in {str(m["key"]) for m in CATEGORY_META}:
            self._send_json(400, {"error": f"Unknown category: {category!r}."})
            return
        try:
            companies = _validate_terms(payload.get("companies", []), "Company", 100)
            keywords = _validate_terms(payload.get("keywords", []), "Keyword", scraper.MAX_TERM_LENGTH)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            app_config = config.load_config()
            cat = app_config.category(category)
            cat.companies = companies
            cat.keywords = keywords
            config.save_config(app_config)
        except (config.ConfigError, ValueError) as exc:
            self._send_json(500, {"error": f"Failed to save: {exc}"})
            return

        self._send_json(200, {"ok": True, "config": _config_to_json(app_config)})

    def _handle_autogen(self, payload: dict) -> None:
        """Generate a companies/keywords draft from a description via the LLM.

        Args:
            payload: ``{"category": str, "description": str}``.
        """
        category = str(payload.get("category", "")).strip()
        if category not in {str(m["key"]) for m in CATEGORY_META}:
            self._send_json(400, {"error": f"Unknown category: {category!r}."})
            return
        description = str(payload.get("description", "")).strip()
        try:
            result = autogen.generate(description, category)
        except autogen.AutogenError as exc:
            # 200 with an error field so the client renders it inline.
            self._send_json(200, {"error": str(exc)})
            return
        self._send_json(200, {"companies": result["companies"], "keywords": result["keywords"]})

    def _handle_run(self, payload: dict) -> None:
        """Run a scrape for one category and return the results.

        Args:
            payload: ``{"category": str}``.
        """
        category = str(payload.get("category", "")).strip()
        if category not in {str(m["key"]) for m in CATEGORY_META}:
            self._send_json(400, {"error": f"Unknown category: {category!r}."})
            return

        try:
            app_config = config.load_config()
        except config.ConfigError as exc:
            self._send_json(500, {"error": str(exc)})
            return

        cat = app_config.category(category)
        active = scraper.build_scraper(category, cat.companies)
        try:
            results = active.fetch(cat.keywords)
        except scraper.ScraperError as exc:
            self._send_json(200, {"count": 0, "opportunities": [], "errors": [str(exc)]})
            return
        finally:
            errors = list(active.errors)
            active.close()

        self._send_json(
            200,
            {
                "count": len(results),
                "opportunities": [o.to_dict() for o in results],
                "errors": errors,
            },
        )


# --- The single-page app ---------------------------------------------------
# Plain string (not a template) so the CSS/JS braces need no escaping. The
# category metadata is injected as JSON at the marker below.

INDEX_HTML = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opportunity Monitor</title>
<style>
  :root { color-scheme: light; --blue:#2563eb; --blue-d:#1d4ed8; --ink:#1f2937;
          --muted:#64748b; --line:#e2e8f0; --bg:#f4f7fc; --card:#ffffff; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.55 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background: var(--bg); color: var(--ink); }
  header { background: #fff; border-bottom: 1px solid var(--line); padding: 16px 24px 0; }
  .brand { display: flex; align-items: baseline; gap: 10px; }
  header h1 { margin: 0; font-size: 20px; color: var(--blue-d); }
  header .sub { color: var(--muted); font-size: 13px; }
  nav.tabs { display: flex; gap: 4px; margin-top: 14px; }
  nav.tabs button { font: inherit; font-weight: 600; border: none; background: none; cursor: pointer;
                    color: var(--muted); padding: 9px 16px; border-bottom: 3px solid transparent; }
  nav.tabs button.active { color: var(--blue-d); border-bottom-color: var(--blue); }
  main { max-width: 860px; margin: 0 auto; padding: 22px 16px 60px; }
  .page { display: none; }
  .page.active { display: block; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 18px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .card-head h2 { margin: 0; font-size: 17px; }
  .badge { font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 999px; letter-spacing: .3px; }
  .badge.saved { background: #e6efff; color: var(--blue-d); border: 1px solid #c7dbff; }
  .badge.draft { background: #fff3d6; color: #92600b; border: 1px solid #f4d58a; }
  .hint { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; text-transform: uppercase;
          letter-spacing: .4px; font-weight: 600; }
  textarea { width: 100%; min-height: 60px; resize: vertical; background: #fff; color: var(--ink);
             border: 1px solid #cbd5e1; border-radius: 8px; padding: 9px 11px; font: inherit; }
  textarea:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,.15); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  button.act { font: inherit; font-weight: 600; border: 1px solid transparent; border-radius: 8px;
               padding: 8px 15px; cursor: pointer; }
  button.act:disabled { opacity: .55; cursor: default; }
  .primary { background: var(--blue); color: #fff; }
  .primary:hover:enabled { background: var(--blue-d); }
  .ghost { background: #fff; color: #334155; border-color: #cbd5e1; }
  .run { background: var(--blue); color: #fff; }
  .gen-box { background: #f2f7ff; border: 1px solid #cfe0ff; border-radius: 8px; padding: 11px; margin: 6px 0 4px; }
  .gen-box .gen-title { font-size: 12.5px; color: var(--blue-d); font-weight: 600; margin-bottom: 7px; }
  details.advanced { margin-top: 12px; border-top: 1px dashed var(--line); padding-top: 8px; }
  details.advanced summary { cursor: pointer; color: var(--blue-d); font-size: 13px; font-weight: 600; }
  details.advanced .adv-note { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
  .results { margin-top: 6px; }
  .status { font-size: 13px; color: var(--muted); }
  .err { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; border-radius: 8px;
         font-size: 12.5px; margin-top: 8px; padding: 8px 10px; white-space: pre-wrap; }
  ul.opps { list-style: none; margin: 8px 0 0; padding: 0; }
  ul.opps li { padding: 10px 0; border-top: 1px solid var(--line); }
  ul.opps a { color: var(--blue-d); text-decoration: none; font-weight: 600; }
  ul.opps a:hover { text-decoration: underline; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
</style>
</head>
<body>
<header>
  <div class="brand"><h1>Opportunity Monitor</h1><span class="sub">read-only discovery</span></div>
  <nav class="tabs">
    <button id="tab-config" class="active" onclick="showPage('config')">Configure</button>
    <button id="tab-output" onclick="showPage('output')">Results</button>
  </nav>
</header>
<main>
  <section id="config-page" class="page active"><p class="status">Loading…</p></section>
  <section id="output-page" class="page"></section>
</main>

<script>
const META = /*__META__*/ [];

const el = (tag, props={}, kids=[]) => {
  const n = document.createElement(tag);
  Object.entries(props).forEach(([k,v]) => {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else n.setAttribute(k, v);
  });
  (Array.isArray(kids) ? kids : [kids]).forEach(c => c && n.appendChild(c));
  return n;
};

const toText = (arr) => (arr || []).join('\\n');
const toList = (text) => text.split(/[\\n,]/).map(s => s.trim()).filter(Boolean);

let saved = {};

function showPage(name) {
  document.getElementById('config-page').classList.toggle('active', name === 'config');
  document.getElementById('output-page').classList.toggle('active', name === 'output');
  document.getElementById('tab-config').classList.toggle('active', name === 'config');
  document.getElementById('tab-output').classList.toggle('active', name === 'output');
}

function draftOf(key) {
  const kw = document.getElementById(key + '-kw');
  const co = document.getElementById(key + '-co');
  return {
    companies: co ? toList(co.value) : [],
    keywords: kw ? toList(kw.value) : [],
  };
}

function isDirty(key) {
  const d = draftOf(key);
  const s = saved[key] || {companies: [], keywords: []};
  return JSON.stringify(d) !== JSON.stringify({companies: s.companies || [], keywords: s.keywords || []});
}

function refreshBadge(key) {
  const b = document.getElementById(key + '-badge');
  if (!b) return;
  if (isDirty(key)) { b.textContent = '[UNSAVED DRAFT]'; b.className = 'badge draft'; }
  else { b.textContent = 'Saved'; b.className = 'badge saved'; }
}

function fillFromSaved(key) {
  const s = saved[key] || {companies: [], keywords: []};
  const kw = document.getElementById(key + '-kw');
  const co = document.getElementById(key + '-co');
  if (kw) kw.value = toText(s.keywords);
  if (co) co.value = toText(s.companies);
  refreshBadge(key);
}

async function confirmSave(key) {
  const d = draftOf(key);
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category: key, companies: d.companies, keywords: d.keywords}),
  }).then(r => r.json());
  if (res.error) { alert(res.error); return; }
  saved = res.config;
  fillFromSaved(key);
}

async function autogen(key, btn) {
  const desc = document.getElementById(key + '-desc').value.trim();
  if (!desc) { alert('Describe what you are looking for first.'); return; }
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Generating…';
  let res;
  try {
    res = await fetch('/api/autogen', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({category: key, description: desc}),
    }).then(r => r.json());
  } catch (e) { alert('Request failed: ' + e); return; }
  finally { btn.disabled = false; btn.textContent = label; }

  if (res.error) { alert(res.error); return; }
  const kw = document.getElementById(key + '-kw');
  if (kw && res.keywords) kw.value = toText(res.keywords);
  const co = document.getElementById(key + '-co');
  if (co && res.companies) {
    co.value = toText(res.companies);
    const adv = document.getElementById(key + '-adv');   // reveal filled companies
    if (adv && res.companies.length) adv.open = true;
  }
  refreshBadge(key);  // draft now differs from saved → [UNSAVED DRAFT]
}

async function runCategory(key, btn) {
  const out = document.getElementById(key + '-results');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Running…';
  out.innerHTML = '';
  out.appendChild(el('p', {class: 'status', text: 'Checking live sources…'}));
  let res;
  try {
    res = await fetch('/api/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({category: key}),
    }).then(r => r.json());
  } catch (e) { out.innerHTML = ''; out.appendChild(el('p', {class: 'status', text: 'Request failed: ' + e})); return; }
  finally { btn.disabled = false; btn.textContent = label; }

  out.innerHTML = '';
  out.appendChild(el('p', {class: 'status', text: res.count + ' result' + (res.count === 1 ? '' : 's') + (res.error ? ' — ' + res.error : '')}));
  if (res.errors && res.errors.length) {
    out.appendChild(el('div', {class: 'err', text: res.errors.join('\\n')}));
  }
  const ul = el('ul', {class: 'opps'});
  (res.opportunities || []).slice(0, 200).forEach(o => {
    const a = el('a', {href: o.url, target: '_blank', rel: 'noopener', text: o.title || o.url});
    const meta = el('div', {class: 'meta', text: [o.source, o.date_posted, o.description].filter(Boolean).join('  ·  ')});
    ul.appendChild(el('li', {}, [a, meta]));
  });
  out.appendChild(ul);
}

// ---- Configure page card ----
function configCard(meta) {
  const badge = el('span', {id: meta.key + '-badge', class: 'badge saved', text: 'Saved'});
  const head = el('div', {class: 'card-head'}, [el('h2', {text: meta.title}), badge]);
  const kids = [head, el('p', {class: 'hint', text: meta.hint})];

  // Auto-generate box: describe in plain language → LLM fills the draft fields.
  const desc = el('textarea', {id: meta.key + '-desc', placeholder: 'Describe what you\\'re looking for, e.g. "backend internships at fintech startups"'});
  desc.style.minHeight = '46px';
  const genBtn = el('button', {class: 'act primary', text: '✨ Auto-generate'});
  genBtn.addEventListener('click', () => autogen(meta.key, genBtn));
  kids.push(el('div', {class: 'gen-box'}, [
    el('div', {class: 'gen-title', text: '✨ Auto-generate from a description — fills the draft below; review, then Confirm'}),
    desc,
    el('div', {class: 'row'}, [genBtn]),
  ]));

  // Keywords (the common case).
  kids.push(el('label', {text: 'Keywords (one per line or comma-separated)'}));
  const kw = el('textarea', {id: meta.key + '-kw', placeholder: meta.hasCompanies ? 'engineer\\nintern' : 'fellowship\\nresearch'});
  kw.addEventListener('input', () => refreshBadge(meta.key));
  kids.push(kw);

  // Advanced: specific companies (jobs only), collapsed by default.
  if (meta.hasCompanies) {
    const co = el('textarea', {id: meta.key + '-co', placeholder: 'stripe\\nspotify\\nramp'});
    co.addEventListener('input', () => refreshBadge(meta.key));
    const adv = el('details', {class: 'advanced', id: meta.key + '-adv'}, [
      el('summary', {text: 'Advanced — target specific companies (optional)'}),
      el('label', {text: 'Company board slugs (one per line or comma-separated)'}),
      co,
      el('p', {class: 'adv-note', text: 'Slug = the name in a board URL, e.g. boards.greenhouse.io/stripe → "stripe". Leave empty to skip.'}),
    ]);
    kids.push(adv);
  }

  const save = el('button', {class: 'act primary', text: 'Confirm & Save'});
  save.addEventListener('click', () => confirmSave(meta.key));
  const cancel = el('button', {class: 'act ghost', text: 'Cancel'});
  cancel.addEventListener('click', () => fillFromSaved(meta.key));
  kids.push(el('div', {class: 'row'}, [save, cancel]));

  return el('div', {class: 'card'}, kids);
}

// ---- Results page card ----
function outputCard(meta) {
  const run = el('button', {class: 'act run', text: 'Run ▶'});
  run.addEventListener('click', () => runCategory(meta.key, run));
  return el('div', {class: 'card'}, [
    el('div', {class: 'card-head'}, [el('h2', {text: meta.title})]),
    el('p', {class: 'hint', text: 'Run to fetch live results for your saved keywords.'}),
    el('div', {class: 'row'}, [run]),
    el('div', {id: meta.key + '-results', class: 'results'}),
  ]);
}

async function init() {
  saved = await fetch('/api/config').then(r => r.json());
  const cfg = document.getElementById('config-page');
  const out = document.getElementById('output-page');
  cfg.innerHTML = '';
  out.innerHTML = '';
  META.forEach(m => cfg.appendChild(configCard(m)));
  META.forEach(m => out.appendChild(outputCard(m)));
  META.forEach(m => fillFromSaved(m.key));
}
init();
</script>
</body>
</html>
"""
).replace("/*__META__*/ []", json.dumps([
    {"key": m["key"], "title": m["title"], "hasCompanies": m["hasCompanies"], "hint": m["hint"]}
    for m in CATEGORY_META
]))


def main() -> int:
    """Start the local web server and open the UI in a browser.

    Returns:
        Process exit code (0 on clean shutdown).
    """
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"Could not start on {HOST}:{PORT} — is the app already open in another window? ({exc})")
        return 1
    url = f"http://{HOST}:{PORT}/"
    print(f"Opportunity Monitor UI running at {url}")
    print("Read-only discovery. Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - opening a browser is best-effort
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
