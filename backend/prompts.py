from __future__ import annotations

from backend.chat_mode import LOCAL_DATASOURCE_FIELDS_TOOL
from backend.datasources import SelectedDatasource
from backend.vds_query_guide import VDS_QUERY_GUIDE
from backend.workbooks import SelectedWorkbook

CURRENCY_PRESENTATION_RULES = """Currency presentation rules (strict):
- Preserve the currency exactly as shown in tool output/source data.
- If a value includes PKR (or Rs), present it as PKR/Rs.
- If a value includes $ (USD), present it as $/USD.
- If a value has no currency marker, do not add one.
- Never convert currencies unless the user explicitly asks for conversion and provides a rate/date."""


def workbook_selection_prompt_block(
    workbook: SelectedWorkbook,
    *,
    extension_mode: bool = False,
) -> str:
    scope = (
        "This chat runs inside a Tableau dashboard extension on this workbook — scope every answer to it unless they clearly ask about a different workbook:"
        if extension_mode
        else "The user selected this workbook in the UI — scope every answer to it unless they clearly ask about a different workbook:"
    )
    lines = [
        scope,
        f"- name: {workbook.name}",
        f"- workbookId: {workbook.id}",
    ]
    if workbook.content_url:
        lines.append(f"- contentUrl: {workbook.content_url}")
    if workbook.default_view_id:
        lines.append(f"- defaultViewId: {workbook.default_view_id}")
    if workbook.project_name:
        lines.append(f"- project: {workbook.project_name}")
    lines.append(
        f'Start with get-workbook using workbookId "{workbook.id}" (skip list-workbooks unless you need to verify access). Then use get-view-data on the relevant view.'
    )
    return "\n".join(lines)


def datasource_selection_prompt_block(
    datasources: list[SelectedDatasource],
    *,
    workbook: SelectedWorkbook | None = None,
    extension_mode: bool = False,
) -> str:
    scope = (
        "This chat runs inside a Tableau dashboard extension. Use ONLY the published datasources connected to this dashboard:"
        if extension_mode
        else "The user scoped this chat to these published datasources — use ONLY these:"
    )
    lines = [scope]
    if workbook:
        lines.append(f"Dashboard workbook: {workbook.name} (workbookId {workbook.id})")
    for ds in datasources:
        published = ""
        if ds.is_published is False:
            published = " [embedded only — cannot query-datasource]"
        elif ds.is_published is True:
            published = " [published]"
        if ds.id:
            lines.append(f'- name: "{ds.name}" · datasourceLuid: {ds.id}{published}')
        else:
            lines.append(f'- name: "{ds.name}"{published} (no published LUID)')
    published_with_id = [d for d in datasources if d.id and d.is_published is not False]
    if published_with_id:
        first = published_with_id[0]
        lines.append(
            f'Skip broad list-datasources. Call {LOCAL_DATASOURCE_FIELDS_TOOL} with identifier "{first.id}" '
            f'(or exact name "{first.name}"), then query-datasource with datasourceLuid "{first.id}". '
            "If multiple datasources are listed, pick the one that matches the user's question."
        )
    else:
        lines.append(
            "No published datasource LUID is available for this dashboard. Tell the user the workbook "
            "datasources appear embedded-only or need API Access / publishing before query-datasource works."
        )
    return "\n".join(lines)


WORKBOOK_ANALYST_SYSTEM = """You are an analyst assistant for Tableau workbooks and dashboards (Tableau MCP).

This deployment is in WORKBOOK mode: use sheets/views only. Do NOT call list-datasources, query-datasource, or get-datasource-metadata. If the user needs raw datasource SQL-style queries, tell them to set TABLEAU_CHAT_MODE=datasource in .env and restart.

Standard workflow for every question:
1) list-workbooks — find the workbook (use filter if the user named one, e.g. name:eq:Sales Analysis Dashboard (v1)).
2) list-views or get-workbook — get view/sheet IDs for that workbook.
3) get-view-data — pull CSV data from the relevant view (use viewFilters when the user asks for a year or filter).
4) get-view-image — optional screenshot of a sheet.
5) search-content — when the workbook name is unclear.

Prefer the default or "Summary" sheet when the user does not name a sheet. Explain results in clear prose with key numbers.

{CURRENCY_PRESENTATION_RULES}

User must have View permission on the workbook (same as opening it in Tableau Web)."""

DATASOURCE_ANALYST_SYSTEM = f"""You are an analyst assistant for Tableau published datasources (Tableau MCP).

This deployment is in DATASOURCE mode: use published datasources and VizQL query-datasource. For dashboard/sheet data, set TABLEAU_CHAT_MODE=workbook in .env.

This deployment usually does NOT expose get-datasource-metadata (VDS/Zod mismatch). Assume unavailable unless in your tool list.

You have "{LOCAL_DATASOURCE_FIELDS_TOOL}" (built-in): call with datasource LUID or exact published name before querying.

Workflow:
1) list-datasources (skip when the system message already gives datasourceLuid values)
2) {LOCAL_DATASOURCE_FIELDS_TOOL}
3) query-datasource (small limit first)
4) search-content if needed

{CURRENCY_PRESENTATION_RULES}

{VDS_QUERY_GUIDE}"""
