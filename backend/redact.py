import os
import re


def redact_tableau_secrets(text: str) -> str:
    s = text
    for key in ("TABLEAU_PAT_VALUE", "TABLEAU_CONNECTED_APP_SECRET", "CONNECTED_APP_SECRET_VALUE"):
        pat = (os.environ.get(key) or "").strip()
        if pat and len(pat) > 8:
            s = s.replace(pat, "[REDACTED_TABLEAU_SECRET]")
    s = re.sub(
        r'"personalAccessTokenSecret"\s*:\s*"[^"]*"',
        '"personalAccessTokenSecret":"[REDACTED]"',
        s,
        flags=re.I,
    )
    s = re.sub(
        r'"X-Tableau-Auth"\s*:\s*"[^"]*"',
        '"X-Tableau-Auth":"[REDACTED]"',
        s,
        flags=re.I,
    )
    s = re.sub(
        r'"jwt"\s*:\s*"[^"]*"',
        '"jwt":"[REDACTED]"',
        s,
        flags=re.I,
    )
    return s
