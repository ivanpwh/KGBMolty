"""
Version check — GET /api/version and X-Version header management.
Returns 426 VERSION_MISMATCH if outdated.
"""
import httpx
from bot.config import API_BASE, get_skill_version, set_skill_version
from bot.utils.logger import get_logger

log = get_logger(__name__)


async def check_version(client: httpx.AsyncClient) -> str:
    """Fetch current server version and update runtime version. Returns version string."""
    try:
        resp = await client.get(f"{API_BASE}/version")
        if resp.status_code == 200:
            data = resp.json()
            server_version = data.get("version", get_skill_version())
            if server_version != get_skill_version():
                log.warning("Server version %s != local %s — updating", server_version, get_skill_version())
                set_skill_version(server_version)
            return server_version
    except Exception as e:
        log.warning("Version check failed: %s", e)
    return get_skill_version()


def get_version_header() -> dict:
    """Return X-Version header dict."""
    return {"X-Version": get_skill_version()}
