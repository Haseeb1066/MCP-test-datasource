"""Published datasource listing / resolve helpers (Tableau MCP + optional Metadata)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import env, httpx_verify
from backend.mcp_tableau import call_tool, tool_result_to_text
from backend.tableau_fields import DEFAULT_REST_VERSION, _server_base, sign_in_pat
from backend.workbooks import _extract_json_payload, _normalize_label


@dataclass
class DatasourceSummary:
    id: str
    name: str
    project_name: str | None = None
    is_published: bool | None = None

    def to_api_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.project_name:
            d["projectName"] = self.project_name
        if self.is_published is not None:
            d["isPublished"] = self.is_published
        return d


SelectedDatasource = DatasourceSummary


def _parse_datasource_row(raw: Any) -> DatasourceSummary | None:
    if not isinstance(raw, dict):
        return None
    did = raw.get("id") or raw.get("luid") or raw.get("datasourceLuid")
    name = raw.get("name")
    if not isinstance(did, str) or not did or not isinstance(name, str) or not name:
        return None
    project = raw.get("project") if isinstance(raw.get("project"), dict) else None
    is_published = raw.get("isPublished")
    project_name = None
    if project and isinstance(project.get("name"), str):
        project_name = project["name"]
    elif isinstance(raw.get("projectName"), str):
        project_name = raw["projectName"]
    return DatasourceSummary(
        id=did,
        name=name,
        project_name=project_name,
        is_published=is_published if isinstance(is_published, bool) else None,
    )


def _rows_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("datasources", "data", "items", "publishedDatasources"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if isinstance(payload.get("datasource"), dict):
            return [payload["datasource"]]
    return []


def parse_datasources_from_tool_text(text: str) -> list[DatasourceSummary]:
    payload = _extract_json_payload(text)
    rows = _rows_from_payload(payload)
    out: list[DatasourceSummary] = []
    seen: set[str] = set()
    for row in rows:
        ds = _parse_datasource_row(row)
        if not ds or ds.id in seen:
            continue
        seen.add(ds.id)
        out.append(ds)
    out.sort(key=lambda d: d.name.casefold())
    return out


async def list_datasources_via_mcp() -> list[DatasourceSummary]:
    result = await call_tool("list-datasources", {}, force_datasource_tools=True)
    text = tool_result_to_text(result)
    if result.get("isError"):
        raise RuntimeError(f"list-datasources failed: {text[:800]}")
    datasources = parse_datasources_from_tool_text(text)
    if not datasources and text.strip():
        raise RuntimeError(f"list-datasources returned no parseable datasources. Preview: {text[:400]}")
    return datasources


def parse_fields_from_mcp_metadata(payload: dict[str, Any], *, datasource_luid: str = "") -> dict[str, Any]:
    """Normalize get-datasource-metadata JSON into list-published-datasource-fields shape."""
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in payload.get("fieldGroups") or []:
        if not isinstance(group, dict):
            continue
        for raw in group.get("fields") or []:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            fields.append(
                {
                    "name": name,
                    "typename": raw.get("columnClass") or "Field",
                    "dataType": raw.get("dataType"),
                    "formula": raw.get("formula"),
                }
            )
    fields.sort(key=lambda f: f["name"].casefold())
    return {
        "identifier": datasource_luid,
        "matchedBy": "get-datasource-metadata",
        "matches": [
            {
                "name": "",
                "luid": datasource_luid,
                "fields": fields,
            }
        ],
        "source": "mcp-get-datasource-metadata",
        "fieldCount": len(fields),
    }


async def fetch_fields_via_mcp_metadata(datasource_luid: str) -> dict[str, Any]:
    """Field listing fallback when Tableau Metadata GraphQL is unavailable (403)."""
    luid = (datasource_luid or "").strip()
    if not luid:
        return {"error": "datasourceLuid is required"}
    raw = await call_tool(
        "get-datasource-metadata",
        {"datasourceLuid": luid},
        force_datasource_tools=True,
    )
    text = tool_result_to_text(raw)
    if raw.get("isError"):
        return {"error": text[:800], "identifier": luid, "matchedBy": "get-datasource-metadata"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"error": f"Unparseable get-datasource-metadata response: {text[:400]}", "identifier": luid}
    if not isinstance(payload, dict):
        return {"error": "Unexpected metadata payload type", "identifier": luid}
    return parse_fields_from_mcp_metadata(payload, datasource_luid=luid)


_GENERIC_NAME_TOKENS = frozenset(
    {
        "ai",
        "mcp",
        "data",
        "dataset",
        "extract",
        "source",
        "sources",
        "sales",
        "demo",
        "test",
        "v1",
        "v2",
        "dashboard",
        "workbook",
        "ap",
    }
)


def pick_primary_datasource(datasources: list[DatasourceSummary]) -> list[DatasourceSummary]:
    """Keep a single primary published datasource for dashboard-scoped chat."""
    published = [d for d in datasources if d.id and d.is_published is not False]
    if published:
        return [published[0]]
    with_id = [d for d in datasources if d.id]
    if with_id:
        return [with_id[0]]
    return datasources[:1] if datasources else []


def resolve_datasources_from_list(
    all_datasources: list[DatasourceSummary],
    *,
    names: list[str] | None = None,
    ids: list[str] | None = None,
    exact_name_only: bool = False,
    single_best: bool = True,
) -> list[DatasourceSummary]:
    """Match published datasources by id and/or name (case-insensitive)."""
    by_id = {d.id.casefold(): d for d in all_datasources}
    by_name: dict[str, list[DatasourceSummary]] = {}
    for d in all_datasources:
        by_name.setdefault(_normalize_label(d.name), []).append(d)

    matched: list[DatasourceSummary] = []
    seen: set[str] = set()

    def _add(ds: DatasourceSummary) -> None:
        if ds.id not in seen:
            seen.add(ds.id)
            matched.append(ds)

    for raw_id in ids or []:
        key = (raw_id or "").strip().casefold()
        if not key:
            continue
        ds = by_id.get(key)
        if ds:
            _add(ds)

    for raw_name in names or []:
        label = _normalize_label(raw_name)
        if not label:
            continue
        exact = by_name.get(label) or []
        if exact:
            _add(exact[0])
            continue
        if exact_name_only:
            continue
        # Score partial matches; take only the best for this name.
        scored: list[tuple[int, DatasourceSummary]] = []
        for d in all_datasources:
            dn = _normalize_label(d.name)
            if not dn:
                continue
            score = 0
            if dn == label:
                score = 100
            elif dn.startswith(label) or label.startswith(dn):
                score = 50
            elif label in dn or dn in label:
                score = 25
            if score > 0:
                scored.append((score, d))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], x[1].name.casefold()))
        _add(scored[0][1])

    if single_best and len(matched) > 1:
        return pick_primary_datasource(matched)
    return matched


async def resolve_datasources_via_mcp(
    *,
    names: list[str] | None = None,
    ids: list[str] | None = None,
    exact_name_only: bool = False,
    single_best: bool = True,
) -> list[DatasourceSummary]:
    if not (names or ids):
        return []
    try:
        all_ds = await list_datasources_via_mcp()
    except RuntimeError:
        all_ds = []
    matched = resolve_datasources_from_list(
        all_ds,
        names=names,
        ids=ids,
        exact_name_only=exact_name_only,
        single_best=single_best,
    )
    if matched:
        return matched

    # REST fallback when MCP list is empty / name not visible via MCP.
    rest_hits: list[DatasourceSummary] = []
    seen: set[str] = set()
    for raw_name in names or []:
        label = (raw_name or "").strip()
        if not label:
            continue
        ds = fetch_published_datasource_by_name(label)
        if ds and ds.id and ds.id not in seen:
            seen.add(ds.id)
            rest_hits.append(ds)
    if single_best and len(rest_hits) > 1:
        return pick_primary_datasource(rest_hits)
    return rest_hits


def fetch_published_datasource_by_name(name: str) -> DatasourceSummary | None:
    """REST lookup of a published datasource by exact name (site-scoped)."""
    label = (name or "").strip()
    if not label:
        return None
    token, site_id = sign_in_pat()
    if not site_id:
        return None
    version = _rest_version()
    url = f"{_server_base()}/api/{version}/sites/{site_id}/datasources"
    with httpx.Client(verify=httpx_verify(), timeout=120.0) as client:
        res = client.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Tableau-Auth": token,
            },
            params={"filter": f"name:eq:{label}"},
        )
    if not res.is_success:
        return None
    try:
        payload = res.json()
    except json.JSONDecodeError:
        return None
    parsed = _parse_rest_datasource_nodes(payload)
    for ds in parsed:
        if ds.id and _normalize_label(ds.name) == _normalize_label(label):
            return ds
    return parsed[0] if parsed and parsed[0].id else None


def _workbook_match_tokens(workbook_name: str | None, content_url: str | None) -> set[str]:
    tokens: set[str] = set()

    def _add_raw(raw: str) -> None:
        compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
        if compact and len(compact) >= 4 and compact not in _GENERIC_NAME_TOKENS:
            tokens.add(compact)
        for part in re.split(r"[^a-z0-9]+", raw.lower()):
            if len(part) >= 4 and part not in _GENERIC_NAME_TOKENS:
                tokens.add(part)

    for raw in (workbook_name, content_url):
        if not raw:
            continue
        _add_raw(raw)
        _add_raw(raw.split("(")[0])
    for t in list(tokens):
        if "payable" in t or "accountspayable" in t:
            tokens.add("accountspayable")
    return {t for t in tokens if t and t not in _GENERIC_NAME_TOKENS and len(t) >= 4}


def _workbook_datasource_aliases(
    workbook_name: str | None, content_url: str | None
) -> list[str]:
    """Known published DS names when Metadata/workbook REST cannot discover upstreams."""
    blob = re.sub(
        r"[^a-z0-9]+",
        "",
        f"{workbook_name or ''}{content_url or ''}".lower(),
    )
    aliases: list[str] = []
    if "accountspayable" in blob:
        aliases.extend(["AP Dataset", "Accounts Payable", "AP Data"])
    # Sales_AI-MCP uses an embedded Hyper extract; site publishes "Sales Data".
    if "salesaimcp" in blob or ("salesai" in blob and "mcp" in blob):
        aliases.extend(["Sales Data", "Sales Data Extract", "Sales Pipeline"])
    return aliases


def _resolve_workbook_ds_via_aliases(
    workbook_name: str | None, content_url: str | None
) -> list[DatasourceSummary]:
    for alias in _workbook_datasource_aliases(workbook_name, content_url):
        ds = fetch_published_datasource_by_name(alias)
        if ds and ds.id:
            return [ds]
    return []


def _score_workbook_ds_name(
    ds_name: str,
    tokens: set[str],
    *,
    workbook_name: str | None = None,
    content_url: str | None = None,
) -> int:
    dn = re.sub(r"[^a-z0-9]+", "", (ds_name or "").lower())
    if not dn:
        return 0
    score = 0
    for token in tokens:
        if dn == token:
            score = max(score, 100)
        elif dn.startswith(token) or token.startswith(dn):
            if min(len(dn), len(token)) >= 6:
                score = max(score, 55)
        elif len(token) >= 8 and token in dn:
            score = max(score, 40)
    wb_blob = re.sub(
        r"[^a-z0-9]+",
        "",
        f"{workbook_name or ''}{content_url or ''}".lower(),
    )
    # Accounts Payable workbooks commonly use "AP Dataset" (tokens alone miss this).
    if ("accountspayable" in tokens or "accountspayable" in wb_blob) and (
        dn in {"apdataset", "apdata", "accountspayable", "accountspayables"}
        or ("payable" in dn and "dataset" in dn)
        or (dn.startswith("ap") and "dataset" in dn)
    ):
        score = max(score, 90)
    # Sales_AI-MCP → published "Sales Data" (not the embedded extract name).
    if ("salesaimcp" in wb_blob or "salesai" in wb_blob) and dn in {
        "salesdata",
        "salesdataextract",
        "salespipeline",
    }:
        score = max(score, 90 if dn == "salesdata" else 70)
    return score


async def resolve_workbook_datasources(
    workbook_id: str,
    workbook_name: str | None = None,
    content_url: str | None = None,
) -> list[DatasourceSummary]:
    """Resolve the primary published datasource for a workbook (at most one)."""
    wid = (workbook_id or "").strip()
    matched: list[DatasourceSummary] = []

    if wid:
        # Prefer REST (works when Metadata GraphQL is 403).
        rest = fetch_workbook_datasources_rest(wid)
        published_rest = [d for d in rest if d.id]
        if published_rest:
            return pick_primary_datasource(published_rest)
        matched.extend(rest)

        meta = fetch_workbook_published_datasources(wid)
        published_meta = [d for d in meta if d.id]
        if published_meta:
            return pick_primary_datasource(published_meta)
        matched.extend(meta)

    try:
        all_ds = await list_datasources_via_mcp()
    except RuntimeError:
        all_ds = []

    if not all_ds:
        aliased = _resolve_workbook_ds_via_aliases(workbook_name, content_url)
        if aliased:
            return aliased
        return pick_primary_datasource([d for d in matched if d.id] or matched)

    # Match metadata/REST embedded names to published LUIDs (exact preferred).
    if matched:
        names = [d.name for d in matched if d.name]
        resolved = resolve_datasources_from_list(
            all_ds, names=names, exact_name_only=False, single_best=True
        )
        if resolved:
            return pick_primary_datasource(resolved)

    tokens = _workbook_match_tokens(workbook_name, content_url)
    hits: list[tuple[int, DatasourceSummary]] = []
    for d in all_ds:
        score = _score_workbook_ds_name(
            d.name, tokens, workbook_name=workbook_name, content_url=content_url
        )
        if score >= 55:
            hits.append((score, d))

    if hits:
        hits.sort(key=lambda x: (-x[0], x[1].name.casefold()))
        return [hits[0][1]]

    aliased = _resolve_workbook_ds_via_aliases(workbook_name, content_url)
    if aliased:
        return aliased

    return pick_primary_datasource([d for d in matched if d.id] or matched)


_WORKBOOK_DS_QUERY = """
query WorkbookDatasources($luid: String!) {
  workbooks(filter: { luid: $luid }) {
    name
    luid
    embeddedDatasources {
      name
      upstreamDatasources {
        name
        luid
      }
    }
  }
}
"""


def _rest_version() -> str:
    return env("TABLEAU_REST_API_VERSION") or DEFAULT_REST_VERSION


def _parse_rest_datasource_nodes(payload: Any) -> list[DatasourceSummary]:
    """Normalize Tableau REST workbook datasources / connections JSON."""
    out: list[DatasourceSummary] = []
    seen: set[str] = set()

    def _add(did: str, name: str, *, published: bool | None = True) -> None:
        key = did or f"name:{name}"
        if not name or key in seen:
            return
        seen.add(key)
        out.append(DatasourceSummary(id=did, name=name, is_published=published))

    if not isinstance(payload, dict):
        return out

    datasources = payload.get("datasources")
    if isinstance(datasources, dict):
        nodes = datasources.get("datasource")
        if isinstance(nodes, dict):
            nodes = [nodes]
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            did = node.get("id") or node.get("luid") or ""
            name = node.get("name") or ""
            if isinstance(did, str) and isinstance(name, str) and name:
                _add(did, name, published=True)

    connections = payload.get("connections")
    if isinstance(connections, dict):
        nodes = connections.get("connection")
        if isinstance(nodes, dict):
            nodes = [nodes]
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            # Prefer nested datasource object when present.
            ds = node.get("datasource") if isinstance(node.get("datasource"), dict) else None
            if ds:
                did = ds.get("id") or ds.get("luid") or ""
                name = ds.get("name") or node.get("datasourceName") or ""
            else:
                did = node.get("datasourceId") or node.get("id") or ""
                name = node.get("datasourceName") or node.get("name") or ""
            if isinstance(name, str) and name:
                _add(did if isinstance(did, str) else "", name, published=bool(did))

    return out


def fetch_workbook_datasources_rest(workbook_luid: str) -> list[DatasourceSummary]:
    """REST: published datasources attached to a workbook (no Metadata GraphQL)."""
    wid = (workbook_luid or "").strip()
    if not wid:
        return []

    token, site_id = sign_in_pat()
    if not site_id:
        return []
    version = _rest_version()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Tableau-Auth": token,
    }
    base = f"{_server_base()}/api/{version}/sites/{site_id}/workbooks/{wid}"

    with httpx.Client(verify=httpx_verify(), timeout=120.0) as client:
        for path in ("datasources", "connections"):
            res = client.get(f"{base}/{path}", headers=headers)
            if not res.is_success:
                continue
            try:
                payload = res.json()
            except json.JSONDecodeError:
                continue
            parsed = _parse_rest_datasource_nodes(payload)
            published = [d for d in parsed if d.id]
            if published:
                return published
            if parsed:
                return parsed
    return []


def fetch_workbook_published_datasources(workbook_luid: str) -> list[DatasourceSummary]:
    """Metadata API: published upstream datasources used by a workbook."""
    wid = (workbook_luid or "").strip()
    if not wid:
        return []

    token, _site_id = sign_in_pat()
    url = f"{_server_base()}/api/metadata/graphql"
    with httpx.Client(verify=httpx_verify(), timeout=120.0) as client:
        res = client.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Tableau-Auth": token,
            },
            json={"query": _WORKBOOK_DS_QUERY, "variables": {"luid": wid}},
        )
    if not res.is_success:
        return []
    try:
        payload = res.json()
    except json.JSONDecodeError:
        return []

    workbooks = (payload.get("data") or {}).get("workbooks") or []
    out: list[DatasourceSummary] = []
    seen: set[str] = set()
    for wb in workbooks:
        if not isinstance(wb, dict):
            continue
        for eds in wb.get("embeddedDatasources") or []:
            if not isinstance(eds, dict):
                continue
            upstream = eds.get("upstreamDatasources") or []
            if not upstream:
                name = eds.get("name")
                if isinstance(name, str) and name and f"embedded:{name}" not in seen:
                    seen.add(f"embedded:{name}")
                    out.append(DatasourceSummary(id="", name=name, is_published=False))
                continue
            for up in upstream:
                if not isinstance(up, dict):
                    continue
                luid = up.get("luid")
                name = up.get("name")
                if isinstance(luid, str) and luid and isinstance(name, str) and name and luid not in seen:
                    seen.add(luid)
                    out.append(DatasourceSummary(id=luid, name=name, is_published=True))
    return out
