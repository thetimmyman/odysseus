"""Build-time source identity. Runtime Git and environment values are not evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from src.constants import APP_VERSION, BUILD_IDENTITY_FILE

SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


def validate_fields(value):
    if not isinstance(value, dict) or not isinstance(value.get("git_sha"), str) or not SHA.fullmatch(value["git_sha"]):
        raise ValueError("build identity requires a full 40-character source SHA")
    if not isinstance(value.get("branch"), str) or not BRANCH.fullmatch(value["branch"]):
        raise ValueError("build identity requires a branch")
    try:
        timestamp = datetime.fromisoformat(value["built_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("missing timezone")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("build identity requires a timestamp with timezone") from exc
    return {key: value[key] for key in ("git_sha", "branch", "built_at")}


def read_identity(path=None):
    unknown = {"status": "unknown", "git_sha": None, "branch": None, "built_at": None}
    try:
        value = json.loads(Path(path or BUILD_IDENTITY_FILE).read_text())
        if not isinstance(value, dict) or value.get("format") != 1:
            return unknown
        return {"status": "pinned", **validate_fields(value)}
    except (OSError, ValueError, TypeError):
        return unknown


def version_payload():
    return {"version": APP_VERSION, **read_identity()}


def verify(value, expected_sha, image_revision, branch="main", now=None):
    if not SHA.fullmatch(expected_sha):
        raise ValueError("expected source must be a full 40-character SHA")
    fields = validate_fields(value)
    if value.get("status") != "pinned" or fields["git_sha"] != expected_sha or image_revision != expected_sha:
        raise ValueError("application, image and expected source identity do not agree")
    if fields["branch"] != branch:
        raise ValueError("application build branch does not match the intended branch")
    now = now or datetime.now(timezone.utc)
    if datetime.fromisoformat(fields["built_at"].replace("Z", "+00:00")) > now:
        raise ValueError("application build timestamp is in the future")
    return {"status": "verified", **fields}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="Bake metadata during image construction.")
    write.add_argument("--git-sha", default="")
    write.add_argument("--branch", default="")
    write.add_argument("--built-at", default="")
    check = commands.add_parser("verify", help="Verify version JSON on stdin against source and image.")
    check.add_argument("--expect-sha", required=True)
    check.add_argument("--image-revision", required=True)
    check.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            value = {"git_sha": args.git_sha, "branch": args.branch, "built_at": args.built_at}
            fields = validate_fields(value) if any(value.values()) else {}
            Path(BUILD_IDENTITY_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(BUILD_IDENTITY_FILE).write_text(json.dumps({"format": 1, **fields}, sort_keys=True) + "\n")
            Path(BUILD_IDENTITY_FILE).chmod(0o444)
        else:
            print(json.dumps(verify(json.load(sys.stdin), args.expect_sha, args.image_revision, args.branch), sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"build identity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
