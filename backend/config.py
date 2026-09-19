import os
from pathlib import Path

# Windows: ProactorEventLoop before any asyncio subprocess (MCP stdio)
import backend.platform_fix  # noqa: F401

from dotenv import load_dotenv

# Load .env from project root (parent of backend/)
_ROOT = Path(__file__).resolve().parent.parent
# .env must win over stale shell exports (e.g. old TABLEAU_SITE_NAME).
load_dotenv(_ROOT / ".env", override=True)


def _strip_env_value(value: str) -> str:
    """Trim and remove optional surrounding quotes from .env values."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return _strip_env_value(raw)


def require_env(name: str) -> str:
    v = env(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def httpx_verify() -> bool:
    # Default off: many Tableau Server installs use a private/self-signed CA
    # (nunomics.ai fails verify=True). Set TABLEAU_SSL_VERIFY=1 to enforce.
    raw = env("TABLEAU_SSL_VERIFY", "0").lower()
    return raw in ("1", "true", "yes")


def httpx_client(*, timeout: float = 120.0) -> "httpx.Client":
    """Tableau HTTP client — ignore HTTP(S)_PROXY so cloud hosts don't hang."""
    import httpx

    return httpx.Client(verify=httpx_verify(), timeout=timeout, trust_env=False)
