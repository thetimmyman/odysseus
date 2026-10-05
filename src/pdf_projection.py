"""Optional bounded PDF projection client; existing indexes remain authoritative."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from src.constants import PDF_PROJECTION_COMMAND

MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
PROFILE = "docling-native-pages-v1"
SOURCE_COMMIT = "eda5b5fc86d10b2b79a900ff3e69fc159d5f6014"
PROVIDER_VERSION = "2.133.0"


class PDFProjectionError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return bool(PDF_PROJECTION_COMMAND)


def source_digest(path: str) -> str:
    with open(path, "rb") as stream:
        data = stream.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise PDFProjectionError("rejected", "PDF exceeds the 30 MiB profile")
    return hashlib.sha256(data).hexdigest()


def validate_result(result: dict, digest: str, byte_size: int) -> dict:
    if not isinstance(result, dict):
        raise PDFProjectionError("failed", "Projection result must be an object")
    if result.get("status") != "complete":
        raise PDFProjectionError(str(result.get("status", "failed")),
                                 str(result.get("diagnostic", "Projection did not complete")))
    if (result.get("schema") != "document-page-projection-v1"
            or result.get("profile") != PROFILE
            or result.get("source_sha256") != digest
            or type(result.get("byte_size")) is not int or result["byte_size"] != byte_size
            or result.get("provider_version") != PROVIDER_VERSION
            or result.get("source_commit") != SOURCE_COMMIT):
        raise PDFProjectionError("failed", "Projection source binding or profile differs")
    for name in ("runtime_manifest_sha256", "adapter_sha256"):
        if not isinstance(result.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", result[name]):
            raise PDFProjectionError("failed", "Projection runtime receipt is missing")
    resources = result.get("resources")
    if not isinstance(resources, dict) or resources.get("scope") != "worker_and_descendants" or resources.get("limits") != {
        "memory.max": "2147483648", "memory.swap.max": "0", "pids.max": "64",
        "cpu.max": "200000 100000"
    }:
        raise PDFProjectionError("failed", "Projection resource boundary is not admitted")
    pages = result.get("pages")
    count = result.get("page_count")
    if type(count) is not int or not 1 <= count <= 48 or not isinstance(pages, list):
        raise PDFProjectionError("failed", "Projection physical-page count is invalid")
    if len(pages) != count or any(
        not isinstance(page, dict) or type(page.get("physical_page")) is not int
        or page["physical_page"] != idx or not isinstance(page.get("text"), str)
        for idx, page in enumerate(pages, 1)
    ):
        raise PDFProjectionError("failed", "Projection page sequence is invalid")
    return result


def extract(path: str) -> dict:
    # Configuration is a JSON argv vector, never a shell command.
    try:
        command = json.loads(PDF_PROJECTION_COMMAND)
        if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg for arg in command
        ) or not Path(command[0]).is_absolute():
            raise ValueError("Expected an absolute executable and JSON argv")
        with open(path, "rb") as stream:
            data = stream.read(MAX_PDF_BYTES + 1)
        if not data or len(data) > MAX_PDF_BYTES:
            raise PDFProjectionError("rejected", "PDF is outside the input byte cap")
        # Bounded adapter writes regular files; its child FSIZE cap also applies.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            subprocess.run(command, input=data, stdout=output, stderr=errors,
                           timeout=50, check=True)
            output.seek(0)
            raw = output.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise PDFProjectionError("failed", "Projection output exceeds the byte cap")
        return validate_result(json.loads(raw), hashlib.sha256(data).hexdigest(), len(data))
    except PDFProjectionError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise PDFProjectionError("timeout", "Bounded projection command timed out") from exc
    except Exception as exc:
        raise PDFProjectionError("unavailable", "Bounded PDF projection is unavailable") from exc


def chunk_metadata(projection: dict, physical_page: int) -> dict:
    return {"pdf_projection": PROFILE, "source_sha256": projection["source_sha256"],
            "physical_page": physical_page, "pdf_page_count": projection["page_count"],
            "pdf_parser_version": projection["provider_version"]}


def citation(metadata: dict) -> str:
    digest = metadata.get("source_sha256", "")
    page = metadata.get("physical_page")
    count = metadata.get("pdf_page_count")
    if (metadata.get("pdf_projection") == PROFILE and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) and type(page) is int
            and type(count) is int and 1 <= page <= count <= 48):
        return f" :: physical page {page} :: sha256 {digest}"
    return ""


def current(metadata: dict, path: str) -> bool:
    if metadata.get("pdf_projection") != PROFILE:
        # Existing PDFs need reindexing before they can satisfy the opt-in contract.
        return not (enabled() and Path(path).suffix.lower() == ".pdf")
    if not citation(metadata):
        return False
    try:
        return source_digest(path) == metadata["source_sha256"]
    except (OSError, PDFProjectionError):
        return False
