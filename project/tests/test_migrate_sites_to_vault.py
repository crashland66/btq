"""Gates for the one-time btq_sites → btq_vault fold (site-identity merge).

The fold must EXECUTE the new merge semantics, not just look plausible:
registration fields land on the location doc, the vault display name is never
overwritten, canonical drift turns the retired registry name into an alias,
SANDBOX stays out of the vault, and the fold is idempotent.
"""

from __future__ import annotations

from event_pipeline.couchdb.migrate_sites_to_vault import folded_location_doc


def site_doc(**overrides):
    doc = {
        "_id": "site_7050",
        "type": "site",
        "site_id": "7050",
        "account": "Summit Wire",
        "location": "Summit Wire",
        "active": True,
        "aliases": ["Summit", "summit  wire"],
        "note_path": "Accounts/Summit/about.md",
        "vision_context": {"context_id": "7050", "label": "Summit Wire"},
    }
    doc.update(overrides)
    return doc


def location_doc(**overrides):
    doc = {
        "_id": "location_7050",
        "type": "location",
        "operator": "op_greg",
        "status": "active",
        "account": "Summit",
        "location": "Summit Wire",
        "billing_monthly": "1883.15",
        "address": "1 Wire Way",
        "job": 7050,
    }
    doc.update(overrides)
    return doc


def test_folds_registration_fields_onto_location_doc():
    merged, _notes = folded_location_doc(site_doc(), location_doc())

    assert merged["site_id"] == "7050"
    assert merged["capture_active"] is True
    assert merged["aliases"] == ["summit", "summit wire"]  # normalized + sorted
    assert merged["note_path"] == "Accounts/Summit/about.md"
    assert merged["vision_context"] == {"context_id": "7050", "label": "Summit Wire"}


def test_never_overwrites_vault_operational_fields():
    merged, _notes = folded_location_doc(site_doc(account="Different Name"), location_doc())

    # The vault display name and operational fields are canonical — the fold
    # must not touch them even when the site doc disagrees.
    assert merged["location"] == "Summit Wire"
    assert merged["account"] == "Summit"
    assert merged["billing_monthly"] == "1883.15"
    assert merged["address"] == "1 Wire Way"
    assert merged["operator"] == "op_greg"


def test_canonical_drift_adds_old_registry_name_as_alias():
    merged, notes = folded_location_doc(
        site_doc(account="Cencam High School"),
        location_doc(location="Central Cambria High School"),
    )

    assert merged["location"] == "Central Cambria High School"
    assert "cencam high school" in merged["aliases"]
    assert any("drift" in note for note in notes)


def test_inactive_site_folds_capture_active_false():
    merged, _notes = folded_location_doc(site_doc(active=False), location_doc())
    assert merged["capture_active"] is False

    # Missing active is inactive, not active — the by_alias gate was
    # doc.active === true and the folded gate is capture_active === true.
    site = site_doc()
    del site["active"]
    merged, _notes = folded_location_doc(site, location_doc())
    assert merged["capture_active"] is False


def test_optional_fields_fold_only_when_present():
    merged, _notes = folded_location_doc(site_doc(), location_doc())
    assert "capture_guidance" not in merged
    assert "display_categories" not in merged

    merged, _notes = folded_location_doc(
        site_doc(capture_guidance="Focus dock", display_categories=[{"label": "A", "canonical": "a"}]),
        location_doc(),
    )
    assert merged["capture_guidance"] == "Focus dock"
    assert merged["display_categories"] == [{"label": "A", "canonical": "a"}]


def test_missing_location_doc_creates_minimal_canonical_doc():
    merged, notes = folded_location_doc(site_doc(), None)

    assert merged["_id"] == "location_7050"
    assert merged["type"] == "location"
    assert merged["operator"]  # validator requires an operator on location docs
    assert merged["status"] == "active"
    assert merged["location"] == "Summit Wire"
    assert any("created" in note for note in notes)


def test_fold_is_idempotent():
    first, _ = folded_location_doc(site_doc(), location_doc())
    second, _ = folded_location_doc(site_doc(), first)
    assert second == first
