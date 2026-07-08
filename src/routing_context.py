"""src/routing_context.py — builds a bounded ContextBundle (spec Section 9)
from a RoutingTask's explicit inputs. v1 scope: explicit files/logs/diffs
only, no smart stack-trace-following auto-inclusion -- that's a documented
future enhancement, not implemented here.

Phase 2 governance (spec Section 9): every bundle item carries a ContextSource
provenance record; untrusted items (inline text that didn't come from the repo
or a repo file read) are wrapped in <<<UNTRUSTED_START/END>>> fences and
capped at the policy's maxUntrustedTokens; secret/PII redaction runs at bundle
build time — BEFORE any prompt assembly — so nothing downstream (local or
remote) ever sees a raw credential."""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from src.routing_redaction import redact_text

UNTRUSTED_FENCE_START = '<<<UNTRUSTED_START source="{source}">>>'
UNTRUSTED_FENCE_END = "<<<UNTRUSTED_END>>>"

# Filenames/patterns that are legitimately in-repo but should never be read
# into a prompt shipped to a third-party model, even via an ordinary
# (non-traversal) task.inputs.files entry. Not exhaustive -- a deliberate,
# reasonable default denylist, matching this app's existing "never send
# secrets externally" posture (see CLAUDE.md's OpenRouter delegation
# guidance in the sibling PersonalOS repo) rather than a security boundary.
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
    """Resolve `rel_path` relative to `repo_path`, refusing to let it escape
    the repo root via an absolute path or `..` traversal. Matches the
    realpath+commonpath jailing convention already used elsewhere in this
    app (src/crew_orchestrator.py's Mnemosyne jail, src/dev_preview.py's
    REPOS_ROOT jail, src/tool_execution.py's project_root confinement).
    Returns None if `rel_path` would resolve outside `repo_path`."""
    repo_root = os.path.realpath(repo_path)
    candidate = os.path.realpath(os.path.join(repo_root, rel_path))
    if os.path.commonpath([repo_root, candidate]) != repo_root:
        return None
    return candidate


def estimate_tokens(text: str) -> int:
    """Approximate token estimator (~4 chars/token) -- good enough for
    routing/budget decisions, not billing-accurate."""
    return max(1, len(text) // 4) if text else 0


def _resolve_log_entry(repo_path: str, entry: str):
    """`entry` is either a path relative to repo_path, or literal log
    content already. Returns (content, path_or_None). A path that would
    escape repo_path (absolute or `..` traversal) is treated as literal
    content instead of being read -- same containment rule as
    build_context_bundle's file loop."""
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
    """`entry` is either a git range (e.g. "main...HEAD") to diff, or literal
    diff text already. Heuristic: if it contains ".." or looks like a bare
    ref, try `git diff`; fall back to treating it as literal text. Entries
    starting with "-" are never passed to git -- a bare non-`--`-separated
    argument like "--output=/some/path" would otherwise let a task JSON
    make git write to an arbitrary filesystem path (a real git flag, not a
    shell-injection risk since subprocess is called with a list, no
    shell=True)."""
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
    """Policy-configurable fence cap (spec Section 9 default: 256 tokens).
    Late import: routing_policy has no dependency on this module, but keep the
    coupling one-directional and non-fatal if the policy file is unreadable."""
    try:
        from src.routing_policy import load_policy
        return int(load_policy().get("maxUntrustedTokens", 256))
    except Exception:
        return 256


def fence_untrusted(content: str, source: str, max_tokens: Optional[int] = None) -> str:
    """Wrap untrusted content in the spec's delimiters, truncating to the
    policy cap first. The prompt wrapper (routing_prompts) instructs models to
    treat fenced content as evidence and ignore any instructions inside."""
    cap = max_tokens if max_tokens is not None else _max_untrusted_tokens()
    max_chars = cap * 4  # inverse of estimate_tokens' ~4 chars/token
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n<<truncated to {cap} untrusted tokens by policy>>"
    return f"{UNTRUSTED_FENCE_START.format(source=source)}\n{content}\n{UNTRUSTED_FENCE_END}"


def _provenance(source_type: str, uri: Optional[str], content: str,
                redaction_applied: bool, trusted: bool) -> dict:
    """ContextSource record (spec Section 9). aclChecked reflects that repo
    reads went through the safe_repo_path jail + secret denylist; inline
    content has no ACL to check. promptInjectionRisk is coarse: untrusted
    free text is 'high', repo-derived content 'low'."""
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
    """`task` is a core.database.RoutingTask row. Returns a dict matching the
    spec's ContextBundle shape: {task_id, files, logs, metadata, sources}.

    Trust model: file reads and git-produced diffs are trusted_repo_code /
    trusted_test_log; INLINE log/diff literals arrived as free text in the
    task JSON and are treated as untrusted — fenced, capped, and flagged
    high-injection-risk. All content is redacted here, before any prompt."""
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
            # Inline literal from the task JSON: untrusted evidence. Fence +
            # cap BEFORE it can reach any prompt.
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
