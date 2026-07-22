from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from backend.chat_mode import WORKBOOK_MODE_EXCLUDE_GROUPS, is_workbook_mode
from backend.config import env, require_env
from backend.mcp_stdio import McpStdioClient
from backend.platform_fix import mcp_spawn_command
from backend.redact import redact_tableau_secrets

_client: McpStdioClient | None = None
_mcp_env_fp: tuple[str, ...] | None = None
_init_lock = asyncio.Lock()

_MCP_TABLEAU_KEYS = ("SERVER", "SITE_NAME", "PAT_NAME", "PAT_VALUE", "EXCLUDE_TOOLS", "INCLUDE_TOOLS")


def _build_mcp_env(*, force_datasource_tools: bool = False) -> dict[str, str]:
    mcp_env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    mcp_env["SERVER"] = require_env("TABLEAU_SERVER")
    # Tableau MCP reads SITE_NAME; empty string = default site.
    mcp_env["SITE_NAME"] = env("TABLEAU_SITE_NAME")
    mcp_env["PAT_NAME"] = require_env("TABLEAU_PAT_NAME")
    mcp_env["PAT_VALUE"] = require_env("TABLEAU_PAT_VALUE")
    if env("NODE_TLS_REJECT_UNAUTHORIZED") == "0":
        mcp_env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    include_tools = env("INCLUDE_TOOLS")
    enable_metadata = env("ENABLE_DATASOURCE_METADATA_TOOL") == "1"
    workbook_only = is_workbook_mode() and not force_datasource_tools

    if workbook_only and not include_tools:
        parts = [p.strip() for p in env("EXCLUDE_TOOLS").split(",") if p.strip()]
        for g in WORKBOOK_MODE_EXCLUDE_GROUPS:
            if g not in parts:
                parts.append(g)
        mcp_env["EXCLUDE_TOOLS"] = ",".join(parts)
    elif not enable_metadata and not include_tools:
        meta = "get-datasource-metadata"
        raw = env("EXCLUDE_TOOLS")
        if raw:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if meta not in parts:
                mcp_env["EXCLUDE_TOOLS"] = ",".join([*parts, meta])
        else:
            mcp_env["EXCLUDE_TOOLS"] = meta

    return mcp_env


def _mcp_env_fingerprint(*, force_datasource_tools: bool = False) -> tuple[str, ...]:
    e = _build_mcp_env(force_datasource_tools=force_datasource_tools)
    return tuple(e.get(k, "") for k in _MCP_TABLEAU_KEYS)


def mcp_tableau_env_summary() -> dict[str, str]:
    e = _build_mcp_env()
    return {k: e.get(k, "") for k in ("SERVER", "SITE_NAME", "PAT_NAME", "PAT_VALUE")}


async def reset_mcp_client() -> None:
    global _client, _mcp_env_fp
    async with _init_lock:
        if _client is not None:
            await _client.close()
        _client = None
        _mcp_env_fp = None


async def get_mcp_client(*, force_datasource_tools: bool = False) -> McpStdioClient:
    global _client, _mcp_env_fp
    fp = _mcp_env_fingerprint(force_datasource_tools=force_datasource_tools)
    async with _init_lock:
        if _client is not None and _mcp_env_fp != fp:
            await _client.close()
            _client = None
        if _client is not None:
            return _client
        c = McpStdioClient(
            mcp_spawn_command(["npx", "-y", "@tableau/mcp-server@latest"]),
            _build_mcp_env(force_datasource_tools=force_datasource_tools),
        )
        await c.start()
        _client = c
        _mcp_env_fp = fp
        return c


def tool_result_to_text(result: dict[str, Any]) -> str:
    if result.get("structuredContent"):
        sc = result["structuredContent"]
        if isinstance(sc, dict) and sc:
            return redact_tableau_secrets(json.dumps(sc))

    lines: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            lines.append(json.dumps(block))
            continue
        if block.get("type") == "text" and block.get("text") is not None:
            lines.append(str(block["text"]))
        elif block.get("type") == "image":
            lines.append(
                f"[image {block.get('mimeType', '')}, base64 length {len(block.get('data') or '')}]"
            )
        elif block.get("type") == "resource":
            r = block.get("resource") or {}
            if isinstance(r, dict) and r.get("text"):
                lines.append(str(r["text"]))
            else:
                lines.append(f"[resource blob {r.get('mimeType', '') if isinstance(r, dict) else ''}]")
        else:
            lines.append(json.dumps(block))

    text = "\n\n".join(lines) if lines else "(empty tool result)"
    if result.get("isError"):
        return redact_tableau_secrets(json.dumps({"isError": True, "content": text}))
    return redact_tableau_secrets(text)


async def list_tools(*, force_datasource_tools: bool = False) -> list[dict[str, Any]]:
    client = await get_mcp_client(force_datasource_tools=force_datasource_tools)
    return await client.list_tools()


def _tool_result_is_auth_error(result: dict[str, Any]) -> bool:
    if not result.get("isError"):
        return False
    text = tool_result_to_text(result).lower()
    return "401" in text or ("invalid" in text and "token" in text)


async def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    force_datasource_tools: bool = False,
) -> dict[str, Any]:
    global _client, _mcp_env_fp
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            client = await get_mcp_client(force_datasource_tools=force_datasource_tools)
            result = await client.call_tool(name, arguments)
            if attempt == 0 and _tool_result_is_auth_error(result):
                await reset_mcp_client()
                continue
            return result
        except Exception as e:
            last_error = e
            _client = None
            _mcp_env_fp = None
            if attempt == 1:
                raise
    if last_error:
        raise last_error
    raise RuntimeError("call_tool failed without a result")


# Alias used by workbooks module
get_mcp_session = get_mcp_client
