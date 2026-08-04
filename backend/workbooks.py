from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from backend.mcp_stdio import McpStdioClient
from backend.mcp_tableau import call_tool, tool_result_to_text


@dataclass
class WorkbookSummary:
    id: str
    name: str
    content_url: str | None = None
    project_name: str | None = None
    default_view_id: str | None = None
    webpage_url: str | None = None

    def to_api_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.content_url:
            d["contentUrl"] = self.content_url
        if self.project_name:
            d["projectName"] = self.project_name
        if self.default_view_id:
            d["defaultViewId"] = self.default_view_id
        if self.webpage_url:
            d["webpageUrl"] = self.webpage_url
        return d


SelectedWorkbook = WorkbookSummary


def _parse_workbook_row(raw: Any) -> WorkbookSummary | None:
    if not isinstance(raw, dict):
        return None
    wid = raw.get("id")
    name = raw.get("name")
    if not isinstance(wid, str) or not wid or not isinstance(name, str) or not name:
        return None
    project = raw.get("project") if isinstance(raw.get("project"), dict) else None
    return WorkbookSummary(
        id=wid,
        name=name.strip(),
        content_url=raw.get("contentUrl").strip() if isinstance(raw.get("contentUrl"), str) else None,
        project_name=project.get("name") if project and isinstance(project.get("name"), str) else None,
        default_view_id=raw.get("defaultViewId") if isinstance(raw.get("defaultViewId"), str) else None,
        webpage_url=raw.get("webpageUrl") if isinstance(raw.get("webpageUrl"), str) else None,
    )


def _extract_json_payload(text: str) -> Any:  # used by datasources module too
    trimmed = text.strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        start = trimmed.find("[")
        end = trimmed.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(trimmed[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _rows_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("workbooks"), list):
            return payload["workbooks"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("items"), list):
            return payload["items"]
        if isinstance(payload.get("workbook"), dict):
            return [payload["workbook"]]
    return []


def parse_workbooks_from_tool_text(text: str) -> list[WorkbookSummary]:
    payload = _extract_json_payload(text)
    rows = _rows_from_payload(payload)
    out: list[WorkbookSummary] = []
    seen: set[str] = set()
    for row in rows:
        wb = _parse_workbook_row(row)
        if not wb or wb.id in seen:
            continue
        seen.add(wb.id)
        out.append(wb)
    out.sort(key=lambda w: w.name.casefold())
    return out


def _normalize_label(value: str | None) -> str:
    return (value or "").strip().casefold()


def _parse_dropdown_label(label: str) -> tuple[str | None, str | None]:
    """Parse dropdown labels like 'Sales (Sales)' or 'Sales(Sales)'."""
    trimmed = label.strip()
    if not trimmed:
        return None, None
    match = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", trimmed)
    if match:
        return match.group(1).strip() or None, match.group(2).strip() or None
    return trimmed, None


def _sanitize_query_value(value: str | None) -> str | None:
    """Trim user/query input; strip accidental trailing backslashes from URLs."""
    if value is None:
        return None
    cleaned = value.strip().rstrip("\\").strip()
    return cleaned or None


def resolve_workbook_from_list(
    workbooks: list[WorkbookSummary],
    *,
    workbook_id: str | None = None,
    name: str | None = None,
    content_url: str | None = None,
    project_name: str | None = None,
) -> WorkbookSummary | None:
    """Match a workbook by id, contentUrl, name+project, or name (case-insensitive, trims whitespace)."""
    wid = _sanitize_query_value(workbook_id)
    if wid:
        wid_fold = wid.casefold()
        for wb in workbooks:
            if wb.id.casefold() == wid_fold:
                return wb

    cu = _sanitize_query_value(content_url) or ""
    if cu:
        cu_fold = cu.casefold()
        for wb in workbooks:
            if wb.content_url and wb.content_url.casefold() == cu_fold:
                return wb
        for wb in workbooks:
            if _normalize_label(wb.name) == cu_fold:
                return wb

    wb_name, parsed_project = _parse_dropdown_label(_sanitize_query_value(name) or "")
    proj = _sanitize_query_value(project_name) or _sanitize_query_value(parsed_project)

    if not wb_name and not proj:
        return None

    def name_matches(wb: WorkbookSummary) -> bool:
        if not wb_name:
            return True
        return _normalize_label(wb.name) == _normalize_label(wb_name)

    def project_matches(wb: WorkbookSummary) -> bool:
        if not proj:
            return True
        return _normalize_label(wb.project_name) == _normalize_label(proj)

    if wb_name and proj:
        matches = [wb for wb in workbooks if name_matches(wb) and project_matches(wb)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return matches[0]

    if wb_name:
        exact = [wb for wb in workbooks if name_matches(wb)]
        if len(exact) == 1:
            return exact[0]
        if proj:
            return None
        if len(exact) > 1:
            return exact[0]
        nm_fold = _normalize_label(wb_name)
        partial = [wb for wb in workbooks if nm_fold in _normalize_label(wb.name)]
        if len(partial) == 1:
            return partial[0]

    return None


async def resolve_workbook_via_mcp(
    *,
    workbook_id: str | None = None,
    name: str | None = None,
    content_url: str | None = None,
    project_name: str | None = None,
    limit: int = 1000,
) -> WorkbookSummary | None:
    workbooks = await list_workbooks_via_mcp(limit=limit)
    return resolve_workbook_from_list(
        workbooks,
        workbook_id=workbook_id,
        name=name,
        content_url=content_url,
        project_name=project_name,
    )


async def list_workbooks_via_mcp(_session: McpStdioClient | None = None, limit: int = 1000) -> list[WorkbookSummary]:
    # Tableau MCP schema caps `limit` at 1000.
    safe_limit = max(1, min(int(limit), 1000))
    raw = await call_tool(
        "list-workbooks",
        {"limit": safe_limit, "pageSize": safe_limit},
    )
    text = tool_result_to_text(raw)
    if raw.get("isError") or '"isError": true' in text[:200].lower():
        hint = ""
        if "401" in text:
            hint = (
                " Tableau MCP returned 401 — PAT/site mismatch for MCP. "
                "Check TABLEAU_SITE_NAME (empty = default site) and TABLEAU_PAT_NAME (case-sensitive). "
                "Run: python scripts/test-pat.py then fully restart npm run dev."
            )
        raise RuntimeError(f"list-workbooks failed: {text[:800]}{hint}")
    workbooks = parse_workbooks_from_tool_text(text)
    if not workbooks and text.strip():
        raise RuntimeError(
            f"list-workbooks returned no parseable workbooks. Preview: {text[:400]}"
        )
    return workbooks
