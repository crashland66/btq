from __future__ import annotations

import json
import re
from pathlib import Path

from btq_vault.entity_types import CANONICAL_ENTITY_TYPES


DESIGN_DOC_PATH = Path("project/event_pipeline/couchdb/design_btq_vault.json")


def load_design_doc() -> dict[str, object]:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


def test_design_btq_vault_json_is_valid_json() -> None:
    assert isinstance(load_design_doc(), dict)


def test_design_btq_vault_has_validate_doc_update() -> None:
    validate_doc_update = load_design_doc().get("validate_doc_update")

    assert isinstance(validate_doc_update, str)
    assert validate_doc_update


def test_design_btq_vault_canonical_type_set_matches_python_constant() -> None:
    validate_doc_update = load_design_doc()["validate_doc_update"]
    assert isinstance(validate_doc_update, str)

    match = re.search(r"var canonicalTypes = \{(?P<body>.*?)\n  \};", validate_doc_update, re.DOTALL)
    assert match is not None
    js_types = set(re.findall(r'"([^"]+)": true', match.group("body")))

    assert js_types == CANONICAL_ENTITY_TYPES


def test_design_btq_vault_has_by_type_view() -> None:
    design_doc = load_design_doc()

    assert isinstance(design_doc.get("views"), dict)
    assert isinstance(design_doc["views"].get("by_type"), dict)  # type: ignore[index,union-attr]
    assert isinstance(design_doc["views"]["by_type"].get("map"), str)  # type: ignore[index,union-attr]


def test_design_btq_vault_has_by_operator_type_view() -> None:
    design_doc = load_design_doc()

    assert isinstance(design_doc.get("views"), dict)
    assert isinstance(design_doc["views"].get("by_operator_type"), dict)  # type: ignore[index,union-attr]
    assert isinstance(design_doc["views"]["by_operator_type"].get("map"), str)  # type: ignore[index,union-attr]
