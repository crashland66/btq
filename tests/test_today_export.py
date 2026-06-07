from __future__ import annotations

from btq_vault import today_export
from btq_vault.today_export import export_today


def test_export_today_includes_visits(monkeypatch) -> None:
    def fake_query_view(*args, **kwargs):
        if kwargs["startkey"][0] == "visit":
            return [
                {
                    "doc": {
                        "type": "visit",
                        "date": "2026-05-24",
                        "site": "Kmf",
                        "site_id": "kmf",
                        "visit_type": "QC",
                        "confidence": "high",
                        "visited_by": None,
                    }
                }
            ]
        return []

    monkeypatch.setattr(today_export, "query_view", fake_query_view)

    assert "Kmf" in export_today("http://couchdb.test", {}, "btq_vault", "2026-05-24")


def test_export_today_includes_issues(monkeypatch) -> None:
    def fake_query_view(*args, **kwargs):
        if kwargs["startkey"][0] == "site_issue":
            return [
                {
                    "doc": {
                        "type": "site_issue",
                        "created_at": "2026-05-24T10:00:00Z",
                        "title": "Restroom drain backup",
                        "site_name": "Summit Wire",
                        "site_id": "7050",
                        "status": "open",
                    }
                }
            ]
        return []

    monkeypatch.setattr(today_export, "query_view", fake_query_view)

    result = export_today("http://couchdb.test", {}, "btq_vault", "2026-05-24")

    assert "Restroom drain backup" in result


def test_export_today_returns_placeholder_when_empty(monkeypatch) -> None:
    monkeypatch.setattr(today_export, "query_view", lambda *args, **kwargs: [])

    assert "No entity activity" in export_today("http://couchdb.test", {}, "btq_vault", "2026-05-24")
