"""src/routing_patch.py — Phase 3: extracts a unified diff from a model's raw
completion text and validates its shape (file count, changed-line count,
forbidden/generated paths, traversal) per spec Section 12
(ODYSSEUS_POLICY: maxChangedFilesWithoutApproval=8, maxPatchLinesWithoutApproval=600).

Deliberately pure text validation -- never touches the filesystem, never
shells out to git, never applies anything. Creating a worktree/branch,
applying the patch, running verification commands, and rollback are Phase 4
("Safe Execution"), not built here."""
import re
from typing import List, Optional

from src.routing_context import looks_like_secret, safe_repo_path

MAX_CHANGED_FILES = 8
MAX_CHANGED_LINES = 600

# Paths a patch must never touch, even if otherwise well-formed. Not
# exhaustive -- a deliberate, reasonable default denylist for VCS internals
# and generated/dependency trees, matching the framing already used for
# routing_context.py's _SECRET_PATTERNS.
_FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)(venv|\.venv)(/|$)"),
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"(^|/)(dist|build)(/|$)"),
    re.compile(r"(^|/)\.next(/|$)"),
]

_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\r?\n(.*?)```", re.DOTALL)
_DIFF_GIT_RE = re.compile(r"^diff --git ", re.MULTILINE)
_HEADER_RE = re.compile(r"^--- (\S+)\r?\n\+\+\+ (\S+)", re.MULTILINE)


def _looks_diff_shaped(text: str) -> bool:
    return bool(_DIFF_GIT_RE.search(text) or _HEADER_RE.search(text))


def extract_diff(response_text: str) -> Optional[str]:
    """Look for a fenced ```diff/```patch code block first (how most models
    wrap patch output); fall back to bare unified-diff markers appearing
    directly in the text. Returns None if nothing diff-shaped is found."""
    if not response_text:
        return None
    for block in _FENCE_RE.findall(response_text):
        if _looks_diff_shaped(block):
            return block.strip()
    match = _DIFF_GIT_RE.search(response_text) or _HEADER_RE.search(response_text)
    if match:
        return response_text[match.start():].strip()
    return None


def _file_paths_from_header(dash_path: str, plus_path: str) -> List[str]:
    """`dash_path`/`plus_path` are the raw --- / +++ header targets (e.g.
    "a/src/foo.py", "/dev/null"), still carrying their a/ or b/ prefix.
    Returns the distinct real repo-relative path(s) touched -- one for a
    modify, one for a pure add or delete (the /dev/null side is not a real
    path and is skipped)."""
    paths = []
    for raw in (dash_path, plus_path):
        if raw == "/dev/null":
            continue
        # Strip a conventional single-letter prefix ("a/", "b/") if present;
        # tolerate patches that omit it (bare repo-relative paths).
        stripped = re.sub(r"^[ab]/", "", raw)
        paths.append(stripped)
    # Modify: both sides are the same real path -- one entry. Add/delete:
    # one side was /dev/null -- the other real path is the only entry.
    seen = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def parse_patch_shape(diff_text: str) -> dict:
    """Parse `--- a/x` / `+++ b/x` header pairs and their following hunks
    into changed-file and changed-line counts. Not a full unified-diff
    parser (no hunk-range/context validation) -- shape validation only, per
    spec Section 12's acceptance criterion ("rejects oversized/unsafe
    diffs"), not a correctness check of whether the diff would even apply."""
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
    """Returns {"extracted", "allowed", "reasons", "file_count",
    "changed_lines", "changed_files"}. `allowed=False` with `reasons`
    explaining why whenever the patch is oversized, touches a
    forbidden/secret/out-of-repo path, or no diff was found at all."""
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
