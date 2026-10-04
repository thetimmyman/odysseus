#!/usr/bin/env python3
"""Validate the source-bound UI surface register without importing the app."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sys

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "UI_SURFACE_REGISTER.json"
REQUIRED_ANALYSES = {
    "configuration_ia", "workspace_map", "common_shell_coverage", "reflow_source_audit"
}
DISPOSITIONS = {"KEEP", "MERGE", "MODE-OF", "RETIRE", "REDESIGN"}


class RegisterError(ValueError):
    pass


class UnsupportedSourceError(RegisterError):
    pass


class _HTMLCandidates(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.settings = []
        self.nav = []
        self.containers = []
        self.line_for = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id")
        line = self.getpos()[0]
        if ident:
            self.ids.append((ident, tag, line))
            self.line_for.setdefault(ident, line)
            if ident.startswith(("rail-", "tool-", "user-bar-")):
                self.nav.append((ident, tag, line))
            classes = set((attrs.get("class") or "").split())
            if classes.intersection({"modal", "search-overlay", "crew-overlay", "ghost-text-overlay"}) or ident.endswith("-overlay"):
                self.containers.append((ident, tuple(sorted(classes)), line))
        for key in ("data-memory-panel", "data-settings-tab", "data-rhpanel", "data-cfgpanel"):
            if key in attrs:
                self.settings.append((key, attrs[key], line))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_source(root: Path, relative: str, overrides: dict[str, bytes] | None) -> bytes:
    if overrides and relative in overrides:
        return overrides[relative]
    path = root / relative
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegisterError(f"missing pinned source: {relative}") from exc


def _direct_app_routes(app_bytes: bytes) -> tuple[dict[str, tuple[str, int]], dict[str, tuple[str, int]]]:
    try:
        tree = ast.parse(app_bytes.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise UnsupportedSourceError("app.py route syntax unsupported or invalid") from exc
    pages, api = {}, {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                continue
            if not isinstance(deco.func.value, ast.Name) or deco.func.value.id != "app":
                continue
            method = deco.func.attr
            if method not in {"get", "route", "api_route", "post", "put", "delete"}:
                continue
            if not deco.args or not isinstance(deco.args[0], ast.Constant) or not isinstance(deco.args[0].value, str):
                raise UnsupportedSourceError("nonliteral direct app route cannot be inventoried")
            path = deco.args[0].value
            target = api if path.startswith("/api/") or method not in {"get", "route"} else pages
            if path in pages or path in api:
                raise RegisterError(f"duplicate direct app route in source: {path}")
            target[path] = (node.name, deco.lineno)
    return pages, api


def _open_command_aliases(source: bytes) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Parse the narrow literal /open navigation table; reject grammar drift."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedSourceError("slash command source is not UTF-8") from exc
    start = text.find("async function _cmdOpen(")
    end = text.find("async function _cmdToolPanel(", start)
    if start < 0 or end < 0:
        raise UnsupportedSourceError("slash command navigation grammar is missing _cmdOpen boundary")
    body = text[start:end]
    table_match = re.search(r"(?ms)^\s*const targets = \{\n(.*?)^\s*\};", body)
    if not table_match:
        raise UnsupportedSourceError("slash command target table grammar is unsupported")
    table_lines = [line for line in table_match.group(1).splitlines() if line.strip()]
    targets: dict[str, set[str]] = {}
    for line in table_lines:
        match = re.fullmatch(r"\s*([a-zA-Z0-9_-]+): \[(.*?)\],?\s*", line)
        if not match:
            raise UnsupportedSourceError("slash command target table has unsupported syntax")
        values = re.findall(r"'([^']*)'", match.group(2))
        if not values or "" in values:
            raise UnsupportedSourceError("slash command target table has empty or dynamic target")
        targets[match.group(1)] = set(values)
    direct = set(re.findall(r"target === '([^']+)'", body))
    if not direct:
        raise UnsupportedSourceError("slash command direct target branches are missing")
    command_match = re.search(r"(?ms)^  open: \{(.*?)^  cookbook: \{", text)
    if not command_match:
        raise UnsupportedSourceError("slash command /open registry grammar is unsupported")
    alias_match = re.search(r"(?m)^\s{4}alias: \[(.*?)\],?\s*$", command_match.group(1))
    if not alias_match:
        raise UnsupportedSourceError("slash command /open alias list is unsupported")
    top_aliases = set(re.findall(r"'([^']+)'", alias_match.group(1))) | {"open"}
    return targets, direct, top_aliases


def _static_mounts(app_bytes: bytes) -> list[tuple[str, str, int]]:
    try:
        tree = ast.parse(app_bytes.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise UnsupportedSourceError("app.py mount syntax unsupported or invalid") from exc
    rows = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"
                and node.func.attr == "mount"):
            if (len(node.args) < 2 or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)):
                raise UnsupportedSourceError("nonliteral static mount cannot be inventoried")
            handler = ast.unparse(node.args[1])
            rows.append((node.args[0].value, handler, node.lineno))
    return rows


def _secondary_html_handlers(root: Path, overrides=None):
    found = {}
    for path in sorted((root / "routes").glob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(_load_source(root, relative, overrides).decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise UnsupportedSourceError(f"route source grammar unsupported: {relative}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = None
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id in {"router", "app"}
                        and decorator.func.attr in {"get", "post", "route", "api_route"}):
                    if (not decorator.args or not isinstance(decorator.args[0], ast.Constant)
                            or not isinstance(decorator.args[0].value, str)):
                        raise UnsupportedSourceError(f"nonliteral mounted route cannot be inventoried: {relative}")
                    route = decorator.args[0].value
                    break
            if route is None:
                continue
            uses_html = any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                            and child.func.id == "HTMLResponse" for child in ast.walk(node))
            if uses_html:
                if route in found:
                    raise RegisterError(f"duplicate HTML route in mounted source: {route}")
                found[route] = (node.name, relative)
    return found


def _asset_response_handlers(root: Path, overrides=None):
    found = {}
    for path in sorted((root / "routes").glob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(_load_source(root, relative, overrides).decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise UnsupportedSourceError(f"route source grammar unsupported: {relative}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = None
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id in {"router", "app"}
                        and decorator.func.attr in {"get", "post", "route", "api_route"}):
                    if (not decorator.args or not isinstance(decorator.args[0], ast.Constant)
                            or not isinstance(decorator.args[0].value, str)):
                        raise UnsupportedSourceError(f"nonliteral mounted route cannot be inventoried: {relative}")
                    route = decorator.args[0].value
                    break
            if route is None:
                continue
            returns_file = any(isinstance(child, ast.Call)
                               and ((isinstance(child.func, ast.Name) and child.func.id == "FileResponse")
                                    or (isinstance(child.func, ast.Attribute) and child.func.attr == "FileResponse"))
                               for child in ast.walk(node))
            if returns_file:
                if route in found:
                    raise RegisterError(f"duplicate file-response route: {route}")
                found[route] = (node.name, relative)
    return found


def _generated_dom_id_candidates(source: bytes, path: str) -> set[str]:
    """Extract the admitted narrow grammar: literal/dynamic id= attributes in module markup."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedSourceError(f"generated DOM module is not UTF-8: {path}") from exc
    return set(re.findall(r"\bid=[\"']([^\"']+)[\"']", text))


def _candidate_html(root: Path, register: dict, overrides: dict[str, bytes] | None):
    data = _load_source(root, "static/index.html", overrides)
    parser = _HTMLCandidates()
    try:
        parser.feed(data.decode("utf-8"))
        parser.close()
    except (UnicodeDecodeError, Exception) as exc:
        raise UnsupportedSourceError("index.html candidate grammar is unsupported") from exc
    return parser


def _validate_refs(obj, pins: dict[str, str], root: Path, overrides=None):
    refs = []
    if isinstance(obj, dict):
        if "path" in obj and "line" in obj and "sha256" in obj:
            refs.append(obj)
        for value in obj.values():
            refs.extend(_validate_refs(value, pins, root, overrides))
    elif isinstance(obj, list):
        for value in obj:
            refs.extend(_validate_refs(value, pins, root, overrides))
    for ref in refs:
        path, line, digest = ref.get("path"), ref.get("line"), ref.get("sha256")
        if path not in pins or digest != pins[path]:
            raise RegisterError(f"source reference is not in pinned inventory: {path}")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise RegisterError(f"invalid source line reference: {path}")
        source = _load_source(root, path, overrides).decode("utf-8")
        if line > max(1, len(source.splitlines())):
            raise RegisterError(f"source line exceeds file length: {path}:{line}")
    return refs


def validate_register(register: dict, root: Path, source_overrides=None) -> list[str]:
    """Validate register and coverage against explicit source root; return facts."""
    if register.get("schema") != "ui-surface-register-v1":
        raise RegisterError("unsupported register schema")
    snapshot = register.get("source_snapshot")
    pins = snapshot.get("source_pins") if isinstance(snapshot, dict) else None
    if not isinstance(pins, dict) or not pins or snapshot.get("source_file_count") != len(pins):
        raise RegisterError("source pin inventory missing or inconsistent")
    excluded_pins = {}
    for item in snapshot.get("scope_exclusions", []):
        path, digest = item.get("path"), item.get("sha256")
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or not isinstance(digest, str) or path in pins or path in excluded_pins
                or not item.get("reason")):
            raise RegisterError("invalid source-bound scope exclusion")
        excluded_pins[path] = digest
    for relative, expected in {**pins, **excluded_pins}.items():
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RegisterError("unsafe source path in pin inventory")
        if _sha(_load_source(root, relative, source_overrides)) != expected:
            raise RegisterError(f"source pin mismatch; register requires source review: {relative}")
    discovered = set()
    for pattern in ("app.py", "routes/*.py", "static/*.html", "static/app.js", "static/style.css", "static/js/**/*.js"):
        discovered.update(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())
    if source_overrides:
        discovered.update(path for path in source_overrides
                          if path == "app.py" or path.startswith("routes/") and path.endswith(".py")
                          or path.startswith("static/") and (path.endswith(".html") or path == "static/app.js"
                                                               or path == "static/style.css"
                                                               or path.startswith("static/js/") and path.endswith(".js")))
    pinned_relevant = {p for p in pins if p == "app.py" or p.startswith("routes/") and p.endswith(".py") or p.startswith("static/") and p.endswith((".html", ".js", ".css"))}
    if discovered != pinned_relevant:
        added, missing = sorted(discovered-pinned_relevant), sorted(pinned_relevant-discovered)
        raise RegisterError(f"relevant source inventory changed; added={added[:4]} missing={missing[:4]}")

    surfaces = register.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise RegisterError("surface list is empty")
    ids = [s.get("id") for s in surfaces if isinstance(s, dict)]
    if len(ids) != len(surfaces) or len(ids) != len(set(ids)) or any(not isinstance(x, str) or not x for x in ids):
        raise RegisterError("surface IDs must be unique nonempty strings")
    surface_ids = set(ids)
    required = {"name", "purpose", "primary_tasks", "frontend_modules", "entry_urls", "disposition", "rationale", "source_evidence", "state_scope", "state_evidence", "backend_dependencies", "availability", "shell_behavior", "responsive_behavior", "operator_friction", "proposed_destination"}
    for s in surfaces:
        if required-set(s):
            raise RegisterError(f"surface {s.get('id')} lacks required fields: {sorted(required-set(s))}")
        if any(not s.get(k) for k in ("name", "purpose", "primary_tasks", "rationale", "source_evidence", "state_scope", "state_evidence")):
            raise RegisterError(f"surface {s['id']} has empty evidence or rationale")
        if s["disposition"] not in DISPOSITIONS:
            raise RegisterError(f"unsupported disposition for {s['id']}")
        if s["disposition"] != "KEEP" and not s.get("proposed_destination"):
            raise RegisterError(f"non-KEEP disposition requires destination: {s['id']}")
        for field in ("observed_desktop", "observed_mobile"):
            observed = s["availability"].get(field)
            if observed != "UNKNOWN" and not s["availability"].get(field + "_evidence"):
                raise RegisterError(f"measured availability claim lacks observation evidence: {s['id']}")
        if s["operator_friction"].get("status") != "UNKNOWN" and not s["operator_friction"].get("evidence"):
            raise RegisterError(f"measured friction claim lacks observation evidence: {s['id']}")
        if s["availability"].get("observed_mobile") == "UNKNOWN" and not s["availability"].get("reason"):
            raise RegisterError(f"UNKNOWN mobile state requires reason: {s['id']}")
        shell = s["shell_behavior"]
        responsive = s["responsive_behavior"]
        if (not shell.get("navigation_evidence") or not shell.get("frontend_owner_modules")
                or not shell.get("state_owner_summary") or not shell.get("evidence")):
            raise RegisterError(f"surface lacks source-specific shell/navigation/state ownership evidence: {s['id']}")
        if set(shell["frontend_owner_modules"]) != set(s["frontend_modules"]):
            raise RegisterError(f"surface shell owner map differs from owning frontend modules: {s['id']}")
        if (responsive.get("observed_mobile") != "UNKNOWN"
                or not responsive.get("responsive_source_evidence")
                or not responsive.get("surface_specific_media_review")):
            raise RegisterError(f"surface lacks responsive source evidence or honest observation status: {s['id']}")
        for path in s["frontend_modules"]:
            if path not in pins:
                raise RegisterError(f"un pinned frontend module: {path}")

    coverage = register.get("entry_point_coverage", {})
    for key in ("routes", "static_html", "navigation_candidates", "root_container_candidates", "settings_tabs", "generated_dom_modules", "exclusions"):
        if not isinstance(coverage.get(key), list):
            raise RegisterError(f"coverage category missing: {key}")
    for name in REQUIRED_ANALYSES:
        analysis = register.get("analyses", {}).get(name)
        if not isinstance(analysis, dict) or not analysis:
            raise RegisterError(f"required analysis missing: {name}")
        _validate_refs(analysis, pins, root, source_overrides)

    # Every sealed source page route must map once to a known surface.
    inventory = snapshot.get("candidate_inventory")
    if not isinstance(inventory, dict):
        raise RegisterError("sealed census candidate inventory is missing")
    app_bytes = _load_source(root, "app.py", source_overrides)
    actual_routes, actual_api_routes = _direct_app_routes(app_bytes)
    census_routes = {item.get("path"): item.get("function") for item in inventory.get("direct_app_page_routes", [])}
    if census_routes != {path: data[0] for path, data in actual_routes.items()}:
        raise RegisterError("page route source no longer matches the sealed census")
    api_rows = coverage.get("direct_api_exclusions", [])
    api_keys = [row.get("path") for row in api_rows]
    if len(api_keys) != len(set(api_keys)) or set(api_keys) != set(actual_api_routes):
        raise RegisterError("direct app API route exclusion coverage is incomplete")
    for row in api_rows:
        if row.get("handler") != actual_api_routes[row["path"]][0] or not row.get("reason"):
            raise RegisterError(f"direct API exclusion lacks evidence: {row.get('path')}")
    mounts = _static_mounts(app_bytes)
    expected_mount = coverage.get("static_mount")
    if len(mounts) != 1 or not expected_mount or (expected_mount.get("path"), expected_mount.get("handler")) != mounts[0][:2]:
        raise RegisterError("static file mount coverage is missing or stale")
    route_rows = coverage["routes"]
    route_keys = [row.get("path") for row in route_rows]
    if len(route_keys) != len(set(route_keys)) or set(route_keys) != set(actual_routes):
        raise RegisterError("page route coverage is missing, duplicated, or stale")
    for row in route_rows:
        if row.get("surface_id") not in surface_ids or row.get("handler") != actual_routes[row["path"]][0]:
            raise RegisterError(f"route lacks valid owner mapping: {row.get('path')}")
    static_paths = sorted(row.get("path") for row in coverage["static_html"])
    if static_paths != sorted(inventory.get("static_html_paths", [])):
        raise RegisterError("static HTML direct-entry coverage differs from the sealed census")
    if any(row.get("surface_id") not in surface_ids or not row.get("url") or not row.get("url_aliases")
           or row.get("url") not in row.get("url_aliases", []) for row in coverage["static_html"]):
        raise RegisterError("static HTML entry lacks surface or URL mapping")
    expected_static = {"static/index.html": {"/", "/static/index.html"},
                       "static/login.html": {"/login", "/static/login.html"}}
    if {row.get("path"): set(row.get("url_aliases", [])) for row in coverage["static_html"]} != expected_static:
        raise RegisterError("static HTML direct URL aliases are incomplete")

    parser = _candidate_html(root, register, source_overrides)
    nav_rows = coverage["navigation_candidates"]
    nav_ids = [row.get("candidate_id") for row in nav_rows]
    actual_nav = [(ident, tag, line) for ident, tag, line in parser.nav]
    census_nav = [(row.get("id"), row.get("tag"), row.get("line")) for row in inventory.get("index_navigation_candidates", [])]
    mapped_nav = [(row.get("candidate_id"), row.get("tag"), row.get("source", {}).get("line")) for row in nav_rows]
    if Counter(actual_nav) != Counter(census_nav) or Counter(mapped_nav) != Counter(actual_nav):
        raise RegisterError("navigation candidate coverage is incomplete or conflicting")
    for row in nav_rows:
        if row.get("surface_id") not in surface_ids or not row.get("reason"):
            raise RegisterError(f"navigation candidate has no explicit owner/reason: {row.get('candidate_id')}")
    supplementary = coverage.get("secondary_navigation_aliases", [])
    known_ids = {ident: (tag, line) for ident, tag, line in parser.ids}
    supplementary_ids = [row.get("candidate_id") for row in supplementary]
    expected_supplementary = inventory.get("secondary_navigation_aliases", [])
    if (len(supplementary_ids) != len(set(supplementary_ids)) or set(supplementary_ids) & set(nav_ids)
            or Counter((row.get("candidate_id"), row.get("tag"), row.get("source", {}).get("line")) for row in supplementary)
            != Counter((row.get("candidate_id"), row.get("tag"), row.get("source", {}).get("line")) for row in expected_supplementary)):
        raise RegisterError("supplementary navigation aliases are duplicated, missing, or conflicting")
    for row in supplementary:
        ident = row.get("candidate_id")
        if (ident not in known_ids or row.get("surface_id") not in surface_ids or not row.get("reason")
                or (row.get("tag"), row.get("source", {}).get("line")) != known_ids[ident]):
            raise RegisterError(f"supplementary navigation alias lacks source mapping: {ident}")
    command = coverage.get("command_navigation_aliases", {})
    command_source = _load_source(root, "static/js/slashCommands.js", source_overrides)
    actual_targets, direct_targets, actual_top_aliases = _open_command_aliases(command_source)
    target_rows = command.get("target_aliases", [])
    mapped_tokens = [row.get("token") for row in target_rows]
    if (set(mapped_tokens) != set(actual_targets) | direct_targets or len(mapped_tokens) != len(set(mapped_tokens))
            or command.get("command") != "/open"
            or set(command.get("command_aliases", [])) != {"/" + alias for alias in actual_top_aliases}):
        raise RegisterError("slash-command navigation aliases are incomplete or conflicting")
    surface_by_element = {row["candidate_id"]: row["surface_id"] for row in nav_rows + supplementary}
    direct_owner = {"cookbook": "cookbook", "cook": "cookbook",
                    "settings": "settings-admin", "setting": "settings-admin", "config": "settings-admin"}
    for row in target_rows:
        token = row.get("token")
        if token in direct_targets:
            expected_surface = direct_owner.get(token)
        else:
            owners = {surface_by_element.get(ident) for ident in actual_targets.get(token, set())}
            expected_surface = next(iter(owners)) if len(owners) == 1 and None not in owners else None
        if row.get("surface_id") not in surface_ids or row.get("surface_id") != expected_surface:
            raise RegisterError(f"slash-command target maps to the wrong surface: {token}")
    keyboard = coverage.get("keyboard_navigation_aliases", [])
    expected_keyboard = inventory.get("keyboard_navigation_aliases", [])
    actual_keyboard = [{"key": row.get("key"), "surface_id": row.get("surface_id")} for row in keyboard]
    if Counter((row["key"], row["surface_id"]) for row in actual_keyboard) != Counter((row["key"], row["surface_id"]) for row in expected_keyboard):
        raise RegisterError("keyboard navigation alias mapping is incomplete or conflicting")
    for row in keyboard:
        if row.get("surface_id") not in surface_ids or not row.get("key") or not row.get("reason"):
            raise RegisterError("keyboard navigation alias lacks an owner or evidence")

    container_rows = coverage["root_container_candidates"]
    actual_containers = [(ident, classes, line) for ident, classes, line in parser.containers]
    census_containers = [(row.get("id"), tuple(sorted(row.get("class", []))), row.get("line")) for row in inventory.get("index_surface_container_candidates", [])]
    mapped_containers = [(row.get("candidate_id"), tuple(sorted(row.get("classes", []))), row.get("source", {}).get("line")) for row in container_rows]
    if Counter(actual_containers) != Counter(census_containers) or Counter(mapped_containers) != Counter(actual_containers):
        raise RegisterError("root container coverage is incomplete or conflicting")
    for row in container_rows:
        if row.get("surface_id") not in surface_ids or not row.get("reason"):
            raise RegisterError(f"container has no owner/reason: {row.get('candidate_id')}")

    settings_rows = coverage["settings_tabs"]
    actual_settings = [(kind, value, line) for kind, value, line in parser.settings]
    census_settings = [(row.get("kind"), row.get("value"), row.get("line")) for row in inventory.get("settings_and_config_tabs", [])]
    mapped_settings = [(row.get("kind"), row.get("value"), row.get("source", {}).get("line")) for row in settings_rows]
    if Counter(actual_settings) != Counter(census_settings) or Counter(mapped_settings) != Counter(actual_settings):
        raise RegisterError("settings/config tab coverage is incomplete or conflicting")
    if any(row.get("surface_id") not in surface_ids for row in settings_rows):
        raise RegisterError("settings tab has no mapped surface")

    modules = coverage["generated_dom_modules"]
    owners_by_surface = {surface["id"]: set(surface["frontend_modules"]) for surface in surfaces}
    module_keys = [m.get("module") for m in modules]
    if len(module_keys) != len(set(module_keys)):
        raise RegisterError("duplicate generated DOM module mapping")
    expected_ids = inventory.get("generated_literal_dom_ids_by_js_module", {})
    expected_modules = set(expected_ids)
    if set(module_keys) != expected_modules:
        raise RegisterError("generated DOM module set changed or is unsupported")
    for mod in modules:
        path = mod.get("module")
        if path not in pins or mod.get("surface_id") not in surface_ids:
            raise RegisterError(f"generated DOM module has no pinned owner: {path}")
        if path not in owners_by_surface[mod["surface_id"]]:
            raise RegisterError(f"generated DOM module is mapped to a surface that does not list it as an owner: {path}")
        source = _load_source(root, path, source_overrides).decode("utf-8")
        candidates = mod.get("candidates")
        values = [item.get("candidate_id") for item in candidates or []]
        source_ids = _generated_dom_id_candidates(source.encode("utf-8"), path)
        if source_ids != set(expected_ids.get(path, [])):
            raise RegisterError(f"sealed generated DOM census differs from literal source id attributes: {path}")
        if len(values) != len(set(values)) or not values or set(values) != source_ids:
            raise RegisterError(f"generated candidate list malformed or incomplete: {path}")
        for item in candidates:
            if not isinstance(item.get("candidate_id"), str) or item["candidate_id"] not in source:
                raise RegisterError(f"generated DOM candidate is not source-backed: {path}")
            if item.get("resolution") not in {"grouped_control", "separate_surface", "explicit_exclusion"} or not item.get("reason"):
                raise RegisterError(f"generated DOM candidate lacks explicit resolution: {path}")
    actual_secondary_html = _secondary_html_handlers(root, source_overrides)
    secondary_rows = coverage.get("secondary_html_handlers", [])
    secondary_paths = [row.get("path") for row in secondary_rows]
    if len(secondary_paths) != len(set(secondary_paths)) or set(secondary_paths) != set(actual_secondary_html):
        raise RegisterError("mounted route HTML response coverage is incomplete or conflicting")
    for row in secondary_rows:
        if row.get("surface_id") not in surface_ids or row.get("handler") != actual_secondary_html[row["path"]][0]:
            raise RegisterError(f"HTML route lacks a valid surface mapping: {row.get('path')}")
    actual_assets = _asset_response_handlers(root, source_overrides)
    asset_rows = coverage.get("asset_response_exclusions", [])
    asset_paths = [row.get("path") for row in asset_rows]
    if len(asset_paths) != len(set(asset_paths)) or set(asset_paths) != set(actual_assets):
        raise RegisterError("mounted file-response exclusion coverage is incomplete or conflicting")
    for row in asset_rows:
        if row.get("handler") != actual_assets[row["path"]][0] or row.get("surface_id") not in surface_ids or not row.get("reason"):
            raise RegisterError(f"file-response exclusion lacks evidence: {row.get('path')}")

    for exclusion in coverage["exclusions"]:
        if not all(exclusion.get(k) for k in ("candidate", "type", "reason", "source")):
            raise RegisterError("source exclusion requires candidate, type, reason, and source evidence")
        if exclusion.get("source", {}).get("path") not in pins:
            raise RegisterError("source exclusion refers to unpinned source")

    all_objects = register
    _validate_refs(all_objects, {**pins, **excluded_pins}, root, source_overrides)
    for item in snapshot.get("scope_exclusions", []):
        source_ref = item.get("source", {})
        if source_ref.get("path") != item.get("path") or source_ref.get("sha256") != item.get("sha256"):
            raise RegisterError("scope exclusion source citation does not match its pin")
    return [f"source_pins={len(pins)}", f"routes={len(route_rows)}", f"direct_api_exclusions={len(api_rows)}", f"static_html={len(coverage['static_html'])}", f"static_url_aliases={sum(len(row['url_aliases']) for row in coverage['static_html'])}", f"mounted_html_handlers={len(secondary_rows)}", f"asset_responses={len(asset_rows)}", f"navigation={len(nav_rows)}", f"supplementary_navigation_aliases={len(supplementary)}", f"slash_targets={len(target_rows)}", f"containers={len(container_rows)}", f"settings_tabs={len(settings_rows)}", f"generated_modules={len(modules)}", f"generated_candidates={sum(len(m['candidates']) for m in modules)}"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--register", type=Path, default=REGISTER)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.register.read_text(encoding="utf-8"))
        facts = validate_register(data, args.root.resolve())
    except UnsupportedSourceError as exc:
        print(f"UI surface register unsupported source grammar: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, RegisterError) as exc:
        print(f"UI surface register refused: {exc}", file=sys.stderr)
        return 1
    print("UI surface register valid: " + ", ".join(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
