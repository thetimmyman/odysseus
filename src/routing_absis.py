"""Dispatcher for the ``absis_tacticus_job_queue`` execution backend.

It only submits jobs to the ABSIS Redis queue and reads their state back, so
oracle/evidence gates stay intact; routing to this backend is decided upstream.
The queue has no HTTP API and is not reachable from the host, so every
operation runs as a one-shot python script inside the orchestrator pod over
ssh + kubectl exec, keeping the Redis secret in the cluster.

An unclaimable job is requeued and FAILED within seconds, so ``enqueue``
refuses unless ``check_availability`` finds a matching worker.

Embedded values go through json.dumps (valid Python literals) and the script is
shlex.quote()d; identifiers are whitelist-validated and rejected, never escaped.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

WORKER_CLASSES = ("llm_inference", "oracle_runner")

# Terminal job statuses in the ABSIS lifecycle (queued→assigned→running→…).
TERMINAL_STATUSES = ("completed", "failed")

# Anything outside these whitelists is rejected, never escaped.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
_SAFE_JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{1,64}$")

# Kept in sync with routing_policy.DEFAULT_POLICY["absis"]; duplicated here so
# this module stays usable when the policy file predates the absis section.
DEFAULT_ABSIS_POLICY = {
    "enabled": False,
    "sshTarget": "minipc",
    "kubectlExecPrefix": "sudo kubectl exec -n tacticus deploy/absis-orchestrator --",
    "transportTimeoutSeconds": 30,
    "note": ("no llm_inference/oracle_runner workers deployed as of 2026-07-08; "
             "enable after workers exist in tacticus-analytics"),
}


class AbsisValidationError(ValueError):
    """A field failed the whitelist validation (bad worker class, or an
    identifier containing quotes/backslashes/newlines/etc.)."""


class AbsisTransportError(RuntimeError):
    """The ssh/kubectl one-shot failed, timed out, or returned non-JSON."""


def load_absis_policy() -> dict:
    """The policy's 'absis' section over DEFAULT_ABSIS_POLICY, so an older file
    still yields a complete, disabled config."""
    from src.routing_policy import load_policy

    cfg = dict(DEFAULT_ABSIS_POLICY)
    section = load_policy().get("absis")
    if isinstance(section, dict):
        cfg.update(section)
    return cfg


def _validate_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise AbsisValidationError(
            f"{name} must match {_SAFE_ID_RE.pattern} (got {value!r}); "
            "quotes, backslashes, whitespace and newlines are rejected, not escaped"
        )
    return value


def _validate_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not _SAFE_JOB_ID_RE.match(job_id):
        raise AbsisValidationError(f"job_id must match {_SAFE_JOB_ID_RE.pattern} (got {job_id!r})")
    return job_id


@dataclass
class AbsisJobSpec:
    """Client-side ABSIS job spec whose serialization must match
    absis_infra.schemas.Job's wire format exactly (sort_keys, enum values)."""

    scenario_id: str
    required_worker_class: str
    required_capabilities: List[str] = field(default_factory=list)
    timeout_s: int = 600
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _validate_identifier("scenario_id", self.scenario_id)
        if self.required_worker_class not in WORKER_CLASSES:
            raise AbsisValidationError(
                f"required_worker_class must be one of {WORKER_CLASSES} "
                f"(got {self.required_worker_class!r})"
            )
        if not isinstance(self.required_capabilities, list):
            raise AbsisValidationError("required_capabilities must be a list of strings")
        for cap in self.required_capabilities:
            _validate_identifier("capability", cap)
        if not isinstance(self.timeout_s, int) or isinstance(self.timeout_s, bool) or self.timeout_s <= 0:
            raise AbsisValidationError(f"timeout_s must be a positive int (got {self.timeout_s!r})")
        if not isinstance(self.payload, dict):
            raise AbsisValidationError("payload must be a dict")

    def to_job_dict(self) -> Dict[str, Any]:
        """A fresh Job dict with the real defaults; mints a new job_id/created_at."""
        try:
            payload = json.loads(json.dumps(self.payload))  # must be JSON-serializable
        except (TypeError, ValueError) as e:
            raise AbsisValidationError(f"payload is not JSON-serializable: {e}")
        return {
            "scenario_id": self.scenario_id,
            "required_worker_class": self.required_worker_class,
            "required_capabilities": list(self.required_capabilities),
            "priority": 0,
            "timeout_s": self.timeout_s,
            "job_id": str(uuid.uuid4()),
            "created_at": time.time(),
            "payload": payload,
            "status": "queued",
            "assigned_worker_id": None,
            "attempts": 0,
            "last_error": None,
        }

    def to_job_json(self) -> str:
        return json.dumps(self.to_job_dict(), sort_keys=True)


# Each remote script prints exactly one JSON line; values are embedded only as
# json.dumps() literals.

_SCAN_WORKERS_SCRIPT = """\
import json, os
import redis
r = redis.from_url(os.environ["REDIS_URL"])
workers = []
cursor = 0
while True:
    cursor, keys = r.scan(cursor=cursor, match="conformance:worker:*", count=200)
    for k in keys:
        raw = r.get(k)
        if raw is None:
            continue
        try:
            workers.append(json.loads(raw))
        except Exception:
            pass
    if cursor == 0:
        break
print(json.dumps({"registered_workers": workers}, sort_keys=True, default=str))
"""


def build_scan_workers_script() -> str:
    """SCAN worker heartbeat keys (30s TTL), so the result is the live worker set."""
    return _SCAN_WORKERS_SCRIPT


def build_enqueue_script(wire_json: str) -> str:
    """LPUSH + SET + PUBLISH for one job; ``wire_json`` is embedded as a literal."""
    if not isinstance(wire_json, str):
        raise AbsisValidationError("wire_json must be a str")
    job = json.loads(wire_json)  # must be valid JSON with a job_id before we ship it
    _validate_job_id(job.get("job_id"))
    return (
        "import json, os\n"
        "import redis\n"
        "r = redis.from_url(os.environ[\"REDIS_URL\"])\n"
        f"wire = {json.dumps(wire_json)}\n"
        "job = json.loads(wire)\n"
        "r.lpush(\"conformance:jobs\", wire)\n"
        "r.set(\"conformance:job:\" + job[\"job_id\"], wire)\n"
        "r.publish(\"conformance:status\", wire)\n"
        "print(json.dumps({\"enqueued\": True, \"job_id\": job[\"job_id\"]}))\n"
    )


def build_get_status_script(job_id: str) -> str:
    _validate_job_id(job_id)
    return (
        "import json, os\n"
        "import redis\n"
        "r = redis.from_url(os.environ[\"REDIS_URL\"])\n"
        f"job_id = {json.dumps(job_id)}\n"
        "raw = r.get(\"conformance:job:\" + job_id)\n"
        "if raw is None:\n"
        "    print(json.dumps({\"found\": False, \"job\": None}))\n"
        "else:\n"
        "    print(json.dumps({\"found\": True, \"job\": json.loads(raw)}, sort_keys=True))\n"
    )


class AbsisTransport:
    """Runs a python one-shot inside the orchestrator pod over ssh + kubectl exec.

    Local argv is a list (no shell=True); the script is shlex.quote()d so the
    remote shell sees one argument."""

    def __init__(self, ssh_target: str = "minipc",
                 kubectl_exec_prefix: str = DEFAULT_ABSIS_POLICY["kubectlExecPrefix"],
                 timeout_s: int = 30):
        self.ssh_target = ssh_target
        self.kubectl_exec_prefix = kubectl_exec_prefix
        self.timeout_s = timeout_s

    @classmethod
    def from_policy(cls, cfg: Optional[dict] = None) -> "AbsisTransport":
        cfg = cfg if cfg is not None else load_absis_policy()
        return cls(
            ssh_target=cfg.get("sshTarget", DEFAULT_ABSIS_POLICY["sshTarget"]),
            kubectl_exec_prefix=cfg.get("kubectlExecPrefix", DEFAULT_ABSIS_POLICY["kubectlExecPrefix"]),
            timeout_s=cfg.get("transportTimeoutSeconds", DEFAULT_ABSIS_POLICY["transportTimeoutSeconds"]),
        )

    def build_argv(self, script: str) -> List[str]:
        remote_cmd = f"{self.kubectl_exec_prefix} python -c {shlex.quote(script)}"
        return ["ssh", self.ssh_target, remote_cmd]

    def run_remote_python(self, script: str) -> dict:
        """Parse the last non-empty stdout line, so kubectl chatter can't break parsing."""
        argv = self.build_argv(script)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise AbsisTransportError(
                f"absis transport timed out after {self.timeout_s}s (ssh {self.ssh_target})")
        except OSError as e:
            raise AbsisTransportError(f"absis transport failed to launch ssh: {e}")
        if proc.returncode != 0:
            raise AbsisTransportError(
                f"absis one-shot exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise AbsisTransportError("absis one-shot produced no output")
        try:
            result = json.loads(lines[-1])
        except ValueError:
            raise AbsisTransportError(f"absis one-shot returned non-JSON: {lines[-1][:200]!r}")
        if not isinstance(result, dict):
            raise AbsisTransportError("absis one-shot result was not a JSON object")
        return result


def list_registered_workers(transport: AbsisTransport) -> List[dict]:
    result = transport.run_remote_python(build_scan_workers_script())
    workers = []
    for w in result.get("registered_workers") or []:
        if not isinstance(w, dict):
            continue
        workers.append({
            "worker_id": w.get("worker_id"),
            "worker_class": w.get("worker_class"),
            "capabilities": list(w.get("capabilities") or []),
        })
    return workers


def _matching_workers(workers: Sequence[dict], worker_class: str,
                      capabilities: Sequence[str]) -> List[dict]:
    required = set(capabilities or [])
    return [w for w in workers
            if w.get("worker_class") == worker_class
            and required.issubset(set(w.get("capabilities") or []))]


def check_availability(transport: AbsisTransport, worker_class: str,
                       capabilities: Optional[Sequence[str]] = None) -> dict:
    """True when a live worker matches the class and covers the capabilities.

    Feeds GateContext.backend_available: unclaimable jobs fail within seconds."""
    if worker_class not in WORKER_CLASSES:
        raise AbsisValidationError(
            f"worker_class must be one of {WORKER_CLASSES} (got {worker_class!r})")
    for cap in capabilities or []:
        _validate_identifier("capability", cap)
    workers = list_registered_workers(transport)
    matching = _matching_workers(workers, worker_class, capabilities or [])
    if matching:
        reason = None
    elif not workers:
        reason = "no_workers_registered"
    else:
        reason = "no_matching_worker"
    return {"available": bool(matching), "workers": workers, "reason": reason}


def enqueue(transport: AbsisTransport, spec: AbsisJobSpec, force: bool = False) -> dict:
    """Submit one job; refuses without a matching worker. force=True is for tests."""
    if not force:
        avail = check_availability(transport, spec.required_worker_class,
                                   spec.required_capabilities)
        if not avail["available"]:
            return {"job_id": None, "enqueued": False,
                    "error": "no_matching_worker",
                    "availability": {"workers": avail["workers"], "reason": avail["reason"]}}
    job = spec.to_job_dict()
    wire = json.dumps(job, sort_keys=True)
    result = transport.run_remote_python(build_enqueue_script(wire))
    if not result.get("enqueued"):
        return {"job_id": job["job_id"], "enqueued": False, "error": "remote_enqueue_failed"}
    return {"job_id": job["job_id"], "enqueued": True, "error": None}


def get_status(transport: AbsisTransport, job_id: str) -> dict:
    """Parsed job dict from conformance:job:<id>, or {"error": "not_found"}."""
    result = transport.run_remote_python(build_get_status_script(job_id))
    if not result.get("found") or not isinstance(result.get("job"), dict):
        return {"error": "not_found", "job_id": job_id}
    return result["job"]


def wait_for_terminal(transport: AbsisTransport, job_id: str, timeout_s: int,
                      poll_interval: float = 5) -> dict:
    """Poll until completed/failed; on timeout return the last observed status."""
    _validate_job_id(job_id)
    deadline = time.monotonic() + max(0, timeout_s)
    last_status = None
    while True:
        job = get_status(transport, job_id)
        if job.get("error") != "not_found":
            last_status = job.get("status")
            if last_status in TERMINAL_STATUSES:
                return job
        if time.monotonic() >= deadline:
            return {"error": "timeout", "job_id": job_id, "last_status": last_status,
                    "timeout_s": timeout_s}
        time.sleep(poll_interval)


def map_job_to_model_run(job: dict) -> dict:
    """Pure mapping of an ABSIS job to a RoutingModelRun-shaped outcome; results
    live only in the job payload, hence artifacts.absis_payload."""
    status = job.get("status")
    job_id = job.get("job_id")
    notes = (f"absis job {job_id} status={status} "
             f"attempts={job.get('attempts')} worker={job.get('assigned_worker_id')}")
    return {
        "completed": status == "completed",
        "errored": status == "failed",
        "error_message": job.get("last_error"),
        "notes": notes,
        "artifacts": {"absis_payload": job.get("payload") or {}},
    }
