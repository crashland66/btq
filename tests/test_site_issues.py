from __future__ import annotations

import io
import json
import urllib.request

import pytest

from site_issues import discover_site_issues, issue_as_export


def issue_doc(
    *,
    issue_id: str = "iss_drain",
    site_id: str = "7050",
    status: str = "open",
    priority: str = "high",
    category: str = "maintenance",
    client_notified: bool = True,
    archived: bool = False,
    **extra: object,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": f"site_issue_{issue_id}",
        "type": "site_issue",
        "issue_id": issue_id,
        "site_id": site_id,
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "title": "Restroom drain backup",
        "status": status,
        "priority": priority,
        "category": category,
        "client_notified": client_notified,
        "client_notified_at": "2026-05-08T15:28:09+00:00",
        "client_notified_by": "Jordan",
        "client_notified_method": "email",
        "archived": archived,
        "archived_at": "2026-06-10T12:00:00+00:00",
        "archived_by": "Jordan",
        "reported_by": "Tom Walsh",
        "observed_at": "2026-05-08T14:12:43+00:00",
        "created_at": "2026-05-08T17:43:43+00:00",
        "summary": "Drain backed up and water reached the floor.",
        "resolution_trigger": "Maintenance confirms the drain is clear.",
        "related_capture_ids": ["cap-photo-drain"],
        "related_candidate_ids": ["ac_drain"],
    }
    doc.update(extra)
    return doc


def _selector_matches(doc: dict[str, object], selector: dict[str, object]) -> bool:
    for key, expected in selector.items():
        if key == "type":
            if doc.get("type") != expected:
                return False
        elif key == "archived":
            actual = bool(doc.get("archived"))
            if isinstance(expected, dict) and "$ne" in expected:
                if actual == bool(expected["$ne"]):
                    return False
            elif actual is not bool(expected):
                return False
        elif key == "site_id":
            if str(doc.get("site_id")) != str(expected):
                return False
    return True


def patch_couch(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, object]]) -> None:
    """Drive the CouchDB ``_find`` reader off an in-memory doc list."""
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        payload = json.loads(req.data.decode("utf-8"))
        selector = payload["selector"]
        matched = [doc for doc in docs if _selector_matches(doc, selector)]
        body = json.dumps({"docs": matched}).encode("utf-8")
        return io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_site_issues_discovers_counts_and_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            issue_doc(),
            issue_doc(issue_id="iss_resolved", status="resolved", priority="normal", client_notified=False),
        ],
    )

    report = discover_site_issues()
    issues = report["issues"]
    counts = report["counts"]

    assert len(issues) == 2
    assert counts["total"] == 2
    assert counts["current"] == 1
    assert counts["open"] == 1
    assert counts["resolved"] == 1
    assert counts["by_site"] == {"7050": 2}
    assert counts["by_category"] == {"maintenance": 2}
    assert counts["by_priority"] == {"high": 1, "normal": 1}
    assert counts["by_status"] == {"open": 1, "resolved": 1}
    assert counts["client_notified"] == {"true": 1, "false": 0}
    exported = issue_as_export(issues[0])
    assert exported["issue_id"] == "iss_drain"
    assert exported["client_notified"] is True
    assert exported["related_capture_ids"] == ["cap-photo-drain"]
    assert exported["related_candidate_ids"] == ["ac_drain"]
    assert "vault_issue_path" not in exported


def test_site_issues_filters_by_site(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            issue_doc(issue_id="iss_7050", site_id="7050"),
            issue_doc(issue_id="iss_7060", site_id="7060"),
        ],
    )

    report = discover_site_issues(site_id="7060")

    assert [issue.issue_id for issue in report["issues"]] == ["iss_7060"]
    assert report["counts"]["by_site"] == {"7060": 1}


def test_site_issues_excludes_archived_by_default_and_can_list_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            issue_doc(issue_id="iss_open"),
            issue_doc(issue_id="iss_archived", archived=True),
        ],
    )

    default_report = discover_site_issues()
    archived_report = discover_site_issues(include_archived=True, archived_only=True)

    assert [issue.issue_id for issue in default_report["issues"]] == ["iss_open"]
    assert default_report["counts"]["total"] == 1
    assert [issue.issue_id for issue in archived_report["issues"]] == ["iss_archived"]
    assert archived_report["counts"]["total"] == 1
