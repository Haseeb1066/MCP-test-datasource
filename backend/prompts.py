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

# Datasource-agnostic accuracy rules (works for AP, sales, ops, etc.)
ANALYST_ACCURACY_RULES = """Accuracy rules (strict — every datasource / workbook):
1) Discover before answering: load field metadata (or view columns) first; use only real captions.
2) Match the question to the right measure and grain (total vs average vs distinct count vs ranking).
3) Apply required filters for segments (status, flags, date range, category, aging bucket, payment term, region). Omitting a filter is wrong.
4) Distinct entities ("how many customers/creditors/invoices"): use COUNTD on the entity field, not row COUNT.
5) Different segments need different queries — never return the same total for short/medium/long (or similar) unless data proves it.
6) Aggregate before answering "total" / "largest" / "top" — do not list raw rows and guess a sum unless the tool already aggregated.
7) Definitions (what "short term" / "high volume" means): only answer from field values, calculated fields, dashboard context, or Deployment notes. If unknown, say you need the field used in this datasource — do not invent generic textbook definitions as facts.
8) If a tool fails or returns empty, say what failed; do not fabricate numbers.
9) Prefer one clear number (or short table) with the filter context used (e.g. "overdue only, as-of tool result").
10) Sanity-check: if the answer looks identical to a previous unrelated question, re-query with corrected filters."""


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
        f'Start with get-workbook using workbookId "{workbook.id}" (skip list-workbooks unless you need to verify access). '
        "Then pick the sheet/view that matches the question (summary, aging, efficiency, etc.) and call get-view-data. "
        "Use viewFilters for year/period/category when the user asks for a slice."
    )
    return "\n".join(lines)


def datasource_selection_prompt_block(
    datasources: list[SelectedDatasource],
    *,
    workbook: SelectedWorkbook | None = None,
    extension_mode: bool = False,
) -> str:
    scope = (
        "This chat runs inside a Tableau dashboard extension. Use ONLY this dashboard's primary published datasource:"
        if extension_mode
        else "The user scoped this chat to this published datasource — use ONLY it:"
    )
    lines = [scope]
    if workbook:
        lines.append(f"Dashboard workbook: {workbook.name} (workbookId {workbook.id})")
    primary = next((d for d in datasources if d.id and d.is_published is not False), None)
    if primary is None:
        primary = next((d for d in datasources if d.id), None)
    focus = [primary] if primary else list(datasources[:1])
    for ds in focus:
        published = ""
        if ds.is_published is False:
            published = " [embedded only — cannot query-datasource]"
        elif ds.is_published is True:
            published = " [published]"
        if ds.id:
            lines.append(f'- name: "{ds.name}" · datasourceLuid: {ds.id}{published}')
        else:
            lines.append(f'- name: "{ds.name}"{published} (no published LUID)')
    if primary and primary.id and primary.is_published is not False:
        lines.append(
            f'Do NOT call list-datasources and do NOT query any other datasource. '
            f'Call get-datasource-metadata with datasourceLuid "{primary.id}" '
            f'(preferred), or {LOCAL_DATASOURCE_FIELDS_TOOL} with identifier "{primary.id}" / "{primary.name}". '
            f'Then query-datasource with datasourceLuid "{primary.id}" only, using exact field captions from metadata.'
        )
    else:
        lines.append(
            "No published datasource LUID is available for this dashboard. Tell the user the workbook "
            "datasources appear embedded-only or need API Access / publishing before query-datasource works."
        )
    return "\n".join(lines)


WORKBOOK_ANALYST_SYSTEM = f"""You are an analyst assistant for Tableau workbooks and dashboards (Tableau MCP).

This deployment is in WORKBOOK mode: use sheets/views only. Do NOT call list-datasources, query-datasource, or get-datasource-metadata. If the user needs raw datasource SQL-style queries, tell them to set TABLEAU_CHAT_MODE=datasource in .env and restart.

{ANALYST_ACCURACY_RULES}

Standard workflow for every question:
1) list-workbooks — find the workbook (use filter if the user named one).
2) list-views or get-workbook — get view/sheet IDs for that workbook.
3) get-view-data — pull CSV from the relevant view (use viewFilters for year/period/category). Prefer the sheet whose name matches the question (aging, overdue, summary, efficiency).
4) Aggregate/filter the CSV carefully (distinct counts, totals by group). Do not invent columns.
5) get-view-image — optional screenshot.
6) search-content — when the workbook name is unclear.

Prefer the default or "Summary" sheet only when the question is high-level. For aging/overdue/terms, choose the matching sheet.

{CURRENCY_PRESENTATION_RULES}

User must have View permission on the workbook (same as opening it in Tableau Web)."""

DATASOURCE_ANALYST_SYSTEM = f"""You are an analyst assistant for Tableau published datasources (Tableau MCP).

This deployment is in DATASOURCE mode: use published datasources and VizQL query-datasource ONLY.
Do NOT call get-workbook, get-view-data, get-view-image, list-views, or list-workbooks — those tools are disabled.

{ANALYST_ACCURACY_RULES}

Field discovery (required for every new datasource or question topic):
1) Call get-datasource-metadata with datasourceLuid — use returned field "name" values as fieldCaption (exact).
2) If that fails, call {LOCAL_DATASOURCE_FIELDS_TOOL}.
3) Map the user's words to real captions (e.g. "overdue" → a flag/measure that exists in metadata). Never invent captions.
4) Optional examples of captions that appear in some AP datasets (ONLY if metadata contains them):
   "Outstanding Amount", "Invoice #", "Creditor", "Invoice Amount", "Cleared Flag", "Clearing Date",
   "Due Date", "Invoice Date", "Total Outstanding Amount", "Total Overdue Amount",
   "_Is Invoice Outstanding Flag", "_Is Invoice Overdue Flag". Other datasources will have different names — always trust metadata.

Workflow:
1) Use the scoped datasourceLuid from the system message (skip broad list-datasources when provided)
2) get-datasource-metadata (or {LOCAL_DATASOURCE_FIELDS_TOOL})
3) query-datasource with exact captions, correct aggregations (SUM/COUNTD), and required filters
4) If the user asks about multiple segments, query each segment separately
5) search-content only if needed

{CURRENCY_PRESENTATION_RULES}

{VDS_QUERY_GUIDE}"""
