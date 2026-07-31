"""Field listing via Tableau Metadata GraphQL (same auth as MCP: Connected App or PAT)."""

from __future__ import annotations

import re
from typing import Any, Literal, TypedDict

import httpx

from backend.config import env, httpx_verify, require_env
from backend.tableau_auth import auth_mode, probe_tableau_sign_in, sign_in

# Re-export for callers / health
__all__ = ["probe_tableau_sign_in", "sign_in_pat", "fetch_published_datasource_fields", "check_metadata_api_access"]

DEFAULT_REST_VERSION = "3.27"
LUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

TROUBLESHOOTING = [
    "API Access on a datasource (green checkmarks in Tableau Web) = query-datasource / VizQL. It does NOT grant Metadata API field listing.",
    "Tableau Server: enable Metadata API — tsm maintenance metadata-services enable (requires server admin; services restart).",
    "With Connected App: chat runs as the Tableau username you enter (JWT sub). Permissions follow that user.",
    "Permissions are per datasource: access on one datasource does not apply to another.",
    "Test: GET /api/metadata-check and /api/datasource-fields?name=YourDatasource",
    "If Metadata API stays forbidden, use query-datasource with the LUID from list-datasources anyway.",
]

FIELD_FRAGMENT = """
  __typename
  name
  ... on ColumnField { dataType }
  ... on CalculatedField { formula }
"""


def _server_base() -> str:
    s = require_env("TABLEAU_SERVER")
    return s.rstrip("/")


def _rest_version() -> str:
    return env("TABLEAU_REST_API_VERSION") or DEFAULT_REST_VERSION


def _site_content_url() -> str:
    return env("TABLEAU_SITE_NAME")


def _client() -> httpx.Client:
    return httpx.Client(verify=httpx_verify(), timeout=120.0)


def sign_in_pat() -> tuple[str, str]:
    """Sign in — Connected App JWT when configured, otherwise PAT."""
    return sign_in()


def _build_query(filter_key: Literal["luid", "name", "nameWithin"], var_type: str) -> str:
    if filter_key == "luid":
        filt = "luid: $id"
    elif filter_key == "nameWithin":
        filt = "nameWithin: [$id]"
    else:
        filt = "name: $id"
    return f"""
query Fields($id: {var_type}!) {{
  publishedDatasources(filter: {{ {filt} }}) {{
    name
    luid
    fieldsConnection(first: 2000, permissionMode: OBFUSCATE_RESULTS) {{
      nodes {{ {FIELD_FRAGMENT} }}
    }}
  }}
}}"""


def _map_field(f: dict[str, Any]) -> dict[str, Any] | None:
    name = f.get("name")
    if not name:
        return None
    return {
        "name": name,
        "typename": f.get("__typename") or "Field",
        "dataType": f.get("dataType"),
        "formula": f.get("formula"),
    }


def _rows_to_matches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_fields = 2000
    matches: list[dict[str, Any]] = []
    for ds in rows:
        nodes = (
            (ds.get("fieldsConnection") or {}).get("nodes")
            or (ds.get("fields") if isinstance(ds.get("fields"), list) else [])
            or []
        )
        fields: list[dict[str, Any]] = []
        for rf in nodes[:max_fields]:
            m = _map_field(rf) if isinstance(rf, dict) else None
            if m:
                fields.append(m)
        if len(nodes) > max_fields:
            fields.append(
                {
                    "name": "_truncation",
                    "typename": "Notice",
                    "formula": f"Only first {max_fields} fields returned.",
                }
            )
        matches.append(
            {
                "name": ds.get("name") or "",
                "luid": ds.get("luid") or "",
                "fields": fields,
            }
        )
    return matches


def _run_metadata_query(
    token: str,
    filter_key: Literal["luid", "name", "nameWithin"],
    id_val: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]] | None, int]:
    var_type = "[String!]" if filter_key == "nameWithin" else "String!"
    query = _build_query(filter_key, var_type)
    variables: dict[str, Any] = {"id": [id_val] if filter_key == "nameWithin" else id_val}
    gql_url = f"{_server_base()}/api/metadata/graphql"
    with _client() as client:
        res = client.post(
            gql_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Tableau-Auth": token,
            },
            json={"query": query, "variables": variables},
        )
    try:
        body = res.json()
    except Exception:
        body = {}
    if res.status_code == 403:
        raise RuntimeError(
            "Metadata GraphQL HTTP 403 Forbidden. Datasource API Access in Tableau Web is not the same as Metadata API access. "
            f"Body: {str(body)[:400]}"
        )
    if not res.is_success:
        raise RuntimeError(f"Metadata GraphQL HTTP {res.status_code}: {str(body)[:800]}")
    rows = body.get("data", {}).get("publishedDatasources") or []
    errors = body.get("errors")
    return rows, errors, res.status_code


def fetch_published_datasource_fields(identifier: str) -> dict[str, Any]:
    id_val = identifier.strip()
    if not id_val:
        raise ValueError("identifier is empty")

    token, _ = sign_in_pat()
    by_luid = bool(LUID_RE.match(id_val))
    matched_by: Literal["luid", "name", "nameWithin"] = "luid" if by_luid else "name"
    rows, errors, _ = _run_metadata_query(token, "luid" if by_luid else "name", id_val)
    matches = _rows_to_matches(rows)

    if not matches and not by_luid:
        rows2, errors2, _ = _run_metadata_query(token, "nameWithin", id_val)
        if rows2:
            matched_by = "nameWithin"
            matches = _rows_to_matches(rows2)
            errors = errors2

    if errors:
        return {
            "identifier": id_val,
            "matchedBy": matched_by,
            "matches": [],
            "graphqlErrors": errors,
            "hint": "Metadata API returned errors (often permissions or Metadata API disabled on Server).",
            "troubleshooting": TROUBLESHOOTING,
        }

    if not matches:
        return {
            "identifier": id_val,
            "matchedBy": matched_by,
            "matches": [],
            "hint": "No published datasource matched this name/LUID, or your user cannot see its fields in the Metadata API. list-datasources may still work — use the LUID from that response.",
            "troubleshooting": TROUBLESHOOTING,
        }

    if all(len(m["fields"]) == 0 for m in matches):
        return {
            "identifier": id_val,
            "matchedBy": matched_by,
            "matches": matches,
            "hint": "Datasource was found but fields list is empty — usually View permission on the datasource or Metadata indexing still in progress.",
            "troubleshooting": TROUBLESHOOTING,
        }

    return {"identifier": id_val, "matchedBy": matched_by, "matches": matches, "graphqlErrors": errors}


def check_metadata_api_access() -> dict[str, Any]:
    site = _site_content_url()
    base: dict[str, Any] = {
        "server": _server_base(),
        "siteContentUrl": site,
        "authMode": auth_mode(),
        "patName": env("TABLEAU_PAT_NAME"),
        "signInOk": False,
        "metadataReachable": False,
        "note": (
            "Datasource “API Access” (green check on the datasource) allows query-datasource / VDS. "
            "Field listing uses Metadata API (/api/metadata/graphql), which is a separate server feature and permission model."
        ),
    }

    try:
        token, _ = sign_in_pat()
        base["signInOk"] = True
    except Exception as e:
        base["hint"] = str(e)
        return base

    probe = "query Probe { publishedDatasourcesConnection(first: 3) { nodes { name luid } } }"
    gql_url = f"{_server_base()}/api/metadata/graphql"
    with _client() as client:
        res = client.post(
            gql_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Tableau-Auth": token,
            },
            json={"query": probe},
        )
    base["metadataHttpStatus"] = res.status_code
    try:
        body = res.json()
    except Exception:
        base["hint"] = f"Metadata endpoint returned HTTP {res.status_code} with non-JSON body."
        return base

    if res.status_code == 403:
        base["graphqlErrors"] = body.get("errors")
        base["hint"] = (
            "HTTP 403 Forbidden on Metadata API — usually Metadata services disabled on Server, "
            "or this PAT’s user lacks catalog/metadata rights. Datasource API Access does not fix this."
        )
        return base

    if not res.is_success:
        base["graphqlErrors"] = body.get("errors")
        base["hint"] = f"Metadata GraphQL HTTP {res.status_code}. See graphqlErrors."
        return base

    base["metadataReachable"] = True
    base["graphqlErrors"] = body.get("errors")
    nodes = body.get("data", {}).get("publishedDatasourcesConnection", {}).get("nodes") or []
    base["sampleDatasourceCount"] = len(nodes)

    errs = body.get("errors") or []
    if any(re.search(r"forbidden|denied|permission", e.get("message", ""), re.I) for e in errs if isinstance(e, dict)):
        base["hint"] = (
            "GraphQL returned permission errors. Grant View on each datasource; confirm PAT was created as the same user who has Administer on the datasource."
        )
    elif len(nodes) == 0 and not errs:
        base["hint"] = "Metadata API responded but returned 0 datasources — check site name or indexing still running."

    return base
