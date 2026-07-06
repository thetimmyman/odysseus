"""src/routing_context.py — builds a bounded ContextBundle (spec Section 9)
from a RoutingTask's explicit inputs. v1 scope: explicit files/logs/diffs
only, no smart stack-trace-following auto-inclusion -- that's a documented
future enhancement, not implemented here."""
import json
import os
import subprocess
from typing import Optional


def estimate_tokens(text: str) -> int:
    """Approximate token estimator (~4 chars/token) -- good enough for
    routing/budget decisions, not billing-accurate."""
    return max(1, len(text) // 4) if text else 0


def _resolve_log_entry(repo_path: str, entry: str):
    """`entry` is either a path relative to repo_path, or literal log
    content already. Returns (content, path_or_None)."""
    candidate = os.path.join(repo_path, entry)
    if os.path.isfile(candidate):
        try:
            with open(candidate, "r", errors="replace") as f:
                return f.read(), entry
        except OSError as e:
            return f"<<could not read {entry}: {e}>>", entry
    return entry, None


def _resolve_diff_entry(repo_path: str, entry: str) -> str:
    """`entry` is either a git range (e.g. "main...HEAD") to diff, or literal
    diff text already. Heuristic: if it contains ".." or looks like a bare
    ref, try `git diff`; fall back to treating it as literal text."""
    looks_like_range = ".." in entry or (" " not in entry and "\n" not in entry and len(entry) < 200)
    if looks_like_range:
        try:
            out = subprocess.run(
                ["git", "-C", repo_path, "diff", entry],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except Exception:
            pass
    return entry


def _git(repo_path: str, *args) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _build_metadata(repo_path: str, test_commands: list) -> dict:
    package_manager = None
    if os.path.exists(os.path.join(repo_path, "package-lock.json")):
        package_manager = "npm"
    elif os.path.exists(os.path.join(repo_path, "pnpm-lock.yaml")):
        package_manager = "pnpm"
    elif os.path.exists(os.path.join(repo_path, "yarn.lock")):
        package_manager = "yarn"
    return {
        "repo_name": os.path.basename(os.path.normpath(repo_path)),
        "branch": _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit_sha": _git(repo_path, "rev-parse", "HEAD"),
        "package_manager": package_manager,
        "test_commands": test_commands or [],
    }


def build_context_bundle(task) -> dict:
    """`task` is a core.database.RoutingTask row. Returns a dict matching the
    spec's ContextBundle shape: {task_id, files, logs, metadata}."""
    inputs = json.loads(task.inputs) if task.inputs else {}
    files_list = inputs.get("files") or []
    logs_list = inputs.get("logs") or []
    diffs_list = inputs.get("diffs") or []
    test_commands = inputs.get("test_commands") or []

    files = []
    seen_paths = set()
    for rel_path in files_list:
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)
        full_path = os.path.join(task.repo_path, rel_path)
        try:
            with open(full_path, "r", errors="replace") as f:
                content = f.read()
        except OSError as e:
            content = f"<<could not read {rel_path}: {e}>>"
        files.append({
            "path": rel_path,
            "content": content,
            "reason": "explicitly listed in task.inputs.files",
            "token_estimate": estimate_tokens(content),
        })

    logs = []
    seen_log_keys = set()
    for entry in logs_list:
        content, path = _resolve_log_entry(task.repo_path, entry)
        key = path or content
        if key in seen_log_keys:
            continue
        seen_log_keys.add(key)
        logs.append({"path": path, "content": content, "reason": "explicitly listed in task.inputs.logs"})

    for entry in diffs_list:
        content = _resolve_diff_entry(task.repo_path, entry)
        logs.append({"path": None, "content": content, "reason": f"diff: {entry}"})

    metadata = _build_metadata(task.repo_path, test_commands)
    metadata["token_estimate"] = sum(f["token_estimate"] for f in files) + sum(
        estimate_tokens(l["content"]) for l in logs
    )

    return {
        "task_id": task.id,
        "prompt": inputs.get("prompt"),
        "acceptance_criteria": inputs.get("acceptance_criteria") or [],
        "files": files,
        "logs": logs,
        "metadata": metadata,
    }
