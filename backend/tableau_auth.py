"""Tableau authentication: Connected App (direct-trust) JWT or PAT."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

import httpx
import jwt

from backend.auth_context import get_tableau_username
from backend.config import env, httpx_verify, require_env

DEFAULT_REST_VERSION = "3.27"

# Scopes needed for Metadata GraphQL + typical REST content reads used by this app.
_REST_JWT_SCOPES = [
    "tableau:content:read",
    "tableau:viz_data_service:read",
    "tableau:views:download",
]


def _server_base() -> str:
    return require_env("TABLEAU_SERVER").rstrip("/")


def _rest_version() -> str:
    return env("TABLEAU_REST_API_VERSION") or DEFAULT_REST_VERSION


def _site_content_url() -> str:
    return env("TABLEAU_SITE_NAME")


def _client() -> httpx.Client:
    return httpx.Client(verify=httpx_verify(), timeout=120.0)


def connected_app_configured() -> bool:
    return bool(
        env("TABLEAU_CONNECTED_APP_CLIENT_ID")
        and env("TABLEAU_CONNECTED_APP_SECRET_ID")
        and env("TABLEAU_CONNECTED_APP_SECRET")
    )


def pat_configured() -> bool:
    return bool(env("TABLEAU_PAT_NAME") and env("TABLEAU_PAT_VALUE"))


def auth_mode() -> Literal["direct-trust", "pat", "none"]:
    if connected_app_configured():
        return "direct-trust"
    if pat_configured():
        return "pat"
    return "none"


def resolve_jwt_username(explicit: str | None = None) -> str | None:
    """Username for JWT sub: request → context → TABLEAU_JWT_SUB_CLAIM."""
    for candidate in (explicit, get_tableau_username(), env("TABLEAU_JWT_SUB_CLAIM")):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def mint_connected_app_jwt(username: str, *, scopes: list[str] | None = None) -> str:
    client_id = require_env("TABLEAU_CONNECTED_APP_CLIENT_ID")
    secret_id = require_env("TABLEAU_CONNECTED_APP_SECRET_ID")
    secret = require_env("TABLEAU_CONNECTED_APP_SECRET")
    now = int(time.time())
    # Tableau Direct Trust: kid + iss in header; sub/aud/exp/jti/scp (+ optional nbf) in payload.
    payload = {
        "sub": username.strip(),
        "aud": "tableau",
        "nbf": now,
        "exp": now + 5 * 60,
        "jti": str(uuid.uuid4()),
        "scp": scopes or list(_REST_JWT_SCOPES),
    }
    return jwt.encode(
        payload,
        secret,
        algorithm="HS256",
        headers={"kid": secret_id, "iss": client_id},
    )


def sign_in_with_jwt(username: str) -> tuple[str, str]:
    token_jwt = mint_connected_app_jwt(username)
    url = f"{_server_base()}/api/{_rest_version()}/auth/signin"
    with _client() as client:
        res = client.post(
            url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "credentials": {
                    "site": {"contentUrl": _site_content_url()},
                    "jwt": token_jwt,
                }
            },
        )
    raw = res.text
    if not res.is_success:
        raise RuntimeError(
            f"Tableau Connected App sign-in failed ({res.status_code}) for user {username!r}: "
            f"{raw[:500]}"
        )
    try:
        j = res.json()
        token = j.get("credentials", {}).get("token")
        site_id = j.get("credentials", {}).get("site", {}).get("id") or ""
        if not token:
            raise ValueError("no token")
        return token, site_id
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(
            f"Tableau JWT sign-in response invalid. Raw start: {raw[:200]}"
        ) from e


def sign_in_with_pat() -> tuple[str, str]:
    pat_name = require_env("TABLEAU_PAT_NAME")
    pat_value = require_env("TABLEAU_PAT_VALUE")
    url = f"{_server_base()}/api/{_rest_version()}/auth/signin"
    with _client() as client:
        res = client.post(
            url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "credentials": {
                    "site": {"contentUrl": _site_content_url()},
                    "personalAccessTokenName": pat_name,
                    "personalAccessTokenSecret": pat_value,
                }
            },
        )
    raw = res.text
    if not res.is_success:
        raise RuntimeError(f"Tableau PAT sign-in failed ({res.status_code}): {raw[:500]}")
    try:
        j = res.json()
        token = j.get("credentials", {}).get("token")
        site_id = j.get("credentials", {}).get("site", {}).get("id") or ""
        if not token:
            raise ValueError("no token")
        return token, site_id
    except (ValueError, KeyError, TypeError) as e:
        if str(e) == "no token":
            raise RuntimeError(
                "Tableau sign-in returned no JSON token. Use a server that supports JSON sign-in, or check PAT/site."
            ) from e
        raise RuntimeError(
            f"Tableau sign-in response was not JSON (often XML on older servers). Raw start: {raw[:200]}"
        ) from e


def sign_in(username: str | None = None) -> tuple[str, str]:
    """Sign in as Connected App user when configured, else PAT."""
    mode = auth_mode()
    if mode == "direct-trust":
        user = resolve_jwt_username(username)
        if not user:
            raise RuntimeError(
                "Connected App is configured but no Tableau username was provided. "
                "Pass tableauUsername from the extension, or set TABLEAU_JWT_SUB_CLAIM in .env."
            )
        return sign_in_with_jwt(user)
    if mode == "pat":
        return sign_in_with_pat()
    raise RuntimeError(
        "No Tableau auth configured. Set Connected App vars "
        "(TABLEAU_CONNECTED_APP_*) or TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE."
    )


def probe_tableau_sign_in() -> dict[str, Any]:
    mode = auth_mode()
    if mode == "none":
        return {
            "tableauSignInOk": False,
            "authMode": "none",
            "tableauHint": "Set Connected App (TABLEAU_CONNECTED_APP_*) or PAT vars in .env.",
        }
    try:
        if mode == "direct-trust":
            user = resolve_jwt_username()
            if not user:
                return {
                    "tableauSignInOk": False,
                    "authMode": "direct-trust",
                    "requiresTableauUsername": True,
                    "tableauHint": (
                        "Connected App ready. Open the extension and enter your Tableau username "
                        "(e.g. demoAdmin or local\\demoAdmin), or set TABLEAU_JWT_SUB_CLAIM."
                    ),
                }
            sign_in_with_jwt(user)
            return {
                "tableauSignInOk": True,
                "authMode": "direct-trust",
                "requiresTableauUsername": True,
                "jwtSubClaim": user,
            }
        sign_in_with_pat()
        return {"tableauSignInOk": True, "authMode": "pat", "requiresTableauUsername": False}
    except Exception as e:
        msg = str(e)
        if mode == "direct-trust":
            hint = (
                "Connected App JWT sign-in failed (often Tableau 401001). Checklist: "
                "(1) Connected App is Enabled on site Settings → Connected Apps; "
                "(2) Domain allowlist is All / includes your server; "
                "(3) TABLEAU_SITE_NAME matches that site (e.g. demo); "
                "(4) username matches Tableau login (try demoAdmin and local\\demoAdmin); "
                "(5) Client ID / Secret ID / Secret Value copied exactly."
            )
        else:
            hint = (
                "Regenerate PAT on Tableau (My Account → Personal Access Tokens) and update "
                "TABLEAU_PAT_VALUE in .env."
            )
        if "401" in msg or "invalid" in msg.lower() or "could not find user" in msg.lower():
            hint = msg[:400]
        return {
            "tableauSignInOk": False,
            "authMode": mode,
            "requiresTableauUsername": mode == "direct-trust",
            "tableauHint": hint,
            "tableauError": msg[:400],
        }
