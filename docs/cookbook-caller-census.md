# Cookbook UI — caller census (OD-54, evidence for OD-3)

Date: 2026-10-20. Repo state: default branch, clean tree.

Census of the seven cookbook UI files in `static/js/`: for each file, its
callers (static imports, dynamic `import()`, and `<script>` tags), its route
registration, and whether anything actually reaches it. This is the
prerequisite evidence for the OD-3 consolidation decision and stands on its
own as a reachability audit.

## Method

For each file `<name>`:

```sh
grep -r "<name>\.js" --include='*.js' --include='*.jsx' --include='*.ts' \
  --include='*.tsx' --include='*.html' --exclude-dir=.git . \
  | grep -v "static/js/<name>.js" | grep -v 'cookbook-caller-census' | wc -l
```

Counts below are raw grep hits from external files (comment mentions included
where noted); the caller tables list only real load-time references.

**Positive control.** The control grep runs the same command against
`cookbook.js`, a file known to be reachable (script tag + multiple static
imports). It must print a nonzero count, so that any future run of this census
printing `0 (control: 15)` for a file means "no external references", not
"the grep is broken". Control result at census time: **15**.

## Census

| File | External-reference count | Reached? |
|---|---|---|
| `static/js/cookbook.js` | 15 (control) | YES — script tag + static imports |
| `static/js/cookbookRunning.js` | 3 | YES — static import from cookbook.js |
| `static/js/cookbookServe.js` | 8 | YES — static import from cookbook.js + 4 dynamic imports |
| `static/js/cookbook-hwfit.js` | 6 | YES — static import from cookbook.js + dynamic import |
| `static/js/cookbook-diagnosis.js` | 5 | YES — static imports from 3 cookbook modules |
| `static/js/cookbookDownload.js` | 1 | YES — static import from cookbook.js |
| `static/js/cookbookSchedule.js` | 2 | YES — classic `<script>` tag in index.html (no JS importers) |

No file in the census is orphaned; every one is reachable from the served
page.

### static/js/cookbook.js — entry module

Route: served app shell — `static/index.html:2943` `<script type="module">`;
`docs/UI_SURFACE_REGISTER.json` maps route `/cookbook` (handler
`serve_cookbook`, `app.py`) to surface `cookbook`, whose file list includes
`cookbook.js`, `cookbookRunning.js`, `cookbook-hwfit.js`.

Callers:
- `static/index.html:2943` — `<script type="module" src="/static/js/cookbook.js">`
- `static/app.js:40` — `import cookbookModule from './js/cookbook.js'`
- `static/js/slashCommands.js:22` — static import (`/cookbook`, `/tour-cookbook` commands)
- `static/js/chatStream.js:168` — dynamic `import('./cookbook.js')`
- Circular back-imports from `cookbook-diagnosis.js` and `cookbook-hwfit.js`
  (plain specifier, no `?v=` — deliberate, see comments at `app.js:35-38`
  and `cookbook-diagnosis.js:22-23`; `sw.js:41` precaches it)

### static/js/cookbookRunning.js

Callers:
- `static/js/cookbook.js:16-22` — static import (`initRunning`, task helpers)
- Registered in `docs/UI_SURFACE_REGISTER.json` under surface `cookbook` and
  as its own analyzed module; read by `tests/test_cookbook_endpoint_registration.py`,
  `tests/test_cookbook_dependency_completion_regression.py`,
  `tests/test_cookbook_same_host_server_profiles_js.py`.

### static/js/cookbookServe.js

Callers:
- `static/js/cookbook.js:30-33` — static import (`initServe`, cache helpers)
- `static/js/cookbook-hwfit.js:1277` — dynamic `import('./cookbookServe.js')`
- `static/js/cookbookRunning.js:1277, 1987` — dynamic `import('./cookbookServe.js')`
- `static/js/slashCommands.js:1390` — dynamic `import('./cookbookServe.js')`

### static/js/cookbook-hwfit.js

Callers:
- `static/js/cookbook.js:11` — static import (`_hwfitInit`, cache/render helpers)
- `static/js/cookbookRunning.js:135` — dynamic `import('./cookbook-hwfit.js')`
- Circular back-import of `cookbook.js` (line 28-31); CSS hook at
  `static/style.css:19069`; read by `tests/test_cookbook_same_host_server_profiles_js.py`.

### static/js/cookbook-diagnosis.js

Callers:
- `static/js/cookbook.js:10` — static import (`_diagnose`, `_showDiagnosis`, `_clearDiagnosis`, `_runQuickCmd`, `ERROR_PATTERNS`)
- `static/js/cookbookDownload.js:8` — static import
- `static/js/cookbookRunning.js:8` — static import
- Backend mirror: `routes/cookbook_helpers.py:910` documents the server-side
  equivalent of this file's patterns.

### static/js/cookbookDownload.js

Callers:
- `static/js/cookbook.js:24-28` — static import (`initDownload`, panel helpers)
- Read by `tests/test_cookbook_download_toast_duration.py`,
  `tests/test_cookbook_same_host_server_profiles_js.py`.

### static/js/cookbookSchedule.js

Callers:
- `static/index.html:2944` — `<script src="/static/js/cookbookSchedule.js">`
  (classic script, not `type="module"` — loaded eagerly with the page; it is
  the only load-time reference).
- Comment-only cross-references (not callers): `static/js/calendar.js:2756`
  (reads the `cookbook_task_id:` marker this file writes),
  `static/js/cookbookServe.js:438` (defers events to this file),
  `routes/task_routes.py:28`.

## Notes for the OD-3 consolidation decision

- Nothing is dead code by reachability: all seven files load in the served
  app (six via `cookbook.js`'s import graph, `cookbookSchedule.js` via its
  own script tag).
- `cookbook.js` is a hub with circular imports to `cookbook-diagnosis.js`
  and `cookbook-hwfit.js`; consolidation must preserve the plain-specifier
  rule noted in `app.js:35-38` (a `?v=` mismatch double-loads modules).
- `cookbookSchedule.js` is the odd one out: classic script, no importers,
  communicates via DOM events and calendar description markers.
- `cookbookProgressSignal.js` (imported by `cookbookRunning.js:10`) is not
  one of the seven census files but is likewise reachable.

## Acceptance grep (OD-54)

```sh
grep -r 'cookbook-diagnosis\|cookbookDownload\|cookbookSchedule' \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' . \
  | grep -v 'cookbook-caller-census' | wc -l
```

Result at census time: **23** (nonzero — the three files have live
cross-references; matches include import statements, comment mentions, and
CSS-selector strings inside these modules).
