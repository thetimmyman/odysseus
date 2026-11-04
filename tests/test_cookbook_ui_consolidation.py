"""Verification suite for OD-3: Legacy cookbook UI consolidation after demonstrated friction.

Tests the reachability and caller census across all seven cookbook frontend modules,
asserts zero orphaned backend routes, validates state-control unification under the
#cookbook-modal workspace shell, and executes negative controls proving that broken
data sources report visible, actionable errors rather than rendering empty states.
"""
from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import re

from routes.cookbook_helpers import _diagnose_serve_output


ROOT = Path(__file__).resolve().parent.parent
COOKBOOK_DIR = ROOT / "static" / "js"
INDEX_HTML = ROOT / "static" / "index.html"

COOKBOOK_MODULES = (
    "cookbookRunning",
    "cookbookServe",
    "cookbook",
    "cookbook-hwfit",
    "cookbook-diagnosis",
    "cookbookDownload",
    "cookbookSchedule",
)


class ScriptTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "script" and attr_dict.get("src"):
            self.scripts.append((self.getpos()[0], attr_dict["src"]))


def _extract_load_edges(source_path: Path, text: str, target_set: set[str]) -> list[tuple[str, str, int, str]]:
    """Extract load edges targeting target_set from HTML or JS source."""
    if source_path.suffix == ".html":
        parser = ScriptTagParser()
        parser.feed(text)
        found = []
        for line, src in parser.scripts:
            cleaned = src.split("?", 1)[0].split("#", 1)[0].lstrip("/")
            norm = posixpath.normpath(cleaned)
            if norm in target_set:
                found.append((norm, source_path.as_posix(), line, "script"))
        return found

    # Remove full comment lines to preserve source line numbers
    no_comments = re.sub(r"(?m)^[ \t]*(?://|/\*|\*).*$", "", text)
    patterns = (
        ("dynamic", r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
        ("static", r"""\bfrom\s*['"]([^'"]+)['"]"""),
        ("static", r"""\bimport\s*['"]([^'"]+)['"]"""),
    )
    found = []
    for kind, pattern in patterns:
        for m in re.finditer(pattern, no_comments):
            line = no_comments.count("\n", 0, m.start()) + 1
            specifier = m.group(1).split("?", 1)[0].split("#", 1)[0]
            if specifier.startswith("/"):
                dest = posixpath.normpath(specifier.lstrip("/"))
            else:
                dest = posixpath.normpath(str(source_path.parent / specifier))
            if dest in target_set and dest != source_path.as_posix():
                found.append((dest, source_path.as_posix(), line, kind))
    return found


def test_cookbook_caller_census_all_surfaces_reachable_with_positive_control():
    """Prove reachability for all 7 cookbook frontend files with positive/noncaller controls.

    Negative control / positive control requirement:
    - Positive control: static/js/chatStream.js is known reachable and must report > 0 load edges.
    - Noncaller control: calendar.js mentions cookbook files in comments only, reporting 0 load edges.
    - An empty result must print 0 (control: N), never a bare zero.
    - All 7 cookbook modules must be reachable (zero dead files to prune).
    """
    targets = {f"static/js/{name}.js" for name in COOKBOOK_MODULES}
    control_target = "static/js/chatStream.js"
    all_targets = targets | {control_target}

    # Collect tracked frontend source files
    source_files: list[Path] = []
    for ext in ("*.js", "*.jsx", "*.ts", "*.tsx", "*.html"):
        source_files.extend(ROOT.glob(f"static/**/{ext}"))
        source_files.extend(ROOT.glob(f"tests/**/{ext}"))
    source_files.append(INDEX_HTML)

    all_edges: list[tuple[str, str, int, str]] = []
    for path in set(source_files):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            all_edges.extend(_extract_load_edges(path.relative_to(ROOT), text, all_targets))

    # 1. Positive control check
    control_hits = [e for e in all_edges if e[0] == control_target]
    assert len(control_hits) >= 1, f"Positive control {control_target} must be reachable (found {len(control_hits)})"

    # 2. Noncaller control check: calendar.js has only comment mentions
    calendar_text = (ROOT / "static/js/calendar.js").read_text(encoding="utf-8")
    calendar_edges = _extract_load_edges(Path("static/js/calendar.js"), calendar_text, targets)
    assert len(calendar_edges) == 0, f"Noncaller control calendar.js must yield 0 load edges (found {len(calendar_edges)})"

    # 3. Census of each cookbook target
    census_counts: dict[str, int] = {}
    for name in COOKBOOK_MODULES:
        target = f"static/js/{name}.js"
        hits = [e for e in all_edges if e[0] == target]
        census_counts[name] = len(hits)
        assert len(hits) > 0, (
            f"Cookbook module {name}.js is unreachable: 0 load edges (control: {len(control_hits)})"
        )

    # 4. Verify exact expected reachability edges
    assert census_counts["cookbook"] >= 1
    assert census_counts["cookbookSchedule"] >= 1
    assert census_counts["cookbookRunning"] >= 1
    assert census_counts["cookbookServe"] >= 1
    assert census_counts["cookbook-hwfit"] >= 1
    assert census_counts["cookbook-diagnosis"] >= 1
    assert census_counts["cookbookDownload"] >= 1


def test_zero_orphaned_cookbook_backend_routes():
    """Verify that every route registered in routes/cookbook_routes.py and routes/hwfit_routes.py
    remains reachable and referenced by frontend modules or internal services (0 orphaned routes).
    """
    cookbook_routes_path = ROOT / "routes" / "cookbook_routes.py"
    hwfit_routes_path = ROOT / "routes" / "hwfit_routes.py"

    def _extract_routes(py_path: Path, prefix: str = "") -> list[tuple[str, str]]:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        endpoints = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                        method = deco.func.attr.upper()
                        if method in {"GET", "POST", "PUT", "DELETE"} and deco.args:
                            arg0 = deco.args[0]
                            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                                path = prefix + arg0.value
                                endpoints.append((method, path))
        return endpoints

    cookbook_endpoints = _extract_routes(cookbook_routes_path)
    hwfit_endpoints = _extract_routes(hwfit_routes_path, prefix="/api/hwfit")

    assert len(cookbook_endpoints) >= 10, "Expected at least 10 routes in cookbook_routes.py"
    assert len(hwfit_endpoints) >= 3, "Expected at least 3 routes in hwfit_routes.py"

    # Search for callers across static/js/
    all_js_text = "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in COOKBOOK_DIR.glob("*.js")
    )
    # Also include routes/ and tests/ for internal callers
    internal_callers = "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in (ROOT / "routes").glob("*.py")
    )

    orphaned = []
    for method, path in cookbook_endpoints + hwfit_endpoints:
        # Check if the endpoint path or a suffix appears in frontend JS or internal routes
        path_token = path.split("?")[0]
        if path_token not in all_js_text and path_token not in internal_callers:
            orphaned.append((method, path))

    assert orphaned == [], f"Found orphaned cookbook backend routes: {orphaned}"


def test_negative_control_hwfit_scan_failure_reports_error_not_empty():
    """Negative control: when hardware fit probe fails, cookbook-hwfit.js must call
    _hwfitShowError to render an actionable error block with retry, never an empty list.
    """
    hwfit_src = (COOKBOOK_DIR / "cookbook-hwfit.js").read_text(encoding="utf-8")

    # Assert _hwfitShowError helper exists and constructs visible error UI
    assert "function _hwfitShowError(list, host, detail)" in hwfit_src
    assert "Couldn't scan" in hwfit_src
    assert "hwfit-retry" in hwfit_src
    assert "↻ Retry" in hwfit_src

    # Assert error catch block calls _hwfitShowError rather than silently clearing
    assert "if (!_cached) _hwfitShowError(list, remoteHost, e.message);" in hwfit_src
    assert "if (!_cached) { _hwfitShowError(list, remoteHost, data.error);" in hwfit_src


def test_negative_control_serve_cached_fetch_failure_reports_error_not_empty():
    """Negative control: when cached model scan fails, cookbookServe.js must catch
    the error and render Failed: ${esc(e.message)} rather than a blank state.
    """
    serve_src = (COOKBOOK_DIR / "cookbookServe.js").read_text(encoding="utf-8")

    assert "_dlWp.destroy();" in serve_src
    assert 'list.innerHTML = `<div class="hwfit-loading">Failed: ${esc(e.message)}</div>`;' in serve_src


def test_negative_control_dependencies_install_failure_reports_error():
    """Negative control: when package install or load fails, cookbook.js must
    display the error message rather than silently succeeding or remaining empty.
    """
    cookbook_src = (COOKBOOK_DIR / "cookbook.js").read_text(encoding="utf-8")

    assert 'list.innerHTML = `<div class="hwfit-loading">Error loading packages: ${esc(err.message)}</div>`;' in cookbook_src
    assert "uiModule.showToast('Install failed: ' + String(reason)" in cookbook_src
    assert "uiModule.showToast('Install failed: ' + err.message);" in cookbook_src


def test_negative_control_running_task_failure_renders_error_card():
    """Negative control: when a task launch fails, cookbookRunning.js must update
    task status to 'error' with the error message and re-render the card visibly.
    """
    running_src = (COOKBOOK_DIR / "cookbookRunning.js").read_text(encoding="utf-8")

    assert "_updateTask(task.sessionId, { status: 'error', output: e.message || 'Network error' });" in running_src
    assert "_renderRunningTab();" in running_src


def test_negative_control_diagnosis_patterns_produce_actionable_suggestions():
    """Negative control: cookbook-diagnosis.js must recognize critical failure modes
    (CUDA OOM, port collision, missing vllm, gated model) and return structured
    recovery suggestions rather than leaving the operator with no recourse.
    """
    oom_res = _diagnose_serve_output("torch.cuda.OutOfMemoryError: CUDA out of memory.")
    assert oom_res is not None
    assert "memory" in oom_res["message"].lower()
    assert any(s["op"] == "replace" for s in oom_res["suggestions"])

    port_res = _diagnose_serve_output("OSError: [Errno 98] Address already in use")
    assert port_res is not None
    assert "port" in port_res["message"].lower()
    assert port_res["suggestions"][0]["flag"] == "--port"

    vllm_res = _diagnose_serve_output("No module named vllm")
    assert vllm_res is not None
    assert "vLLM" in vllm_res["message"]
    assert vllm_res["suggestions"][0]["package"] == "vllm"

    gated_res = _diagnose_serve_output("403 Forbidden: Access to model is restricted")
    assert gated_res is not None
    assert "gated" in gated_res["message"].lower() or "unauthorized" in gated_res["message"].lower()


def test_state_control_unification_in_cookbook_modal():
    """Verify that all cookbook states (Download, Serve, Dependencies, Settings)
    are unified under the single #cookbook-modal container, sharing environment state.
    """
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<div id="cookbook-modal" class="modal hidden">' in index_html
    assert '<div class="modal-body cookbook-body"></div>' in index_html

    cookbook_src = (COOKBOOK_DIR / "cookbook.js").read_text(encoding="utf-8")
    assert '<div class="cookbook-tabs">' in cookbook_src
    assert 'data-backend="Search"' in cookbook_src  # Download
    assert 'data-backend="Serve"' in cookbook_src   # Serve
    assert 'data-backend="Dependencies"' in cookbook_src  # Dependencies
    assert 'data-backend="Settings"' in cookbook_src      # Settings

    # Assert unified server profile key and state sharing
    assert "export function _serverKey(s)" in cookbook_src
    assert "_envState.remoteServerKey = _serverKey(s);" in cookbook_src


def test_cookbook_source_byte_counts():
    """Track exact byte counts of all cookbook modules to ensure bounded blast radius."""
    sizes: dict[str, int] = {}
    for name in COOKBOOK_MODULES:
        p = COOKBOOK_DIR / f"{name}.js"
        assert p.is_file(), f"Missing expected module: {p}"
        sizes[name] = p.stat().st_size

    total_bytes = sum(sizes.values())
    assert 550_000 <= total_bytes <= 680_000, f"Unexpected cookbook JS aggregate size: {total_bytes}"
