from __future__ import annotations

from event_pipeline.couchdb.audit_site_coverage import missing_sites


def test_missing_sites_reports_unregistered_active_location() -> None:
    rows = [
        {
            "id": "location_7040",
            "value": {"site_id": "7040", "account": "KMF"},
            "doc": {
                "_id": "location_7040",
                "type": "location",
                "site_id": "7040",
                "account": "KMF",
                "location": "KMF Industries- Oak St",
                "status": "active",
            },
        },
        {
            "id": "location_7050",
            "value": {"site_id": "7050", "account": "Summit"},
            "doc": {
                "_id": "location_7050",
                "type": "location",
                "site_id": "7050",
                "account": "Summit",
                "location": "Summit Wire",
                "status": "active",
            },
        },
        {
            "id": "location_999",
            "value": {"site_id": "999", "account": "Closed"},
            "doc": {
                "_id": "location_999",
                "type": "location",
                "site_id": "999",
                "account": "Closed",
                "location": "Closed Site",
                "status": "inactive",
            },
        },
    ]

    assert missing_sites({"7050"}, rows) == [
        {
            "site_id": "7040",
            "canonical": "KMF - KMF Industries- Oak St",
            "status": "active",
        }
    ]


def test_missing_sites_empty_when_all_registered() -> None:
    rows = [
        {
            "id": "location_7040",
            "value": {"site_id": "7040", "account": "KMF"},
            "doc": {
                "_id": "location_7040",
                "type": "location",
                "site_id": "7040",
                "account": "KMF",
                "location": "KMF Industries- Oak St",
                "status": "active",
            },
        }
    ]

    assert missing_sites({"7040"}, rows) == []
