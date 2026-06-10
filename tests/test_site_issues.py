from __future__ import annotations

from pathlib import Path

from site_issues import discover_site_issues, issue_as_export


def write_issue(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    issue_id: str = "iss_drain",
    site_id: str = "7050",
    status: str = "open",
    priority: str = "high",
    category: str = "maintenance",
    client_notified: bool = True,
    archived: bool = False,
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Issues" / f"{issue_id}__issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: site_issue
issue_id: {issue_id}
site_id: "{site_id}"
site: Summit Wire
account: {account}
title: Restroom drain backup
status: {status}
priority: {priority}
category: {category}
client_notified: {str(client_notified).lower()}
client_notified_at: 2026-05-08T15:28:09+00:00
client_notified_by: Jordan
client_notified_method: email
archived: {str(archived).lower()}
archived_at: 2026-06-10T12:00:00+00:00
archived_by: Jordan
reported_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
created_at: 2026-05-08T17:43:43+00:00
resolution_trigger: Maintenance confirms the drain is clear.
related_capture_ids:
  - cap-photo-drain
related_candidate_ids:
  - ac_drain
---
# Restroom drain backup

## Summary
Drain backed up and water reached the floor.
""",
        encoding="utf-8",
    )
    return path


def test_site_issues_discovers_counts_and_exports_vault_issues(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_issue(vault_root)
    write_issue(vault_root, issue_id="iss_resolved", status="resolved", priority="normal", client_notified=False)
    malformed = vault_root / "Accounts" / "Bad" / "Locations" / "999 - Bad" / "Issues" / "bad.md"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("---\ntype: site_issue\nissue_id: iss_bad\n---\n", encoding="utf-8")

    report = discover_site_issues(vault_root)
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
    assert report["warnings"][0]["reason"] == "missing_required_site_id_or_issue_id"
    exported = issue_as_export(issues[0])
    assert exported["issue_id"] == "iss_drain"
    assert exported["client_notified"] is True
    assert exported["related_capture_ids"] == ["cap-photo-drain"]
    assert exported["related_candidate_ids"] == ["ac_drain"]
    assert "vault_issue_path" not in exported


def test_site_issues_filters_by_site(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_issue(vault_root, issue_id="iss_7050", site_id="7050")
    write_issue(vault_root, account="Contworks", site_dir="7060 - Continental Metalworks", issue_id="iss_7060", site_id="7060")

    report = discover_site_issues(vault_root, site_id="7060")

    assert [issue.issue_id for issue in report["issues"]] == ["iss_7060"]
    assert report["counts"]["by_site"] == {"7060": 1}


def test_site_issues_excludes_archived_by_default_and_can_list_archived(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_issue(vault_root, issue_id="iss_open")
    write_issue(vault_root, issue_id="iss_archived", archived=True)

    default_report = discover_site_issues(vault_root)
    archived_report = discover_site_issues(vault_root, include_archived=True, archived_only=True)

    assert [issue.issue_id for issue in default_report["issues"]] == ["iss_open"]
    assert default_report["counts"]["total"] == 1
    assert [issue.issue_id for issue in archived_report["issues"]] == ["iss_archived"]
    assert archived_report["counts"]["total"] == 1
