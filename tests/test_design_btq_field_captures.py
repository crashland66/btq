from __future__ import annotations

import json
from pathlib import Path


DESIGN_DOC = Path("project/event_pipeline/couchdb/design_btq_field_captures.json")


def by_upload_id_map() -> str:
    payload = json.loads(DESIGN_DOC.read_text(encoding="utf-8"))
    return str(payload["views"]["by_upload_id"]["map"])


def test_by_upload_id_view_emits_target_type_field() -> None:
    body = by_upload_id_map()

    assert "target_type" in body
    assert "emit(record.upload_id, {" in body


def test_by_upload_id_view_does_not_skip_prospect_docs() -> None:
    body = by_upload_id_map()

    assert "if (!siteId) return;" not in body
