from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from backend.chat_mode import (
    LOCAL_DATASOURCE_FIELDS_TOOL,
    get_tableau_chat_mode,
    is_workbook_mode,
    uses_datasource_tools,
)
from backend.config import env
from backend.datasources import SelectedDatasource, fetch_fields_via_mcp_metadata
from backend.mcp_tableau import call_tool, list_tools, tool_result_to_text
from backend.prompts import (
    DATASOURCE_ANALYST_SYSTEM,
    WORKBOOK_ANALYST_SYSTEM,
    datasource_selection_prompt_block,
    workbook_selection_prompt_block,
)
from backend.redact import redact_tableau_secrets
from backend.tableau_fields import fetch_published_datasource_fields
from backend.workbooks import SelectedWorkbook

LOCAL_DATASOURCE_FIELDS_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": LOCAL_DATASOURCE_FIELDS_TOOL,
        "description": (
            "Lists columns/fields for a published datasource using Tableau Metadata GraphQL (same PAT as MCP). "
            "Use when get-datasource-metadata is missing or broken. Pass the datasource LUID from list-datasources, "
            "OR the exact published datasource name (e.g. CustomerChurn)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": (
                        "Published datasource LUID (UUID) from list-datasources, OR the exact published datasource name."
                    ),
                }
            },
            "required": ["identifier"],
        },
    },
}


@dataclass
class ToolStep:
    tool: str
    arguments: dict[str, Any]
    result_preview: str
    duration_ms: int
    is_error: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "resultPreview": self.result_preview,
            "durationMs": self.duration_ms,
            **({"isError": True} if self.is_error else {}),
        }


@dataclass
class TurnTiming:
    total_ms: int
    open_ai_ms: int
    tools_ms: int
    setup_ms: int
    slowest_tool: str | None = None
    slowest_tool_ms: int = 0

    def to_api_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "totalMs": self.total_ms,
            "openAiMs": self.open_ai_ms,
            "toolsMs": self.tools_ms,
            "setupMs": self.setup_ms,
        }
        if self.slowest_tool:
            out["slowest"] = {"tool": self.slowest_tool, "durationMs": self.slowest_tool_ms}
        return out


@dataclass
class AgentTurnResult:
    reply: str
    steps: list[ToolStep]
    timing: TurnTiming


def _build_system_prompt(
    selected_workbook: SelectedWorkbook | None = None,
    selected_datasources: list[SelectedDatasource] | None = None,
    *,
    extension_mode: bool = False,
    force_datasource_mode: bool = False,
) -> str:
    datasource_scope = bool(selected_datasources)
    use_ds = force_datasource_mode or uses_datasource_tools(has_selected_datasources=datasource_scope)
    base = DATASOURCE_ANALYST_SYSTEM if use_ds else WORKBOOK_ANALYST_SYSTEM
    mode_label = "datasource" if use_ds else get_tableau_chat_mode()
    parts = [base, f"Active mode: {mode_label}."]
    if datasource_scope:
        parts.append(
            datasource_selection_prompt_block(
                selected_datasources or [],
                workbook=selected_workbook,
                extension_mode=extension_mode,
            )
        )
    elif selected_workbook and is_workbook_mode():
        parts.append(
            workbook_selection_prompt_block(selected_workbook, extension_mode=extension_mode)
        )
    extra = env("CHAT_SYSTEM_EXTRA")
    if extra:
        parts.append(f"Deployment notes:\n{extra}")
    return "\n\n".join(parts)


def _schema_to_openai_parameters(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not schema or not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if out.get("type") != "object":
        out["type"] = "object"
    if "properties" not in out:
        out["properties"] = {}
    return out


def _preview_result(text: str, max_len: int = 4000) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return f"{t[:max_len]}\n\n… [truncated {len(t) - max_len} chars]"


def _looks_like_error(text: str) -> bool:
    lower = text[:500].lower()
    return (
        '"iserror":true' in lower
        or '"error":' in lower
        or "tool execution failed" in lower
        or "request failed" in lower
    )


async def mcp_tools_to_openai(*, force_datasource_tools: bool = False) -> list[dict[str, Any]]:
    tools = await list_tools(force_datasource_tools=force_datasource_tools)
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        desc = str(t.get("description") or "")
        ann = t.get("annotations")
        if isinstance(ann, dict) and ann.get("title") and not desc:
            desc = str(ann["title"])
        schema = t.get("inputSchema")
        if not isinstance(schema, dict):
            schema = None
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": desc[:8000],
                    "parameters": _schema_to_openai_parameters(schema),
                },
            }
        )
    return out


def merge_chat_tools(
    mcp_tools: list[dict[str, Any]],
    *,
    force_datasource_tools: bool = False,
) -> list[dict[str, Any]]:
    if not uses_datasource_tools(has_selected_datasources=force_datasource_tools):
        return mcp_tools
    if env("DISABLE_LOCAL_DATASOURCE_FIELD_TOOL") == "1":
        return mcp_tools
    return [*mcp_tools, LOCAL_DATASOURCE_FIELDS_DEF]


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    force_datasource_tools: bool = False,
) -> str:
    if name == LOCAL_DATASOURCE_FIELDS_TOOL:
        ident = str(args.get("identifier") or "").strip()
        if not ident:
            return json.dumps({"error": "identifier is required"})
        try:
            out = fetch_published_datasource_fields(ident)
            # Metadata GraphQL often 403 — fall back to MCP get-datasource-metadata.
            if (
                not out.get("matches")
                or out.get("graphqlErrors")
                or (out.get("matches") and all(len(m.get("fields") or []) == 0 for m in out["matches"]))
            ):
                # Prefer LUID when identifier looks like UUID; otherwise try MCP with LUID from empty match
                luid = ident
                if not (len(ident) == 36 and ident.count("-") == 4):
                    # name-only: still try MCP only if we already have a LUID elsewhere — skip
                    pass
                else:
                    mcp_out = await fetch_fields_via_mcp_metadata(luid)
                    if mcp_out.get("matches") and any(
                        (m.get("fields") or []) for m in mcp_out["matches"]
                    ):
                        return redact_tableau_secrets(json.dumps(mcp_out))
            return redact_tableau_secrets(json.dumps(out))
        except Exception as e:
            msg = str(e)
            # On Metadata 403/failures, try MCP metadata when identifier is a LUID
            if len(ident) == 36 and ident.count("-") == 4:
                try:
                    mcp_out = await fetch_fields_via_mcp_metadata(ident)
                    if mcp_out.get("matches"):
                        mcp_out["graphqlFallbackError"] = msg[:300]
                        return redact_tableau_secrets(json.dumps(mcp_out))
                except Exception as e2:
                    return json.dumps({"error": msg, "mcpFallbackError": str(e2)})
            return json.dumps({"error": msg})

    raw = await call_tool(name, args, force_datasource_tools=force_datasource_tools)
    return tool_result_to_text(raw)


def _build_turn_timing(
    turn_start: float,
    open_ai_ms: int,
    tools_ms: int,
    setup_ms: int,
    steps: list[ToolStep],
) -> TurnTiming:
    slowest_tool: str | None = None
    slowest_tool_ms = 0
    for s in steps:
        if s.duration_ms > slowest_tool_ms:
            slowest_tool_ms = s.duration_ms
            slowest_tool = s.tool
    return TurnTiming(
        total_ms=int((time.time() - turn_start) * 1000),
        open_ai_ms=open_ai_ms,
        tools_ms=tools_ms,
        setup_ms=setup_ms,
        slowest_tool=slowest_tool,
        slowest_tool_ms=slowest_tool_ms,
    )


async def run_agent_turn(
    openai_client: OpenAI,
    user_messages: list[dict[str, str]],
    selected_workbook: SelectedWorkbook | None = None,
    selected_datasources: list[SelectedDatasource] | None = None,
    *,
    extension_mode: bool = False,
    force_datasource_mode: bool = False,
) -> AgentTurnResult:
    turn_start = time.time()
    open_ai_ms = 0
    tools_ms = 0
    force_ds = force_datasource_mode or bool(selected_datasources)

    t_setup = time.time()
    tools = merge_chat_tools(
        await mcp_tools_to_openai(force_datasource_tools=force_ds),
        force_datasource_tools=force_ds,
    )
    setup_ms = int((time.time() - t_setup) * 1000)

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _build_system_prompt(
                selected_workbook,
                selected_datasources,
                extension_mode=extension_mode,
                force_datasource_mode=force_ds,
            ),
        },
        *user_messages,
    ]

    model = env("OPENAI_MODEL") or "gpt-4o-mini"
    max_steps = int(env("AGENT_MAX_STEPS") or "16")
    steps: list[ToolStep] = []

    for _ in range(max_steps):
        t_llm = time.time()
        completion = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        open_ai_ms += int((time.time() - t_llm) * 1000)
        choice = completion.choices[0]
        if not choice.message:
            raise RuntimeError("No message in completion")

        msg = choice.message
        msg_dict: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(msg_dict)

        if not msg.tool_calls:
            text = (msg.content or "").strip()
            timing = _build_turn_timing(turn_start, open_ai_ms, tools_ms, setup_ms, steps)
            return AgentTurnResult(reply=text or "(No text response)", steps=steps, timing=timing)

        for call in msg.tool_calls:
            if call.type != "function":
                continue
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            t0 = time.time()
            try:
                result_text = await _execute_tool(
                    name, args, force_datasource_tools=force_ds
                )
            except Exception as e:
                result_text = json.dumps({"error": str(e)})
            duration_ms = int((time.time() - t0) * 1000)
            tools_ms += duration_ms
            preview = _preview_result(result_text)
            steps.append(
                ToolStep(
                    tool=name,
                    arguments=args,
                    result_preview=preview,
                    duration_ms=duration_ms,
                    is_error=_looks_like_error(preview),
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result_text[:120_000],
                }
            )

    timing = _build_turn_timing(turn_start, open_ai_ms, tools_ms, setup_ms, steps)
    return AgentTurnResult(
        reply="Stopped after maximum tool steps. Try a narrower question.",
        steps=steps,
        timing=timing,
    )
