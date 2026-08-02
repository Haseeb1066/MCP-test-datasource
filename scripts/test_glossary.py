"""Unit checks for per-datasource glossary matching (no Tableau needed)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Allow running as: python scripts/test_glossary.py from repo root
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.datasources import DatasourceSummary
from backend.glossary import (
    entry_matches_datasource,
    format_glossary_prompt_block,
    load_glossary_entries,
    match_glossaries_for_datasources,
)


def _write_glossaries(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "id": "ap",
                        "label": "AP",
                        "match": {
                            "luids": ["aaa-bbb"],
                            "names": ["exact ap"],
                            "nameContains": ["accounts payable"],
                        },
                        "notes": "AP notes here",
                    },
                    {
                        "id": "sales",
                        "label": "Sales",
                        "match": {"nameContains": ["sales"]},
                        "notes": "Sales notes here",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "glossaries.json"
        _write_glossaries(path)
        import os

        os.environ["CHAT_GLOSSARIES_PATH"] = str(path)
        # Clear module cache by forcing reload
        entries = load_glossary_entries(force_reload=True)
        assert len(entries) == 2, entries

        ap_by_luid = DatasourceSummary(id="AAA-BBB", name="Other")
        assert entry_matches_datasource(entries[0], ap_by_luid)

        ap_by_name = DatasourceSummary(id="", name="Exact AP")
        assert entry_matches_datasource(entries[0], ap_by_name)

        ap_by_contains = DatasourceSummary(id="x", name="My Accounts Payable DS")
        assert entry_matches_datasource(entries[0], ap_by_contains)

        sales = DatasourceSummary(id="y", name="Regional Sales")
        matched = match_glossaries_for_datasources([ap_by_contains, sales])
        assert {m["id"] for m in matched} == {"ap", "sales"}, matched

        block = format_glossary_prompt_block([ap_by_contains])
        assert block and "AP notes here" in block
        assert "Sales notes here" not in block

        assert format_glossary_prompt_block([DatasourceSummary(id="z", name="HR")]) is None

        # Wire check: system prompt includes glossary when scoped
        from backend.chat import _build_system_prompt

        prompt = _build_system_prompt(
            selected_datasources=[ap_by_contains],
            force_datasource_mode=True,
        )
        assert "AP notes here" in prompt
        assert "Datasource glossary" in prompt

    print("glossary tests ok")


if __name__ == "__main__":
    main()
