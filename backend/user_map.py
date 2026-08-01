"""Map Tableau Extensions uniqueUserId → Tableau username (JWT sub)."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from backend.config import env, httpx_verify, require_env
from backend.tableau_auth import (
    connected_app_configured,
    mint_connected_app_jwt,
    pat_configured,
    sign_in_with_jwt,
    sign_in_with_pat,
)

_ROOT = Path(__file__).resolve().parents[1]
_MAP_PATH = Path(env("TABLEAU_USER_MAP_PATH") or str(_ROOT / "data" / "user_map.json"))
_lock = threading.Lock()

_USER_LIST_SCOPES = [
    "tableau:content:read",
    "tableau:users:read",
    "tableau:viz_data_service:read",
    "tableau:views:download",
]


def _server_base() -> str:
    return require_env("TABLEAU_SERVER").rstrip("/")


def _rest_version() -> str:
    return env("TABLEAU_REST_API_VERSION") or "3.27"


def _load_map() -> dict[str, str]:
    if not _MAP_PATH.is_file():
        return {}
    try:
        raw = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _save_map(data: dict[str, str]) -> None:
    _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _MAP_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(_MAP_PATH)


def get_mapped_username(unique_user_id: str) -> str | None:
    uid = (unique_user_id or "").strip()
    if not uid:
        return None
    with _lock:
        return _load_map().get(uid)


def remember_user(unique_user_id: str, username: str) -> None:
    uid = (unique_user_id or "").strip()
    name = (username or "").strip()
    if not uid or not name:
        return
    with _lock:
        data = _load_map()
        if data.get(uid) == name:
            return
        data[uid] = name
        _save_map(data)


def _hash_candidates(username: str) -> set[str]:
    """Possible opaque ids derived from a login name (Tableau does not document the algorithm)."""
    variants = {
        username,
        username.lower(),
        username.upper(),
        username.replace("\\", "/"),
        username.replace("/", "\\"),
    }
    # Strip domain prefix: local\demoAdmin -> demoAdmin
    if "\\" in username:
        variants.add(username.split("\\", 1)[-1])
        variants.add(username.split("\\", 1)[-1].lower())
    if "/" in username:
        variants.add(username.split("/", 1)[-1])

    out: set[str] = set()
    for v in variants:
        if not v:
            continue
        out.add(v)
        raw = v.encode("utf-8")
        out.add(hashlib.md5(raw).hexdigest())
        out.add(hashlib.sha1(raw).hexdigest())
        out.add(hashlib.sha256(raw).hexdigest())
        out.add(hashlib.sha256(v.encode("utf-16-le")).hexdigest())
    return out


def _sign_in_for_user_list() -> tuple[str, str]:
    """Admin/bootstrap session used only to Query Users for uniqueUserId matching."""
    sync_user = (env("TABLEAU_JWT_SUB_CLAIM") or env("TABLEAU_USER_SYNC_USERNAME") or "").strip()
    if connected_app_configured() and sync_user:
        # Prefer JWT with users:read for listing.
        token_jwt = mint_connected_app_jwt(sync_user, scopes=_USER_LIST_SCOPES)
        import httpx

        url = f"{_server_base()}/api/{_rest_version()}/auth/signin"
        with httpx.Client(verify=httpx_verify(), timeout=120.0) as client:
            res = client.post(
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={
                    "credentials": {
                        "site": {"contentUrl": env("TABLEAU_SITE_NAME")},
                        "jwt": token_jwt,
                    }
                },
            )
        if not res.is_success:
            # Fall back to normal JWT scopes then PAT.
            return sign_in_with_jwt(sync_user)
        j = res.json()
        token = j.get("credentials", {}).get("token")
        site_id = j.get("credentials", {}).get("site", {}).get("id") or ""
        if token:
            return token, site_id
        return sign_in_with_jwt(sync_user)

    if pat_configured():
        return sign_in_with_pat()
    if connected_app_configured() and sync_user:
        return sign_in_with_jwt(sync_user)
    raise RuntimeError(
        "Cannot list Tableau users to resolve uniqueUserId. "
        "Set TABLEAU_JWT_SUB_CLAIM to a site admin username (for user sync), or configure a PAT."
    )


def list_site_users() -> list[dict[str, str]]:
    """Return [{id, name, fullName}, ...] for the configured site."""
    import httpx

    token, site_id = _sign_in_for_user_list()
    if not site_id:
        raise RuntimeError("Tableau sign-in returned no site id")

    users: list[dict[str, str]] = []
    page = 1
    with httpx.Client(verify=httpx_verify(), timeout=120.0) as client:
        while True:
            url = (
                f"{_server_base()}/api/{_rest_version()}/sites/{site_id}/users"
                f"?pageSize=100&pageNumber={page}"
            )
            res = client.get(
                url,
                headers={"Accept": "application/json", "X-Tableau-Auth": token},
            )
            if not res.is_success:
                raise RuntimeError(f"Query users failed ({res.status_code}): {res.text[:400]}")
            body = res.json()
            block = body.get("users") or {}
            raw_users = block.get("user") or []
            if isinstance(raw_users, dict):
                raw_users = [raw_users]
            for u in raw_users:
                if not isinstance(u, dict):
                    continue
                name = (u.get("name") or "").strip()
                uid = (u.get("id") or "").strip()
                if not name:
                    continue
                users.append(
                    {
                        "id": uid,
                        "name": name,
                        "fullName": (u.get("fullName") or "").strip(),
                    }
                )
            pagination = body.get("pagination") or {}
            try:
                total = int(pagination.get("totalAvailable") or len(users))
                page_size = int(pagination.get("pageSize") or 100)
                page_number = int(pagination.get("pageNumber") or page)
            except (TypeError, ValueError):
                break
            if page_number * page_size >= total:
                break
            page += 1
            if page > 50:
                break
    return users


def match_username_from_site_users(unique_user_id: str) -> str | None:
    """Match Extensions uniqueUserId to a site username (LUID or hash of login name)."""
    uid = (unique_user_id or "").strip()
    if not uid:
        return None
    try:
        users = list_site_users()
    except Exception:
        return None

    uid_cf = uid.casefold()
    for u in users:
        name = u["name"]
        if u.get("id") and u["id"].casefold() == uid_cf:
            return name
        if name.casefold() == uid_cf:
            return name
        if uid in _hash_candidates(name) or uid_cf in {h.casefold() for h in _hash_candidates(name)}:
            return name
    return None


def resolve_username(
    *,
    unique_user_id: str | None = None,
    tableau_username: str | None = None,
) -> dict[str, Any]:
    """
    Resolve JWT username for a viewer from uniqueUserId only.

    Client-supplied tableau_username is ignored for security (impersonation risk).
    Maps are written only when matched via Tableau site users (LUID/name/hash).
    """
    del tableau_username  # intentionally unused — never trust client username
    uid = (unique_user_id or "").strip() or None

    if uid:
        mapped = get_mapped_username(uid)
        if mapped:
            return {
                "tableauUsername": mapped,
                "uniqueUserId": uid,
                "source": "map",
                "resolved": True,
            }
        matched = match_username_from_site_users(uid)
        if matched:
            remember_user(uid, matched)
            return {
                "tableauUsername": matched,
                "uniqueUserId": uid,
                "source": "tableau-users",
                "resolved": True,
            }

    sync_default = (env("TABLEAU_JWT_SUB_CLAIM") or "").strip() or None
    return {
        "tableauUsername": None,
        "uniqueUserId": uid,
        "source": None,
        "resolved": False,
        "hint": (
            "Could not resolve Tableau username from uniqueUserId. "
            "Ensure TABLEAU_JWT_SUB_CLAIM (or admin PAT) can Query Users, "
            "and the viewer’s uniqueUserId matches a site user LUID."
        ),
        "syncUserConfigured": bool(sync_default),
    }
