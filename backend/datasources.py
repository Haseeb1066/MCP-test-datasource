"""Published datasource listing / resolve helpers (Tableau MCP + optional Metadata)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import httpx_verify
from backend.mcp_tableau import call_tool, tool_result_to_text
from backend.tableau_fields import _server_base, sign_in_pat
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
        for key in ("datasources", "items", "publishedDatasources"):
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
    result = await call_tool("list-datasources", {})
    text = tool_result_to_text(result)
    if result.get("isError"):
        raise RuntimeError(f"list-datasources failed: {text[:800]}")
    datasources = parse_datasources_from_tool_text(text)
    if not datasources and text.strip():
        raise RuntimeError(f"list-datasources returned no parseable datasources. Preview: {text[:400]}")
    return datasources


def resolve_datasources_from_list(
    all_datasources: list[DatasourceSummary],
    *,
    names: list[str] | None = None,
    ids: list[str] | None = None,
) -> list[DatasourceSummary]:
    """Match published datasources by id and/or name (case-insensitive)."""
    by_id = {d.id.casefold(): d for d in all_datasources}
    by_name: dict[str, list[DatasourceSummary]] = {}
    for d in all_datasources:
        by_name.setdefault(_normalize_label(d.name), []).append(d)

    matched: list[DatasourceSummary] = []
    seen: set[str] = set()

    for raw_id in ids or []:
        key = (raw_id or "").strip().casefold()
        if not key:
            continue
        ds = by_id.get(key)
        if ds and ds.id not in seen:
            seen.add(ds.id)
            matched.append(ds)

    for raw_name in names or []:
        label = _normalize_label(raw_name)
        if not label:
            continue
        candidates = by_name.get(label) or []
        if not candidates:
            candidates = [
                d
                for d in all_datasources
                if label in _normalize_label(d.name) or _normalize_label(d.name) in label
            ]
        for ds in candidates:
            if ds.id not in seen:
                seen.add(ds.id)
                matched.append(ds)
                break

    return matched


async def resolve_datasources_via_mcp(
    *,
    names: list[str] | None = None,
    ids: list[str] | None = None,
) -> list[DatasourceSummary]:
    if not (names or ids):
        return []
    all_ds = await list_datasources_via_mcp()
    return resolve_datasources_from_list(all_ds, names=names, ids=ids)


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


def fetch_workbook_published_datasources(workbook_luid: str) -> list[DatasourceSummary]:
    """Metadata API fallback: published upstream datasources used by a workbook."""
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
