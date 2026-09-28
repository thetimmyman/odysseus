"""Extract a unified diff from model output and validate its shape.

Pure text validation: never touches the filesystem, git, or applies anything."""
import re
from typing import List, Optional

from src.routing_context import looks_like_secret, safe_repo_path

MAX_CHANGED_FILES = 8
MAX_CHANGED_LINES = 600

# Paths a patch must never touch; a reasonable default, not exhaustive.
_FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)(venv|\.venv)(/|$)"),
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"(^|/)(dist|build)(/|$)"),
    re.compile(r"(^|/)\.next(/|$)"),
]

# Match every fence regardless of tag, then filter by content: anchoring on a
# "diff" tag lets an earlier fence pair with the diff's closing delimiter.
_FENCE_RE = re.compile(r"```[^\n]*\r?\n(.*?)```", re.DOTALL)
_DIFF_GIT_RE = re.compile(r"^diff --git ", re.MULTILINE)
_HEADER_RE = re.compile(r"^--- (\S+)\r?\n\+\+\+ (\S+)", re.MULTILINE)


def _looks_diff_shaped(text: str) -> bool:
    return bool(_DIFF_GIT_RE.search(text) or _HEADER_RE.search(text))


def _trim_to_diff_end(text: str) -> str:
    """Trim an unfenced diff at the first non-diff line, dropping trailing prose."""
    lines = text.splitlines()
    end = 0
    for line in lines:
        if line == "" or line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@", "+", "-", " ", "\\")):
            end += 1
        else:
            break
    return "\n".join(lines[:end]).rstrip()


def extract_diff(response_text: str) -> Optional[str]:
    """Prefer a fenced diff, else a bare one; None if nothing diff-shaped is found."""
    if not response_text:
        return None
    for block in _FENCE_RE.findall(response_text):
        if _looks_diff_shaped(block):
            return block.strip()
    match = _DIFF_GIT_RE.search(response_text) or _HEADER_RE.search(response_text)
    if match:
        trimmed = _trim_to_diff_end(response_text[match.start():])
        return trimmed or None
    return None


def _file_paths_from_header(dash_path: str, plus_path: str) -> List[str]:
    """Distinct repo-relative paths from raw ---/+++ targets, skipping /dev/null."""
    paths = []
    for raw in (dash_path, plus_path):
        if raw == "/dev/null":
            continue
        stripped = re.sub(r"^[ab]/", "", raw)
        paths.append(stripped)
    seen = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def parse_patch_shape(diff_text: str) -> dict:
    """Count changed files and lines; shape only, not whether the diff applies."""
    changed_files: List[str] = []
    changed_lines = 0

    lines = diff_text.splitlines()
    i = 0
    current_paths: List[str] = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            dash = line[4:].split("\t")[0].strip()
            plus = lines[i + 1][4:].split("\t")[0].strip()
            current_paths = _file_paths_from_header(dash, plus)
            for p in current_paths:
                if p not in changed_files:
                    changed_files.append(p)
            i += 2
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed_lines += 1
        elif line.startswith("-") and not line.startswith("---"):
            changed_lines += 1
        i += 1

    return {
        "changed_files": changed_files,
        "file_count": len(changed_files),
        "changed_lines": changed_lines,
    }


def validate_patch_shape(diff_text: Optional[str], repo_path: str) -> dict:
    """`allowed=False` with `reasons` when oversized, touching a forbidden path, or absent."""
    if not diff_text:
        return {
            "extracted": False, "allowed": False,
            "reasons": ["no unified diff found in model response"],
            "file_count": 0, "changed_lines": 0, "changed_files": [],
        }

    shape = parse_patch_shape(diff_text)
    reasons: List[str] = []

    if shape["file_count"] == 0:
        reasons.append("diff-shaped text found but no valid --- /+++ file headers parsed")
    if shape["file_count"] > MAX_CHANGED_FILES:
        reasons.append(f"changes {shape['file_count']} files, exceeds max {MAX_CHANGED_FILES} without approval")
    if shape["changed_lines"] > MAX_CHANGED_LINES:
        reasons.append(f"changes {shape['changed_lines']} lines, exceeds max {MAX_CHANGED_LINES} without approval")

    for rel_path in shape["changed_files"]:
        if safe_repo_path(repo_path, rel_path) is None:
            reasons.append(f"path {rel_path!r} resolves outside repo_path (absolute path or traversal)")
        elif looks_like_secret(rel_path):
            reasons.append(f"path {rel_path!r} matches a secret-file pattern")
        elif any(p.search(rel_path) for p in _FORBIDDEN_PATH_PATTERNS):
            reasons.append(f"path {rel_path!r} matches a forbidden/generated-path pattern")

    return {
        "extracted": True,
        "allowed": len(reasons) == 0,
        "reasons": reasons,
        "file_count": shape["file_count"],
        "changed_lines": shape["changed_lines"],
        "changed_files": shape["changed_files"],
    }
