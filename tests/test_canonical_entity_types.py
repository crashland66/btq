from __future__ import annotations

import json
import re
from pathlib import Path

from btq_vault.entity_types import CANONICAL_ENTITY_TYPES


def test_canonical_entity_types_includes_prospect() -> None:
    assert "prospect" in CANONICAL_ENTITY_TYPES


def test_canonical_entity_types_python_set_matches_json_whitelist() -> None:
    design_doc_path = Path("project/event_pipeline/couchdb/design_btq_vault.json")
    design_doc = json.loads(design_doc_path.read_text(encoding="utf-8"))
    validate_doc_update = design_doc["validate_doc_update"]
    match = re.search(r"var canonicalTypes = \{\n(?P<body>.*?)\n  \};", validate_doc_update, re.DOTALL)

    assert match is not None
    json_types = set(re.findall(r'^\s+"([^"]+)": true,?$', match.group("body"), re.MULTILINE))
    assert json_types == CANONICAL_ENTITY_TYPES
