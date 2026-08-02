"""Per-datasource business glossaries injected into the chat system prompt.

Field captions still come from get-datasource-metadata at query time.
This file only holds org KPI definitions that metadata cannot invent
(e.g. short/medium/long payment terms).

Default file: data/glossaries.json
Override path: CHAT_GLOSSARIES_PATH
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from backend.config import env
from backend.datasources import SelectedDatasource

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _ROOT / "data" / "glossaries.json"
_lock = threading.Lock()
_cache_mtime: float | None = None
_cache_entries: list[dict[str, Any]] = []


def glossaries_path() -> Path:
    raw = (env("CHAT_GLOSSARIES_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _normalize_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    notes = raw.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        return None
    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    luids = [
        str(x).strip().lower()
        for x in (match.get("luids") or [])
        if isinstance(x, str) and x.strip()
    ]
    names = [
        str(x).strip().lower()
        for x in (match.get("names") or [])
        if isinstance(x, str) and x.strip()
    ]
    contains = [
        str(x).strip().lower()
        for x in (match.get("nameContains") or [])
        if isinstance(x, str) and x.strip()
    ]
    if not luids and not names and not contains:
        return None
    return {
        "id": str(raw.get("id") or "").strip() or None,
        "label": str(raw.get("label") or raw.get("id") or "glossary").strip(),
        "luids": luids,
        "names": names,
        "nameContains": contains,
        "notes": notes.strip(),
    }


def _parse_file(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_entry(item)
        if normalized:
            out.append(normalized)
    return out


def load_glossary_entries(*, force_reload: bool = False) -> list[dict[str, Any]]:
    """Load and cache glossary entries from disk (reload when file mtime changes)."""
    global _cache_mtime, _cache_entries
    path = glossaries_path()
    with _lock:
        if not path.is_file():
            _cache_mtime = None
            _cache_entries = []
            return []
        try:
            mtime = path.stat().st_mtime
        except OSError:
            _cache_mtime = None
            _cache_entries = []
            return []
        if not force_reload and _cache_mtime == mtime and _cache_entries is not None:
            return list(_cache_entries)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache_mtime = mtime
            _cache_entries = []
            return []
        entries = _parse_file(payload)
        _cache_mtime = mtime
        _cache_entries = entries
        return list(entries)


def entry_matches_datasource(entry: dict[str, Any], ds: SelectedDatasource) -> bool:
    luid = (ds.id or "").strip().lower()
    name = (ds.name or "").strip().lower()
    if luid and luid in (entry.get("luids") or []):
        return True
    if name and name in (entry.get("names") or []):
        return True
    if name:
        for needle in entry.get("nameContains") or []:
            if needle and needle in name:
                return True
    return False


def match_glossaries_for_datasources(
    datasources: list[SelectedDatasource] | None,
) -> list[dict[str, Any]]:
    if not datasources:
        return []
    entries = load_glossary_entries()
    if not entries:
        return []
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ds in datasources:
        for entry in entries:
            key = entry.get("id") or entry.get("notes") or ""
            if key in seen:
                continue
            if entry_matches_datasource(entry, ds):
                seen.add(str(key))
                matched.append(entry)
    return matched


def format_glossary_prompt_block(
    datasources: list[SelectedDatasource] | None,
) -> str | None:
    """Return prompt text for matched glossaries, or None if nothing matches."""
    matched = match_glossaries_for_datasources(datasources)
    if not matched:
        return None
    parts = [
        "Datasource glossary (org definitions — use with metadata field captions; "
        "do not invent other definitions):"
    ]
    for entry in matched:
        label = entry.get("label") or entry.get("id") or "glossary"
        parts.append(f"### {label}\n{entry['notes']}")
    return "\n\n".join(parts)
