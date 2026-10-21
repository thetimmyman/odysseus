# Cookbook UI: bounded caller census

Evidence date: 2026-10-21. Source snapshot: PR #168 head
`95830cbb297514d324581fa1de71e22840c87769`. The seven modules,
`static/index.html`, `static/app.js`, the other callers listed below, and
`app.py` were compared with integration branch `dev` at
`3f811f80798ebaffc5534f479623fc99d9373977`; those files were unchanged.
This is source evidence for a consolidation decision, not a browser execution
trace or a repository-wide proof that every function is exercised.

## Reproduce

Run the following from the checkout root. It enumerates tracked JS/JSX/TS/TSX
and HTML, resolves literal module specifiers relative to the source file, and
excludes the target's own source **by path**, never by grepping away matching
content. HTML loads use `HTMLParser`, so the script tag survives exclusion.
The JS extractor is deliberately bounded: literal static `from`/side-effect
imports and literal dynamic `import()` candidates, after removing whole
comment lines. It is not a complete JS parser, bundler, template engine, or
runtime tracer. Inspect the emitted locations before treating candidates as
load edges. The locations below were inspected in their source context; none
is a comment, selector, logger, fixture, or test reference.

```sh
python3 - <<'PY'
from pathlib import Path
from html.parser import HTMLParser
import posixpath
import re
import subprocess

names = ('cookbookRunning', 'cookbookServe', 'cookbook', 'cookbook-hwfit',
         'cookbook-diagnosis', 'cookbookDownload', 'cookbookSchedule')
targets = {f'static/js/{name}.js' for name in names}

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.loads = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.loads.append((self.getpos()[0], 'script', attrs['src']))

def loads(source, text):
    if source.suffix == '.html':
        parser = Scripts()
        parser.feed(text)
        return parser.loads
    # Bounded candidate extraction, not a JavaScript parser. Removing whole
    # comment lines preserves line numbers; inspect every reported edge below.
    text = re.sub(r'(?m)^[ \t]*(?://|/\*|\*).*$', '', text)
    patterns = (
        ('dynamic', r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
        ('static', r"""\bfrom\s*['"]([^'"]+)['"]"""),
        ('static', r"""\bimport\s*['"]([^'"]+)['"]"""),
    )
    return [(text.count('\n', 0, m.start()) + 1, kind, m[1])
            for kind, pattern in patterns for m in re.finditer(pattern, text)]

def edges(source, text):
    found = []
    for line, kind, specifier in loads(source, text):
        specifier = specifier.split('?', 1)[0].split('#', 1)[0]
        destination = posixpath.normpath(
            specifier.lstrip('/') if specifier.startswith('/')
            else str(source.parent / specifier))
        if destination in targets and destination != source.as_posix():
            found.append((destination, source.as_posix(), line, kind))
    return found

paths = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
all_edges = []
for filename in paths:
    source = Path(filename)
    if source.suffix in {'.js', '.jsx', '.ts', '.tsx', '.html'}:
        all_edges.extend(edges(source, source.read_text()))
for name in names:
    target = f'static/js/{name}.js'
    matches = sorted(edge for edge in all_edges if edge[0] == target)
    print(f'{name}: {len(matches)} (control: '
          f'{sum(e[0] == "static/js/cookbook.js" for e in all_edges)})')
    for _, source, line, kind in matches:
        print(f'  {source}:{line} {kind}')
# Actual external positive and noncaller controls, plus deletion sensitivity.
index = Path('static/index.html')
html = index.read_text()
schedule = 'static/js/cookbookSchedule.js'
assert (schedule, str(index), 2944, 'script') in edges(index, html)
assert not edges(Path('static/js/calendar.js'), Path('static/js/calendar.js').read_text())
without_tag = html.replace('<script src="/static/js/cookbookSchedule.js"></script>', '')
assert not any(e[0] == schedule for e in edges(index, without_tag))
assert sum(e[0] == schedule for e in all_edges) == 1
assert not edges(Path(schedule), 'console.log("cookbookSchedule.js");\n'
                 '// import "./cookbookDownload.js";\n'
                 'document.querySelector(".cookbook-diagnosis");')
print('controls: external script present; noncaller=0; removed script=0; mentions=0')
PY
```

The positive control is the real external script reference at
`static/index.html:2944`, not a self-mention or an aggregate count across three
files. The known noncaller `calendar.js` mentions `cookbookSchedule.js` only
in a comment and produces zero load edges. The script also removes the actual
schedule script tag in memory and confirms zero remaining schedule loads;
comment, logger, and CSS-selector mentions produce zero. Assertions fail the
command on a control mismatch. No source file is edited by the controls.

## Inspected results

Counts are **individual external load edges**, not filename grep hits or
function-call counts. A static import is a load dependency; a dynamic import
is a conditional load site and is not proof its containing handler runs.

| Target in `static/js/` | Static imports | Dynamic import sites | HTML script loads | Total |
|---|---:|---:|---:|---:|
| `cookbookRunning.js` | 1 | 0 | 0 | 1 |
| `cookbookServe.js` | 1 | 4 | 0 | 5 |
| `cookbook.js` (count control) | 4 | 1 | 1 | 6 |
| `cookbook-hwfit.js` | 1 | 1 | 0 | 2 |
| `cookbook-diagnosis.js` | 3 | 0 | 0 | 3 |
| `cookbookDownload.js` | 1 | 0 | 0 | 1 |
| `cookbookSchedule.js` (external script control) | 0 | 0 | 1 | 1 |

Literal source locations (a multiline static import is reported at its `from`):

- `cookbookRunning.js`: `static/js/cookbook.js:22` (static).
- `cookbookServe.js`: `static/js/cookbook.js:33` (static);
  `static/js/cookbook-hwfit.js:1277`,
  `static/js/cookbookRunning.js:1277,1987`, and
  `static/js/slashCommands.js:1390` (dynamic).
- `cookbook.js`: `static/app.js:40`,
  `static/js/cookbook-diagnosis.js:24`,
  `static/js/cookbook-hwfit.js:31`, and
  `static/js/slashCommands.js:22` (static);
  `static/js/chatStream.js:168` (dynamic);
  `static/index.html:2943` (module script).
- `cookbook-hwfit.js`: `static/js/cookbook.js:11` (static);
  `static/js/cookbookRunning.js:135` (dynamic).
- `cookbook-diagnosis.js`: `static/js/cookbook.js:10`,
  `static/js/cookbookDownload.js:8`, and
  `static/js/cookbookRunning.js:8` (static).
- `cookbookDownload.js`: `static/js/cookbook.js:28` (static).
- `cookbookSchedule.js`: `static/index.html:2944` (classic script).

## Route and interpretation

`app.py:814-816` registers `/cookbook` to `serve_cookbook`, which delegates
to `serve_index`. `serve_index` at `app.py:792-800` reads `static/index.html`
when present, with a root `index.html` fallback. Thus this **static HTML branch**
contains direct loads for `cookbook.js` and `cookbookSchedule.js`; the former
statically imports the other five census modules. They have a source load
path from that shell. They are not seven separately registered page routes.

This supports retaining all seven modules while considering consolidation;
it does not prove successful module evaluation, CSP/network availability,
DOM initialization, event wiring, authorization, or that a particular symbol
or user flow executes. No function-call census or live browser test was run.
The fallback HTML branch and computed imports/URLs are outside this result.
A future zero must be reported per target with its positive-control count
and inspected scope; it cannot establish repository-wide absence.

Important noncallers: selector strings in `cookbook-diagnosis.js`, logger
labels in `cookbookSchedule.js`, comments in `calendar.js` and
`cookbookServe.js`, backend pattern mirrors, test reads, service-worker
precache entries, and documentation are not counted as executing callers.
The original aggregate acceptance grep included such mentions, excluded HTML,
and could pass when just one of three targets had a hit. It is retained only
as historical ticket wording, not as this census's acceptance check.

The `cookbook.js` back-imports from diagnosis/hardware-fit form source cycles.
Preserve the existing plain module specifier convention when changing them;
this census does not establish that a consolidation would preserve behavior.
