# src/constants.py
"""Application-wide constants and configuration values."""
import os

APP_VERSION = "1.0.0"

# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
STATIC_DIR = os.path.join(BASE_DIR, "static")
# Startup repairs ownership below /app; image identity must stay outside that tree.
BUILD_IDENTITY_FILE = "/usr/local/share/odysseus/build-identity.json"
DATA_DIR = os.getenv("ODYSSEUS_DATA_DIR", os.path.join(BASE_DIR, "data"))

# Data file paths
# Single source of truth: every persisted file/dir lives under DATA_DIR, which
# is the ONLY place ODYSSEUS_DATA_DIR is read. Import these constants instead of
# re-deriving paths from __file__ or a relative "data" literal.
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
MEMORY_DOC = os.path.join(DATA_DIR, "memory_doc.md")
PERSONAL_DIR = os.path.join(DATA_DIR, "personal_docs")
RUNBOOK_DIR = os.path.join(PERSONAL_DIR, "runbook")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
FEATURES_FILE = os.path.join(DATA_DIR, "features.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
USER_PREFS_FILE = os.path.join(DATA_DIR, "user_prefs.json")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
INTEGRATIONS_FILE = os.path.join(DATA_DIR, "integrations.json")
CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
APP_KEY_FILE = os.path.join(DATA_DIR, ".app_key")
EMBEDDING_ENDPOINT_FILE = os.path.join(DATA_DIR, "embedding_endpoint.json")
COOKBOOK_STATE_FILE = os.path.join(DATA_DIR, "cookbook_state.json")
BG_JOBS_FILE = os.path.join(DATA_DIR, "bg_jobs.json")
VAULT_FILE = os.path.join(DATA_DIR, "vault.json")
TIDY_CALENDAR_STATE_FILE = os.path.join(DATA_DIR, "tidy_calendar_state.json")
SKILLS_FILE = os.path.join(DATA_DIR, "skills.json")
APP_DB = os.path.join(DATA_DIR, "app.db")
CANONICAL_INSPECTOR_ROOT = os.path.join(DATA_DIR, "canonical_inspector")
CANONICAL_INSPECTOR_WORK_ROOT = os.path.join(CANONICAL_INSPECTOR_ROOT, "work")
CANONICAL_INSPECTOR_RETAINED_ROOT = os.path.join(CANONICAL_INSPECTOR_ROOT, "retained")
CANONICAL_TMOS_MANIFEST_FILE = os.path.join(CANONICAL_INSPECTOR_ROOT, "tmos-owner.json")
LOCAL_MEASUREMENT_COMMAND = os.path.join(BASE_DIR, "scripts", "odysseus-measure-once")
CANONICAL_TMOS_VERIFICATION_DIRNAME = "verification"
CANONICAL_TMOS_TRANSPORT_CLIENT_DIRNAME = "transport-client"
CANONICAL_TMOS_TRANSPORT_EXECUTOR_DIRNAME = "transport-executor"
CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME = "transport-unresolved.json"
CANONICAL_TMOS_CAPTURE_INTENT_FILENAME = "intent.json"
CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME = "birth.json"
CANONICAL_TMOS_CAPTURE_DESCENDANT_FILENAME = "descendant.json"
CANONICAL_TMOS_CAPTURE_COMPLETE_FILENAME = "complete.json"
CANONICAL_TMOS_CAPTURE_PROJECTION_FILENAME = "projection.json"
CANONICAL_TMOS_CAPTURE_CONTROL_PREFIX = "control-"
ROUTING_SANDBOX_ARTIFACT_DIRNAME = "_sandbox"
ROUTING_SANDBOX_OWNERSHIP_SUFFIX = ".ownership.json"
# Ephemeral private SSH config copies must stay outside checkouts and ignore TMPDIR.
LOCAL_MEASUREMENT_CONFIG_TEMP_ROOT = "/tmp"
SSH_SYSTEM_CONFIG_FILE = "/etc/ssh/ssh_config"

# Source-only pilot. No environment variable enables a live adapter or schema.
CANONICAL_INSPECTOR_ENABLED = False
CANONICAL_PILOT_ID = "synthetic-uppercase"
CANONICAL_TMOS_WORK_ID = "tmos-routing-inspector"
CANONICAL_TMOS_PACKET_IDS = ("routing-inspector-command", "routing-inspector-control")
CANONICAL_TMOS_DISCLOSURE_GENERATION = "tmos-routing-inspector-v1"
CANONICAL_PILOT_DISCLOSURE_GENERATION = "public-uppercase-v1"
CANONICAL_PILOT_BODY_BYTES = 512
CANONICAL_PILOT_BODY_SECONDS = 5
CANONICAL_PILOT_KEY_BYTES = 128
CANONICAL_PILOT_CONTEXT_TOKENS = 4096
CANONICAL_PILOT_OUTPUT_TOKENS = 512
CANONICAL_PILOT_WORKER_SECONDS = 180
CANONICAL_PILOT_BROKER_WAIT_SECONDS = 30
CANONICAL_PILOT_SCRATCH_BYTES = 1048576
CANONICAL_PILOT_SETUP_SECONDS = 5
CANONICAL_PILOT_SETUP_TOTAL_SECONDS = 30
CANONICAL_PILOT_SETUP_STDOUT_BYTES = 65536
CANONICAL_PILOT_SETUP_STDERR_BYTES = 65536
CANONICAL_PILOT_SETUP_TOTAL_BYTES = 262144
CANONICAL_PILOT_ORACLE_SECONDS = 30
CANONICAL_PILOT_ORACLE_MEMORY_BYTES = 268435456
CANONICAL_PILOT_ORACLE_TASKS = 16
CANONICAL_PILOT_ORACLE_STDOUT_BYTES = 16384
CANONICAL_PILOT_ORACLE_STDERR_BYTES = 16384
CANONICAL_PILOT_OBSERVATION_BYTES = 65536
CANONICAL_PILOT_GENERATION_BYTES = 262144
CANONICAL_PILOT_PROVIDER_TOTAL_BYTES = 524288
CANONICAL_PILOT_OBSERVATION_CALLS = 6
CANONICAL_PILOT_GENERATION_CALLS = 1
CANONICAL_PILOT_OBSERVATION_SECONDS = 10
CANONICAL_PILOT_TERMINATION_GRACE_SECONDS = 1
CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS = 2
CANONICAL_PILOT_LEASE_POLL_SECONDS = 0.05
CANONICAL_INSPECTOR_BUNDLE_BYTES = 2097152
CANONICAL_INSPECTOR_BLOB_BYTES = 1048576
CANONICAL_INSPECTOR_TOTAL_BLOB_BYTES = 8388608
CANONICAL_INSPECTOR_ARTIFACTS = 64
CANONICAL_INSPECTOR_READ_SECONDS = 5.0
CANONICAL_INSPECTOR_RESPONSE_BYTES = 65536
SCHEDULED_EMAILS_DB = os.path.join(DATA_DIR, "scheduled_emails.db")
EMAIL_CACHE_DB = os.path.join(DATA_DIR, "email_cache.db")

# Data subdirectories
PERSONAL_UPLOADS_DIR = os.path.join(DATA_DIR, "personal_uploads")
EMOJI_CACHE_DIR = os.path.join(DATA_DIR, "emoji_cache")
RAG_DIR = os.path.join(DATA_DIR, "rag")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma")
BG_JOBS_DIR = os.path.join(DATA_DIR, "bg_jobs")
DEEP_RESEARCH_DIR = os.path.join(DATA_DIR, "deep_research")
MCP_OAUTH_DIR = os.path.join(DATA_DIR, "mcp_oauth")
GENERATED_IMAGES_DIR = os.path.join(DATA_DIR, "generated_images")
TTS_CACHE_DIR = os.path.join(DATA_DIR, "tts_cache")
EMAIL_URGENCY_CACHE_DIR = os.path.join(DATA_DIR, "email_urgency_cache")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
GALLERY_DIR = os.path.join(DATA_DIR, "gallery")
GALLERY_UPLOADS_DIR = os.path.join(DATA_DIR, "gallery_uploads")
MEMORY_VECTORS_DIR = os.path.join(DATA_DIR, "memory_vectors")

# Paths with an intentional dedicated env override, defaulting under DATA_DIR.
MAIL_ATTACHMENTS_DIR = os.getenv("ODYSSEUS_MAIL_ATTACHMENTS_DIR", os.path.join(DATA_DIR, "mail-attachments"))
# ``or`` (not a getenv default) so an env var set to the EMPTY string still
# falls back — docker-compose passes FASTEMBED_CACHE_PATH=${FASTEMBED_CACHE_PATH:-},
# i.e. "" when unset on the host, and getenv(x, default) would return "".
FASTEMBED_CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or os.path.join(DATA_DIR, "fastembed_cache")

# Agent tool output limits (single source of truth — imported by tool_execution.py,
# tool_implementations.py, agent_tools.py, and any other module that needs them)
MAX_OUTPUT_CHARS = 10_000       # cap for bash/python/web_search/web_fetch output
MAX_READ_CHARS = 20_000         # cap for read_file / document preview
MAX_DIFF_LINES = 400            # cap for edit_file unified-diff display
MAX_WORKSPACE_PATH_LENGTH = 4096  # session workspace API and request validation

# API Configuration
MAX_CONTEXT_MESSAGES = 90
REQUEST_TIMEOUT = 20
OPENAI_COMPAT_PATH = "/v1/chat/completions"

# Environment variables with defaults
DEFAULT_HOST = os.getenv("LLM_HOST", "localhost")
LLM_HOSTS = [h.strip() for h in os.getenv("LLM_HOSTS", "").split(",") if h.strip()]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SEARXNG_INSTANCE = os.getenv("SEARXNG_INSTANCE", "http://localhost:8080")


# Cleanup configuration
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "True").lower() == "true"
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))

# Default parameters
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 0


def internal_api_base() -> str:
    """Base URL for in-process loopback calls to Odysseus's own API.

    Agent tools and background jobs reach admin-gated routes by calling the
    running server over HTTP. Resolution order:
      1. ODYSSEUS_INTERNAL_BASE  - explicit override (e.g. behind a TLS proxy).
      2. APP_PORT                - http://127.0.0.1:$APP_PORT (docker-compose).
      3. Fallback http://127.0.0.1:7000 - legacy default.

    127.0.0.1 (not "localhost") avoids IPv6/DNS ambiguity for a strictly-local
    call. Without this, loopback tools fail with "All connection attempts
    failed" whenever the server is not on port 7000.
    """
    override = os.environ.get("ODYSSEUS_INTERNAL_BASE")
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('APP_PORT', '7000')}"

# Optional trusted, bounded PDF adapter argv as JSON. Empty keeps the existing parser.
PDF_PROJECTION_COMMAND = os.getenv("ODYSSEUS_PDF_PROJECTION_COMMAND", "")

# Local capability registry authority files and explicit profile override.
TARGET_CAPABILITY_ACTIVE_FILENAME = "active.json"
TARGET_CAPABILITY_LOCK_FILENAME = ".writer.lock"
TARGET_CAPABILITY_PROFILE_REGISTRY_ENV = "PS632_PROFILE_REGISTRY"

# Private evidence emitted by the bounded, operator-run local qualification.
LOCAL_QUALIFICATION_REQUEST_FILENAME = "trial-{trial}-request.json"
LOCAL_QUALIFICATION_RESPONSE_FILENAME = "trial-{trial}-response.json"
LOCAL_QUALIFICATION_REPORT_FILENAME = "qualification.json"
LOCAL_QUALIFICATION_RECEIPT_FILENAME = "receipt.json"
LOCAL_QUALIFICATION_BEFORE_FILENAME = "identity-before.json"
LOCAL_QUALIFICATION_AFTER_FILENAME = "identity-after.json"
LOCAL_QUALIFICATION_FAILURE_FILENAME = "failure.json"
LOCAL_MEASUREMENT_OWNERSHIP_FILENAME = "ownership.json"

# Content-addressed artifacts inside a caller-selected retained evidence root.
EVIDENCE_BLOB_DIRNAME = "blobs"
