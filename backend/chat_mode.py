from __future__ import annotations

from typing import Literal

from backend.config import env

TableauChatMode = Literal["workbook", "datasource"]

LOCAL_DATASOURCE_FIELDS_TOOL = "list-published-datasource-fields"

WORKBOOK_MODE_EXCLUDE_GROUPS = ("datasource",)

# When answering via published datasources, hide sheet/view tools so the model cannot fall back to get-view-data.
DATASOURCE_MODE_EXCLUDE_TOOLS = (
    "get-workbook",
    "get-view-data",
    "get-view-image",
    "get-view",
    "list-views",
    "list-workbooks",
    "list-custom-views",
    "get-custom-view-data",
    "get-custom-view-image",
)


def get_tableau_chat_mode() -> TableauChatMode:
    raw = (env("TABLEAU_CHAT_MODE") or "workbook").lower()
    return "datasource" if raw == "datasource" else "workbook"


def is_workbook_mode() -> bool:
    return get_tableau_chat_mode() == "workbook"


def uses_datasource_tools(*, has_selected_datasources: bool = False) -> bool:
    """Datasource MCP tools when in datasource mode, or when the extension scoped datasources."""
    if has_selected_datasources:
        return True
    return not is_workbook_mode()
