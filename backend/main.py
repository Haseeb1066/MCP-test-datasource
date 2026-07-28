"""FastAPI entry: Tableau MCP chat API + optional static UI."""

from __future__ import annotations

import backend.platform_fix  # noqa: F401 — Windows subprocess / event loop

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from openai import OpenAI
from pydantic import BaseModel

from backend.chat import run_agent_turn
from backend.chat_mode import get_tableau_chat_mode
from backend.config import env
from backend.datasources import (
    DatasourceSummary,
    fetch_workbook_published_datasources,
    resolve_datasources_via_mcp,
    resolve_workbook_datasources,
)
from backend.runner import run_exclusive
from backend.tableau_fields import (
    check_metadata_api_access,
    fetch_published_datasource_fields,
    probe_tableau_sign_in,
)
from backend.workbooks import (
    SelectedWorkbook,
    WorkbookSummary,
    list_workbooks_via_mcp,
    resolve_workbook_via_mcp,
)
from backend.mcp_tableau import get_mcp_client, mcp_tableau_env_summary

ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT / "dist" / "web"


class ChatMessage(BaseModel):
    role: str
    content: str


class SelectedWorkbookBody(BaseModel):
    id: str
    name: str
    contentUrl: Optional[str] = None
    projectName: Optional[str] = None
    defaultViewId: Optional[str] = None


class SelectedDatasourceBody(BaseModel):
    id: Optional[str] = None
    name: str
    projectName: Optional[str] = None
    isPublished: Optional[bool] = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    selectedWorkbook: Optional[SelectedWorkbookBody] = None
    selectedDatasources: Optional[list[SelectedDatasourceBody]] = None
    extensionMode: Optional[bool] = None


def _parse_workbook(body: Optional[SelectedWorkbookBody]) -> Optional[SelectedWorkbook]:
    if not body:
        return None
    wid = body.id.strip()
    name = body.name.strip()
    if not wid or not name:
        return None
    return WorkbookSummary(
        id=wid,
        name=name,
        content_url=body.contentUrl,
        project_name=body.projectName,
        default_view_id=body.defaultViewId,
    )


def _parse_datasources(
    bodies: Optional[list[SelectedDatasourceBody]],
) -> list[DatasourceSummary]:
    if not bodies:
        return []
    out: list[DatasourceSummary] = []
    seen: set[str] = set()
    for body in bodies:
        name = (body.name or "").strip()
        if not name:
            continue
        did = (body.id or "").strip()
        key = did or f"name:{name.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            DatasourceSummary(
                id=did,
                name=name,
                project_name=body.projectName,
                is_published=body.isPublished,
            )
        )
    return out


app = FastAPI(title="Tableau MCP Chat", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    has_keys = bool(
        env("OPENAI_API_KEY")
        and env("TABLEAU_SERVER")
        and env("TABLEAU_PAT_NAME")
        and env("TABLEAU_PAT_VALUE")
    )
    tableau = probe_tableau_sign_in() if has_keys else {"tableauSignInOk": False, "tableauHint": "Set Tableau vars in .env"}
    ok = has_keys and bool(env("OPENAI_API_KEY")) and tableau.get("tableauSignInOk") is True
    mcp_env = mcp_tableau_env_summary() if has_keys else {}
    return {
        "ok": ok,
        "hasOpenAi": bool(env("OPENAI_API_KEY")),
        "hasTableau": bool(env("TABLEAU_SERVER")),
        "chatMode": get_tableau_chat_mode(),
        "backend": "python",
        "mcpSiteName": mcp_env.get("SITE_NAME", ""),
        "mcpPatName": mcp_env.get("PAT_NAME", ""),
        **tableau,
    }


@app.get("/api/workbooks/resolve")
async def api_resolve_workbook(
    id: Optional[str] = Query(None, alias="workbookId"),
    name: Optional[str] = Query(None),
    contentUrl: Optional[str] = Query(None),
    projectName: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Resolve workbook LUID by id, name, project, or contentUrl (for Tableau dashboard extensions)."""
    if not any(
        [
            (id or "").strip(),
            (name or "").strip(),
            (contentUrl or "").strip(),
        ]
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide query parameter workbookId=, name=, or contentUrl=",
        )

    try:

        async def _run() -> dict[str, Any] | None:
            await get_mcp_client()
            wb = await resolve_workbook_via_mcp(
                workbook_id=id,
                name=name,
                content_url=contentUrl,
                project_name=projectName,
            )
            return wb.to_api_dict() if wb else None

        workbook = await run_exclusive(_run)
        if not workbook:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No workbook matched workbookId={id!r} name={name!r} projectName={projectName!r} "
                    f"contentUrl={contentUrl!r}."
                ),
            )
        return {"workbook": workbook}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/datasources/resolve")
async def api_resolve_datasources(
    names: Optional[str] = Query(None, description="Comma-separated datasource names"),
    workbookId: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Resolve published datasource LUIDs by name and/or workbook Metadata upstreams."""
    name_list = [n.strip() for n in (names or "").split(",") if n.strip()]
    wid = (workbookId or "").strip()
    if not name_list and not wid:
        raise HTTPException(
            status_code=400,
            detail="Provide query parameter names= and/or workbookId=",
        )

    try:

        async def _run() -> list[dict[str, Any]]:
            await get_mcp_client(force_datasource_tools=True)
            matched: list[DatasourceSummary] = []

            if name_list:
                matched = await resolve_datasources_via_mcp(names=name_list)

            if wid and not matched:
                wb = await resolve_workbook_via_mcp(workbook_id=wid)
                matched = await resolve_workbook_datasources(
                    wid,
                    wb.name if wb else None,
                    wb.content_url if wb else None,
                )

            return [d.to_api_dict() for d in matched]

        datasources = await run_exclusive(_run)
        return {"datasources": datasources}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/workbooks")
async def api_workbooks() -> dict[str, Any]:
    import logging
    import traceback

    log = logging.getLogger("uvicorn.error")

    try:

        async def _run() -> list[dict[str, Any]]:
            await get_mcp_client()
            wbs = await list_workbooks_via_mcp()
            return [w.to_api_dict() for w in wbs]

        workbooks = await run_exclusive(_run)
        return {"workbooks": workbooks}
    except Exception as e:
        log.error("GET /api/workbooks failed:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/metadata-check")
def api_metadata_check() -> dict[str, Any]:
    try:
        return check_metadata_api_access()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/datasource-fields")
async def api_datasource_fields(
    luid: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
) -> dict[str, Any]:
    identifier = (luid or name or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Provide query parameter luid= or name=")
    try:
        out = fetch_published_datasource_fields(identifier)
        has_fields = bool(out.get("matches")) and any(
            (m.get("fields") or []) for m in out.get("matches") or []
        )
        if not has_fields and luid and len(luid.strip()) == 36:
            from backend.datasources import fetch_fields_via_mcp_metadata

            async def _run():
                await get_mcp_client(force_datasource_tools=True)
                return await fetch_fields_via_mcp_metadata(luid.strip())

            mcp_out = await run_exclusive(_run)
            if mcp_out.get("matches"):
                mcp_out["graphqlFallback"] = out.get("hint") or out.get("error") or "Metadata GraphQL unavailable"
                return mcp_out
        return out
    except Exception as e:
        # LUID path: try MCP metadata on GraphQL 403
        if luid and len(luid.strip()) == 36:
            try:
                from backend.datasources import fetch_fields_via_mcp_metadata

                async def _run():
                    await get_mcp_client(force_datasource_tools=True)
                    return await fetch_fields_via_mcp_metadata(luid.strip())

                mcp_out = await run_exclusive(_run)
                if mcp_out.get("matches"):
                    mcp_out["graphqlFallbackError"] = str(e)[:300]
                    return mcp_out
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/chat")
async def api_chat(body: ChatRequest) -> dict[str, Any]:
    if not body.messages:
        raise HTTPException(status_code=400, detail="Expected { messages: [{ role, content }] }")

    api_key = env("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not set in the server environment (.env).",
        )

    workbook = _parse_workbook(body.selectedWorkbook)
    datasources = _parse_datasources(body.selectedDatasources)
    normalized: list[dict[str, str]] = []
    for m in body.messages:
        if m.role not in ("user", "assistant"):
            raise HTTPException(status_code=400, detail=f"Unsupported role: {m.role}")
        normalized.append({"role": m.role, "content": m.content})

    openai_client = OpenAI(api_key=api_key)

    extension_mode = body.extensionMode is True

    async def _run():
        scoped = list(datasources)
        use_datasource_mode = extension_mode or bool(scoped)

        if scoped and any(not d.id for d in scoped):
            await get_mcp_client(force_datasource_tools=True)
            names = [d.name for d in scoped]
            resolved = await resolve_datasources_via_mcp(names=names)
            if resolved:
                scoped = resolved
            elif workbook and workbook.id:
                scoped = await resolve_workbook_datasources(
                    workbook.id, workbook.name, workbook.content_url
                )
        elif extension_mode and workbook and workbook.id:
            await get_mcp_client(force_datasource_tools=True)
            resolved = await resolve_workbook_datasources(
                workbook.id, workbook.name, workbook.content_url
            )
            if resolved:
                scoped = resolved

        # Extension / scoped datasource runs must not fall back to workbook view tools.
        force_ds = use_datasource_mode or bool(scoped)
        published = [d for d in scoped if d.id]
        if force_ds and not published:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No published datasource LUID resolved for this dashboard. "
                    "Enable API Access on the published datasource, confirm TABLEAU_SITE_NAME, "
                    "then retry GET /api/datasources/resolve?workbookId=..."
                ),
            )
        scoped = published or scoped

        return await run_agent_turn(
            openai_client,
            normalized,
            workbook,
            scoped or None,
            extension_mode=extension_mode,
            force_datasource_mode=force_ds,
        )

    try:
        result = await run_exclusive(_run)
        return {
            "reply": result.reply,
            "steps": [s.to_api_dict() for s in result.steps],
            "timing": result.timing.to_api_dict(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


DEBUG_LOG_PATH = Path(__file__).resolve().parents[1] / ".cursor" / "debug-766da3.log"


def _agent_log(
    location: str,
    message: str,
    data: dict[str, Any],
    hypothesis_id: str = "E",
) -> None:
    payload = {
        "sessionId": "766da3",
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def _index_script_hash() -> str | None:
    try:
        html = (WEB_DIST / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"/assets/index-([^.]+)\.js", html)
    return match.group(1) if match else None


def _index_build_id() -> str | None:
    try:
        html = (WEB_DIST / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'name="app-build-id" content="([^"]+)"', html)
    return match.group(1) if match else None


def _spa_html_response() -> FileResponse:
    response = FileResponse(WEB_DIST / "index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/version")
async def app_version():
    return {
        "buildId": _index_build_id(),
        "scriptHash": _index_script_hash(),
    }


# Production: serve built React app
if WEB_DIST.is_dir():
    assets = WEB_DIST / "assets"

    @app.get("/assets/{asset_path:path}")
    async def spa_assets(asset_path: str):
        file_path = assets / asset_path
        if not file_path.is_file():
            raise HTTPException(status_code=404)
        response = FileResponse(file_path)
        if re.search(r"index-[A-Za-z0-9_-]+\.(js|css)$", asset_path):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/")
    async def spa_index(request: Request):
        # #region agent log
        _agent_log(
            "main.py:spa_index",
            "Serving index.html",
            {
                "scriptHash": _index_script_hash(),
                "buildId": _index_build_id(),
                "referer": request.headers.get("referer"),
                "userAgent": (request.headers.get("user-agent") or "")[:120],
            },
            "E",
        )
        # #endregion
        return _spa_html_response()

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api"):
            raise HTTPException(status_code=404)
        file_path = WEB_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return _spa_html_response()
