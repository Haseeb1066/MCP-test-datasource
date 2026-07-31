#!/usr/bin/env python3
"""Test Tableau Connected App JWT sign-in — run: python scripts/test-connected-app.py [username]"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import env  # noqa: E402
from backend.tableau_auth import (  # noqa: E402
    connected_app_configured,
    probe_tableau_sign_in,
    sign_in_with_jwt,
)


def main() -> int:
    username = (sys.argv[1] if len(sys.argv) > 1 else env("TABLEAU_JWT_SUB_CLAIM") or "demoAdmin").strip()
    print("SERVER:", env("TABLEAU_SERVER"))
    print("SITE_NAME:", repr(env("TABLEAU_SITE_NAME")))
    print("Connected App configured:", connected_app_configured())
    print("JWT username (sub):", repr(username))
    if not connected_app_configured():
        print("FAIL: Set TABLEAU_CONNECTED_APP_CLIENT_ID, SECRET_ID, SECRET in .env")
        return 1
    try:
        token, site_id = sign_in_with_jwt(username)
        print("OK: signed in. site_id=", site_id, "token_len=", len(token))
        return 0
    except Exception as e:
        print("FAIL:", e)
        print("probe:", probe_tableau_sign_in())
        print("Tips: try username without domain (demoAdmin) or with local\\demoAdmin;")
        print("      TABLEAU_SITE_NAME must match the site where the Connected App was created (e.g. demo).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
