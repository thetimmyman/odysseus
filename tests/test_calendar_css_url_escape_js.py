r"""CSS-injection guard for calendar background-image URL escaping.

`bg:` values are CalDAV-syncable, so untrusted. `_cssUrlEscape` (calendar/utils.js)
must double backslashes BEFORE escaping quotes, or a trailing backslash consumes
the closing quote of `url('...')` and breaks out of the CSS string.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_UTILS = (_REPO / "static" / "js" / "calendar" / "utils.js").as_posix()
_CALENDAR_JS = _REPO / "static" / "js" / "calendar.js"
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _run(js: str) -> str:
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_cssurlescape_doubles_backslashes_before_quotes():
    js = textwrap.dedent(
        f"""
        const {{ _cssUrlEscape }} = await import('{_UTILS}');
        console.log(JSON.stringify({{
          backslash: _cssUrlEscape('a\\\\b'),
          trailing:  _cssUrlEscape('img\\\\'),
          quote:     _cssUrlEscape("a'b"),
          dquote:    _cssUrlEscape('a"b'),
        }}));
        """
    )
    out = json.loads(_run(js))
    # one backslash -> two; the escape for "'" is not itself re-escaped
    assert out["backslash"] == r"a\\b"
    assert out["trailing"] == "img\\\\"   # 'img\' -> 'img\\'
    assert out["quote"] == r"a\'b"
    assert out["dquote"] == "a%22b"


def test_backslash_breakout_payload_cannot_close_the_url_string():
    # Without the backslash-first escape, "x\" would render url('x\') and the
    # trailing backslash escapes the closing quote -> breakout. After the fix the
    # backslash is doubled, so the quote we add still terminates the string.
    js = textwrap.dedent(
        f"""
        const {{ _cssUrlEscape, _calBgCss }} = await import('{_UTILS}');
        const payload = 'x\\\\';                       // a string ending in one backslash
        console.log(JSON.stringify({{
          esc: _cssUrlEscape(payload),
          css: _calBgCss('bg:' + payload, 'var(--accent)'),
        }}));
        """
    )
    out = json.loads(_run(js))
    assert out["esc"] == "x\\\\"                       # doubled backslash
    # The rendered declaration keeps the backslash doubled inside url('...').
    assert "url('x\\\\')" in out["css"]


def test_calbgcss_escapes_quote_breakout():
    js = textwrap.dedent(
        f"""
        const {{ _calBgCss }} = await import('{_UTILS}');
        console.log(JSON.stringify(_calBgCss("bg:a'); X{{}}//", 'var(--accent)')));
        """
    )
    css = json.loads(_run(js))
    # the injected single quote is escaped, so the url() string is not closed early
    assert r"\'" in css
    assert "url('a\\'); X{}//')" in css


def test_every_calendar_url_interpolation_is_escaped():
    # Every CSS `url('${...}')` in calendar.js must route its untrusted value
    # through `_cssUrlEscape`; catches newly added sinks that bypass it.
    src = _CALENDAR_JS.read_text(encoding="utf-8")
    interps = re.findall(r"url\('\$\{([^}]*)\}'\)", src)
    assert interps, "expected at least one url('${...}') interpolation in calendar.js"
    unescaped = [expr for expr in interps if "_cssUrlEscape(" not in expr]
    assert not unescaped, (
        "bg-image url() interpolation(s) not routed through _cssUrlEscape: "
        + ", ".join(repr(e) for e in unescaped)
    )
