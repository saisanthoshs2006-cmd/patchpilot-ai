"""Central configuration. Every tunable number lives here, not scattered in engines."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables always win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / ".patchpilot"
DB_PATH = DATA_DIR / "history.sqlite3"
LLM_CACHE_DIR = PROJECT_ROOT / "demo" / "llm_cache"

# --- Ingestion limits -------------------------------------------------------
MAX_FILES = 50
MAX_TOTAL_BYTES = 500_000
MAX_FILE_BYTES = 200_000
SUPPORTED_EXTENSIONS = {".py"}
IGNORED_DIR_NAMES = {
    "__pycache__", ".git", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "__MACOSX",  # junk added by macOS Finder's "Compress"
}
IGNORED_FILE_NAMES = {".DS_Store", "Thumbs.db"}

# --- LLM ---------------------------------------------------------------------
# Provider: explicit PATCHPILOT_LLM_PROVIDER wins; otherwise whichever key is configured. With no key,
# Groq: the recorded demo responses (demo/llm_cache) were made with it, so replay works out of the box.
LLM_PROVIDER = os.environ.get("PATCHPILOT_LLM_PROVIDER") or (
    "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "groq"
)
_DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "groq": "openai/gpt-oss-120b"}[LLM_PROVIDER]
REASONING_MODEL = os.environ.get("PATCHPILOT_REASONING_MODEL", _DEFAULT_MODEL)
PATCH_MODEL = os.environ.get("PATCHPILOT_PATCH_MODEL", _DEFAULT_MODEL)
REASONING_EFFORT = os.environ.get("PATCHPILOT_REASONING_EFFORT", "high")  # low|medium|high|xhigh|max
PATCH_EFFORT = os.environ.get("PATCHPILOT_PATCH_EFFORT", "medium")  # a targeted fix needs less deliberation
# auto: replay from cache when possible, call the API otherwise (and record)
# replay: cache only, never touch the network (demo-safe) | offline: no AI engine at all
LLM_MODE = os.environ.get("PATCHPILOT_LLM_MODE", "auto")
# Public deployments must not run uploaded code: fix verification executes the project's tests,
# and the sandbox is not a security boundary. On unless explicitly turned off (local .env).
PUBLIC_MODE = os.environ.get("PATCHPILOT_PUBLIC", "1") != "0"
LLM_MAX_PARALLEL = 2 if LLM_PROVIDER == "groq" else 4  # Groq's free tier has tight per-minute token limits
LLM_MAX_RETRIES = 6  # SDKs back off on 429 using the server's retry-after
LLM_TIMEOUT_S = 180
LLM_MAX_TOKENS = 6000 if LLM_PROVIDER == "groq" else 16000  # Groq counts this against its per-minute token cap
AI_CHUNK_MAX_LINES = 150  # functions are grouped into chunks of up to this many lines per request
AI_CONFIDENCE_CAP = 0.8  # an unproven AI claim never displays as more certain than this

# --- Analysis thresholds -----------------------------------------------------
COMPLEXITY_WARN = 10  # cyclomatic complexity per function
FUNCTION_MAX_LINES = 50
FUNCTION_MAX_PARAMS = 6

# --- Patching / verification -------------------------------------------------
MAX_FINDINGS_TO_PATCH = 8
PATCH_REPAIR_ATTEMPTS = 1  # extra tries when verification rejects a fix, with its evidence as feedback
SANDBOX_TIMEOUT_S = 20
