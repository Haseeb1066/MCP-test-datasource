#!/usr/bin/env python3
"""
Run dashboard questions from an Excel file against /api/chat and score answers.

Example:
  npm run dev   # in another terminal
  python scripts/benchmark_questions.py \\
    --base-url http://127.0.0.1:8787 \\
    --content-url AccountsPayableAI-MCP \\
    --limit 5

Full 60-question run:
  python scripts/benchmark_questions.py \\
    --base-url https://mcp-test-ldxl.onrender.com \\
    --content-url AccountsPayableAI-MCP
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_XLSX = ROOT / "Accounts_Payable_Dashboard_60_Questions.xlsx"
RESULTS_DIR = ROOT / "benchmark-results"

WORKBOOK_VIEW_TOOLS = frozenset(
    {
        "get-workbook",
        "get-view-data",
        "get-view-image",
        "get-view",
        "list-views",
        "list-workbooks",
        "list-custom-views",
        "get-custom-view-data",
        "get-custom-view-image",
    }
)

DATASOURCE_QUERY_TOOLS = frozenset(
    {
        "query-datasource",
        "list-datasources",
        "list-published-datasource-fields",
        "get-datasource-metadata",
    }
)

FAILURE_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"^stopped after maximum tool steps",
        r"^\(no text response\)",
        r"set openai_api_key",
        r"set tableau pat",
        r"could not connect",
        r"waiting for workbook",
        r"connecting…",
        r"permission denied",
        r"401 unauthorized",
        r"list-datasources failed",
        r"query-datasource failed",
        r"tool execution failed",
    ]
]


@dataclass
class QuestionRow:
    number: int
    section: str
    question: str
    purpose: str


@dataclass
class QuestionResult:
    number: int
    section: str
    question: str
    purpose: str
    status: str
    answered: bool
    http_status: int
    duration_ms: int
    reply_preview: str
    tool_count: int
    tool_errors: int
    tools_used: list[str] = field(default_factory=list)
    used_datasource_query: bool = False
    used_workbook_view: bool = False
    error: str = ""
    reply: str = ""


@dataclass
class BenchmarkSummary:
    started_at: str
    finished_at: str
    base_url: str
    xlsx: str
    workbook: dict[str, Any] = field(default_factory=dict)
    datasources: list[dict[str, Any]] = field(default_factory=list)
    total_questions: int = 0
    answered: int = 0
    failed: int = 0
    answer_rate_pct: float = 0.0
    results: list[QuestionResult] = field(default_factory=list)


def _parse_xlsx(path: Path) -> list[QuestionRow]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in root.findall("m:si", ns):
                texts = [t.text or "" for t in si.findall(".//m:t", ns)]
                shared.append("".join(texts))

        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[list[str]] = []
        for row in sheet.findall("m:sheetData/m:row", ns):
            vals: list[str] = []
            for cell in row.findall("m:c", ns):
                cell_type = cell.get("t")
                value = cell.find("m:v", ns)
                if value is None:
                    vals.append("")
                elif cell_type == "s":
                    vals.append(shared[int(value.text)])
                else:
                    vals.append(value.text or "")
            rows.append(vals)

    if not rows:
        raise RuntimeError(f"No rows found in {path}")

    header = [c.strip().lower() for c in rows[0]]
    try:
        q_idx = next(i for i, h in enumerate(header) if "question" in h)
        no_idx = next(i for i, h in enumerate(header) if h.startswith("no"))
        section_idx = next(i for i, h in enumerate(header) if "section" in h)
        purpose_idx = next(i for i, h in enumerate(header) if "purpose" in h or "insight" in h)
    except StopIteration as e:
        raise RuntimeError(f"Unexpected Excel columns in {path}: {rows[0]}") from e

    out: list[QuestionRow] = []
    for raw in rows[1:]:
        if len(raw) <= q_idx:
            continue
        question = (raw[q_idx] or "").strip()
        if not question:
            continue
        try:
            number = int(float(raw[no_idx])) if raw[no_idx] else len(out) + 1
        except ValueError:
            number = len(out) + 1
        out.append(
            QuestionRow(
                number=number,
                section=(raw[section_idx] if len(raw) > section_idx else "").strip(),
                question=question,
                purpose=(raw[purpose_idx] if len(raw) > purpose_idx else "").strip(),
            )
        )
    return out


def _tool_names(steps: list[Any]) -> list[str]:
    names: list[str] = []
    for step in steps:
        if isinstance(step, dict) and step.get("tool"):
            names.append(str(step["tool"]))
    return names


def _evaluate_tools(tools_used: list[str], *, require_datasource: bool) -> tuple[bool, str]:
    used_workbook_view = any(t in WORKBOOK_VIEW_TOOLS for t in tools_used)
    used_datasource_query = "query-datasource" in tools_used
    if used_workbook_view:
        return False, f"Used workbook view tools: {', '.join(t for t in tools_used if t in WORKBOOK_VIEW_TOOLS)}"
    if require_datasource and not used_datasource_query:
        return False, f"No query-datasource call (tools: {', '.join(tools_used) or 'none'})"
    return True, ""


def _looks_failed(
    reply: str,
    tool_errors: int,
    http_status: int,
    tools_used: list[str],
    *,
    require_datasource: bool,
) -> tuple[bool, str]:
    if http_status != 200:
        return True, f"HTTP {http_status}"
    text = (reply or "").strip()
    if len(text) < 20:
        return True, "Reply too short"
    lower = text.lower()
    for pattern in FAILURE_PATTERNS:
        if pattern.search(lower):
            return True, f"Matched failure pattern: {pattern.pattern}"
    if tool_errors > 0 and len(text) < 80:
        return True, "Tool errors with very short reply"
    ok_tools, tool_reason = _evaluate_tools(tools_used, require_datasource=require_datasource)
    if not ok_tools:
        return True, tool_reason
    return False, ""


def _get_json(client: httpx.Client, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    res = client.get(url, params=params)
    try:
        data = res.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {url}: {res.text[:200]}") from None
    if not res.is_success:
        detail = data.get("detail") if isinstance(data, dict) else res.text
        raise RuntimeError(f"GET {url} failed ({res.status_code}): {detail}")
    return data


def _resolve_workbook(client: httpx.Client, base_url: str, args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, str] = {}
    if args.workbook_id:
        params["workbookId"] = args.workbook_id
    elif args.content_url:
        params["contentUrl"] = args.content_url
    elif args.workbook_name:
        params["name"] = args.workbook_name
        if args.project_name:
            params["projectName"] = args.project_name
    else:
        raise RuntimeError("Provide --workbook-id, --content-url, or --workbook-name")

    data = _get_json(client, f"{base_url}/api/workbooks/resolve", params=params)
    workbook = data.get("workbook")
    if not isinstance(workbook, dict) or not workbook.get("id"):
        raise RuntimeError(f"Workbook resolve returned no workbook: {data}")
    return workbook


def _resolve_datasources(
    client: httpx.Client, base_url: str, workbook: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"workbookId": workbook["id"]}
    if args.datasource_names:
        params["names"] = args.datasource_names
    try:
        data = _get_json(client, f"{base_url}/api/datasources/resolve", params=params)
        ds = data.get("datasources")
        return ds if isinstance(ds, list) else []
    except RuntimeError:
        return []


def _ask_question(
    client: httpx.Client,
    base_url: str,
    workbook: dict[str, Any],
    datasources: list[dict[str, Any]],
    question: str,
    timeout_s: float,
) -> tuple[int, dict[str, Any], int]:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": question}],
        "selectedWorkbook": workbook,
        "extensionMode": True,
    }
    if datasources:
        body["selectedDatasources"] = [
            {
                "id": d.get("id") or None,
                "name": d.get("name"),
                "projectName": d.get("projectName"),
                "isPublished": d.get("isPublished"),
            }
            for d in datasources
            if d.get("name")
        ]

    start = time.time()
    res = client.post(f"{base_url}/api/chat", json=body, timeout=timeout_s)
    duration_ms = int((time.time() - start) * 1000)
    try:
        data = res.json()
    except json.JSONDecodeError:
        data = {"error": res.text[:500]}
    return res.status_code, data, duration_ms


def run_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    xlsx_path = Path(args.xlsx).resolve()
    questions = _parse_xlsx(xlsx_path)
    if args.offset:
        questions = questions[args.offset :]
    if args.sample_size:
        pool = list(questions)
        if args.sample_size < len(pool):
            rng = random.Random(args.seed)
            questions = sorted(rng.sample(pool, args.sample_size), key=lambda q: q.number)
        else:
            questions = pool
    elif args.limit:
        questions = questions[: args.limit]

    base_url = args.base_url.rstrip("/")
    started = datetime.now(timezone.utc).isoformat()

    timeout = httpx.Timeout(args.timeout_s)
    with httpx.Client(timeout=timeout) as client:
        health = _get_json(client, f"{base_url}/api/health")
        if not health.get("ok"):
            hint = health.get("tableauHint") or health.get("healthError") or "Check /api/health"
            raise RuntimeError(f"Server not ready: {hint}")

        workbook = _resolve_workbook(client, base_url, args)
        datasources = _resolve_datasources(client, base_url, workbook, args)
        if args.require_datasource and not any(d.get("id") for d in datasources):
            raise RuntimeError(
                "No published datasource LUID resolved. Fix /api/datasources/resolve before running "
                "datasource-only benchmark."
            )

        results: list[QuestionResult] = []
        for idx, row in enumerate(questions, start=1):
            print(f"[{idx}/{len(questions)}] Q{row.number}: {row.question[:72]}…", flush=True)
            try:
                status_code, data, duration_ms = _ask_question(
                    client, base_url, workbook, datasources, row.question, args.timeout_s
                )
                reply = str(data.get("reply") or data.get("error") or "")
                steps = data.get("steps") if isinstance(data.get("steps"), list) else []
                tool_errors = sum(1 for s in steps if isinstance(s, dict) and s.get("isError"))
                tools_used = _tool_names(steps)
                used_workbook_view = any(t in WORKBOOK_VIEW_TOOLS for t in tools_used)
                used_datasource_query = "query-datasource" in tools_used
                failed, reason = _looks_failed(
                    reply,
                    tool_errors,
                    status_code,
                    tools_used,
                    require_datasource=args.require_datasource,
                )
                result = QuestionResult(
                    number=row.number,
                    section=row.section,
                    question=row.question,
                    purpose=row.purpose,
                    status="answered" if not failed else "failed",
                    answered=not failed,
                    http_status=status_code,
                    duration_ms=duration_ms,
                    reply_preview=reply[:280].replace("\n", " "),
                    tool_count=len(steps),
                    tool_errors=tool_errors,
                    tools_used=tools_used,
                    used_datasource_query=used_datasource_query,
                    used_workbook_view=used_workbook_view,
                    error=reason or str(data.get("error") or ""),
                    reply=reply if args.save_full_replies else "",
                )
            except Exception as e:
                result = QuestionResult(
                    number=row.number,
                    section=row.section,
                    question=row.question,
                    purpose=row.purpose,
                    status="failed",
                    answered=False,
                    http_status=0,
                    duration_ms=0,
                    reply_preview="",
                    tool_count=0,
                    tool_errors=0,
                    error=str(e),
                )
            results.append(result)
            mark = "OK" if result.answered else "FAIL"
            print(f"  -> {mark} ({result.duration_ms}ms)", flush=True)
            if args.delay_s:
                time.sleep(args.delay_s)

    answered = sum(1 for r in results if r.answered)
    total = len(results)
    summary = BenchmarkSummary(
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        base_url=base_url,
        xlsx=str(xlsx_path),
        workbook=workbook,
        datasources=datasources,
        total_questions=total,
        answered=answered,
        failed=total - answered,
        answer_rate_pct=round((answered / total * 100) if total else 0.0, 1),
        results=results,
    )
    return summary


def _write_outputs(summary: BenchmarkSummary, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"benchmark-{stamp}.json"
    csv_path = out_dir / f"benchmark-{stamp}.csv"

    payload = asdict(summary)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "number",
                "section",
                "question",
                "purpose",
                "status",
                "answered",
                "http_status",
                "duration_ms",
                "tool_count",
                "tool_errors",
                "tools_used",
                "used_datasource_query",
                "used_workbook_view",
                "error",
                "reply_preview",
            ],
        )
        writer.writeheader()
        for r in summary.results:
            writer.writerow(
                {
                    "number": r.number,
                    "section": r.section,
                    "question": r.question,
                    "purpose": r.purpose,
                    "status": r.status,
                    "answered": r.answered,
                    "http_status": r.http_status,
                    "duration_ms": r.duration_ms,
                    "tool_count": r.tool_count,
                    "tool_errors": r.tool_errors,
                    "tools_used": "|".join(r.tools_used),
                    "used_datasource_query": r.used_datasource_query,
                    "used_workbook_view": r.used_workbook_view,
                    "error": r.error,
                    "reply_preview": r.reply_preview,
                }
            )

    return json_path, csv_path


def _print_summary(summary: BenchmarkSummary) -> None:
    print()
    print("=" * 60)
    print("Benchmark summary")
    print("=" * 60)
    print(f"Workbook: {summary.workbook.get('name')} ({summary.workbook.get('id')})")
    print(f"Datasources scoped: {len(summary.datasources)}")
    if summary.datasources:
        for d in summary.datasources[:3]:
            print(f"  - {d.get('name')} ({d.get('id', 'no-id')})")
    ds_hits = sum(1 for r in summary.results if r.used_datasource_query)
    wb_hits = sum(1 for r in summary.results if r.used_workbook_view)
    print(f"Used query-datasource: {ds_hits}/{summary.total_questions}")
    print(f"Used workbook views:   {wb_hits}/{summary.total_questions}")
    print(f"Total questions: {summary.total_questions}")
    print(f"Answered:          {summary.answered}")
    print(f"Failed:            {summary.failed}")
    print(f"Answer rate:       {summary.answer_rate_pct}%")
    print()
    if summary.failed:
        print("Failed questions:")
        for r in summary.results:
            if not r.answered:
                print(f"  Q{r.number} [{r.section}]: {r.error or r.reply_preview[:80]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark dashboard questions from Excel.")
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX), help="Path to questions .xlsx")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8787",
        help="API base URL (use http://127.0.0.1:8787 for npm run dev)",
    )
    parser.add_argument("--content-url", default="AccountsPayableAI-MCP", help="Workbook contentUrl slug")
    parser.add_argument("--workbook-id", help="Workbook LUID (overrides content-url resolve)")
    parser.add_argument("--workbook-name", help="Workbook name resolve fallback")
    parser.add_argument("--project-name", help="Optional project name for workbook resolve")
    parser.add_argument(
        "--datasource-names",
        help="Comma-separated datasource names to scope (optional; server can infer from workbook)",
    )
    parser.add_argument("--limit", type=int, help="Run only the first N questions")
    parser.add_argument(
        "--sample-size",
        type=int,
        help="Randomly sample N questions (use with --seed for reproducible runs)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed when using --sample-size (default: 42)",
    )
    parser.add_argument("--offset", type=int, default=0, help="Skip first N questions")
    parser.add_argument("--timeout-s", type=float, default=300.0, help="Per-question HTTP timeout")
    parser.add_argument("--delay-s", type=float, default=0.0, help="Pause between questions")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR), help="Directory for CSV/JSON results")
    parser.add_argument(
        "--save-full-replies",
        action="store_true",
        help="Include full reply text in JSON output (can be large)",
    )
    parser.add_argument(
        "--require-datasource",
        action="store_true",
        default=True,
        help="Fail if query-datasource was not used or workbook view tools were used (default: on)",
    )
    parser.add_argument(
        "--allow-workbook-fallback",
        action="store_true",
        help="Do not require query-datasource (allows workbook view tools)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list questions from the Excel file",
    )
    args = parser.parse_args()
    if args.allow_workbook_fallback:
        args.require_datasource = False
    return args


def main() -> None:
    args = parse_args()
    xlsx_path = Path(args.xlsx).resolve()
    if not xlsx_path.is_file():
        print(f"Excel file not found: {xlsx_path}", file=sys.stderr)
        sys.exit(1)

    questions = _parse_xlsx(xlsx_path)
    if args.dry_run:
        print(f"Loaded {len(questions)} questions from {xlsx_path.name}")
        for q in questions:
            print(f"Q{q.number:02d} [{q.section}] {q.question}")
        sys.exit(0)

    summary = run_benchmark(args)
    json_path, csv_path = _write_outputs(summary, Path(args.output_dir))
    _print_summary(summary)
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")


if __name__ == "__main__":
    main()
