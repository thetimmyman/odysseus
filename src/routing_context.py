"""Build a bounded ContextBundle from a RoutingTask's explicit files/logs/diffs.

Every item carries a ContextSource record; untrusted inline text is fenced and
capped; redaction runs here, before any prompt, so no model sees a raw secret."""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from src.routing_redaction import redact_text

UNTRUSTED_FENCE_START = '<<<UNTRUSTED_START source="{source}">>>'
UNTRUSTED_FENCE_END = "<<<UNTRUSTED_END>>>"

# In-repo files never read into a prompt. A reasonable default, not a security boundary.
_SECRET_PATTERNS = [
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"(^|/)\.env\.local"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.|$)"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)credentials(\.json)?$"),
    re.compile(r"(^|/)secrets?\.(json|ya?ml|toml)$"),
    re.compile(r"(^|/)\.npmrc$"),
    re.compile(r"(^|/)\.netrc$"),
]


def looks_like_secret(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _SECRET_PATTERNS)


def safe_repo_path(repo_path: str, rel_path: str) -> Optional[str]:
    """Resolve `rel_path` inside `repo_path`, or None if it would escape."""
    repo_root = os.path.realpath(repo_path)
    candidate = os.path.realpath(os.path.join(repo_root, rel_path))
    if os.path.commonpath([repo_root, candidate]) != repo_root:
        return None
    return candidate


def estimate_tokens(text: str) -> int:
    """~4 chars/token: fine for routing, not billing-accurate."""
    return max(1, len(text) // 4) if text else 0


def _resolve_log_entry(repo_path: str, entry: str):
    """Return (content, path_or_None); a path escaping the repo is treated as literal text."""
    safe_path = safe_repo_path(repo_path, entry)
    if safe_path is not None and os.path.isfile(safe_path):
        if looks_like_secret(entry):
            return f"<<refused: {entry!r} matches a secret-file pattern, not sent to the model>>", entry
        try:
            with open(safe_path, "r", errors="replace") as f:
                return f.read(), entry
        except OSError as e:
            return f"<<could not read {entry}: {e}>>", entry
    return entry, None


def _resolve_diff_entry(repo_path: str, entry: str) -> str:
    """Diff a git range, else treat `entry` as literal diff text.

    Entries starting with "-" never reach git: "--output=<path>" would let a
    task write an arbitrary file."""
    if entry.startswith("-"):
        return entry
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


def _max_untrusted_tokens() -> int:
    """Policy fence cap; late import so an unreadable policy file is non-fatal."""
    try:
        from src.routing_policy import load_policy
        return int(load_policy().get("maxUntrustedTokens", 256))
    except Exception:
        return 256


def fence_untrusted(content: str, source: str, max_tokens: Optional[int] = None) -> str:
    """Truncate to the policy cap and fence; models treat fenced text as evidence only."""
    cap = max_tokens if max_tokens is not None else _max_untrusted_tokens()
    max_chars = cap * 4  # inverse of estimate_tokens' ~4 chars/token
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n<<truncated to {cap} untrusted tokens by policy>>"
    return f"{UNTRUSTED_FENCE_START.format(source=source)}\n{content}\n{UNTRUSTED_FENCE_END}"


def _provenance(source_type: str, uri: Optional[str], content: str,
                redaction_applied: bool, trusted: bool) -> dict:
    """ContextSource record; aclChecked means the repo jail and denylist applied."""
    return {
        "sourceType": source_type,
        "uri": uri,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "aclChecked": uri is not None,
        "redactionApplied": redaction_applied,
        "maySendToRemoteModel": True,  # engine-level sensitivity filter decides per task
        "promptInjectionRisk": "low" if trusted else "high",
        "tokenCount": estimate_tokens(content),
    }


def build_context_bundle(task) -> dict:
    """Build a ContextBundle {task_id, files, logs, metadata, sources}.

    Repo files and git diffs are trusted; inline log/diff literals from the task
    JSON are untrusted and fenced. Everything is redacted here."""
    inputs = json.loads(task.inputs) if task.inputs else {}
    files_list = inputs.get("files") or []
    logs_list = inputs.get("logs") or []
    diffs_list = inputs.get("diffs") or []
    test_commands = inputs.get("test_commands") or []

    sources: list = []
    any_redaction = False

    files = []
    seen_paths = set()
    for rel_path in files_list:
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)

        safe_path = safe_repo_path(task.repo_path, rel_path)
        if safe_path is None:
            content = f"<<refused: {rel_path!r} resolves outside repo_path, not read>>"
        elif looks_like_secret(rel_path):
            content = f"<<refused: {rel_path!r} matches a secret-file pattern, not sent to the model>>"
        else:
            try:
                with open(safe_path, "r", errors="replace") as f:
                    content = f.read()
            except OSError as e:
                content = f"<<could not read {rel_path}: {e}>>"

        content, redacted = redact_text(content)
        any_redaction = any_redaction or redacted
        sources.append(_provenance("trusted_repo_code", rel_path, content, redacted, trusted=True))
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
        content, redacted = redact_text(content)
        any_redaction = any_redaction or redacted
        if path is not None:
            sources.append(_provenance("trusted_test_log", path, content, redacted, trusted=True))
        else:
            sources.append(_provenance("untrusted_issue_text", None, content, redacted, trusted=False))
            content = fence_untrusted(content, source="task.inputs.logs")
        logs.append({"path": path, "content": content, "reason": "explicitly listed in task.inputs.logs"})

    for entry in diffs_list:
        content = _resolve_diff_entry(task.repo_path, entry)
        from_git = content is not entry  # _resolve_diff_entry returns entry unchanged when not a git range
        content, redacted = redact_text(content)
        any_redaction = any_redaction or redacted
        if from_git:
            sources.append(_provenance("trusted_repo_code", f"git-diff:{entry}", content, redacted, trusted=True))
        else:
            sources.append(_provenance("untrusted_issue_text", None, content, redacted, trusted=False))
            content = fence_untrusted(content, source="task.inputs.diffs")
        logs.append({"path": None, "content": content, "reason": f"diff: {entry}"})

    metadata = _build_metadata(task.repo_path, test_commands)
    metadata["token_estimate"] = sum(f["token_estimate"] for f in files) + sum(
        estimate_tokens(l["content"]) for l in logs
    )
    metadata["redaction_applied"] = any_redaction

    return {
        "task_id": task.id,
        "prompt": inputs.get("prompt"),
        "acceptance_criteria": inputs.get("acceptance_criteria") or [],
        "files": files,
        "logs": logs,
        "metadata": metadata,
        "sources": sources,
    }
