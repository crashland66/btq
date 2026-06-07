from __future__ import annotations

import inspect

from event_pipeline.couchdb import migrate_sites


def test_normalize_contexts_uses_local_seed_data_without_photo_vision_import() -> None:
    source = inspect.getsource(migrate_sites)

    contexts = migrate_sites.normalize_contexts()

    assert "field_capture.photo_vision" not in source
    assert contexts["7050"] == {
        "context_id": "7050",
        "label": "Summit Wire",
        "summary": (
            "Photos are interior field-capture images from an industrial facility. "
            "Common areas may include locker rooms, restrooms, admin offices, entryways, break rooms, "
            "supply areas, mill or plant-adjacent spaces, and maintenance areas. Lighting, concrete floors, "
            "industrial fixtures, lockers, tools, carts, and heavy-use restrooms may appear."
        ),
        "environment": "industrial manufacturing / wire facility",
    }
    assert contexts["contmetal"]["context_id"] == "contmetal"
    assert contexts["contmetal"]["environment"] == "industrial manufacturing"
