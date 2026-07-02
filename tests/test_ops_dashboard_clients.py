from __future__ import annotations

from types import SimpleNamespace

from ops_dashboard.sections import clients


def test_latest_client_by_person_returns_most_recent_synthetic_capture() -> None:
    docs = [
        {
            "_id": "cap-old",
            "person_id": "jordan-avery",
            "person_name": "Jordan Avery",
            "captured_at": "2026-06-26T10:00:00-04:00",
            "client": {"kind": "pwa", "platform": "ios", "os_version": "iOS 17.4", "app_version": "0.0.0"},
        },
        {
            "_id": "cap-new",
            "person_id": "jordan-avery",
            "person_name": "Jordan Avery",
            "captured_at": "2026-06-27T10:00:00-04:00",
            "client": {"kind": "native", "platform": "ios", "os_version": "17.5", "app_version": "1.2.0"},
        },
        {
            "_id": "cap-other",
            "person_id": "damon-smith",
            "person_name": "Damon Smith",
            "created_at": "2026-06-27T09:00:00Z",
            "client": {"kind": "pwa", "platform": "android"},
        },
        {
            "_id": "cap-no-client",
            "person_id": "no-client",
            "person_name": "No Client",
            "created_at": "2026-06-27T11:00:00Z",
        },
    ]

    rollup = clients.latest_client_by_person(docs)

    assert rollup["jordan-avery"]["capture_id"] == "cap-new"
    assert rollup["jordan-avery"]["kind"] == "native"
    assert rollup["jordan-avery"]["app_version"] == "1.2.0"
    assert rollup["damon-smith"]["platform"] == "android"
    assert "no-client" not in rollup


def test_clients_dashboard_renders_no_client_info_as_dash(monkeypatch) -> None:
    monkeypatch.setattr(
        clients,
        "load_employees",
        lambda: [
            {"_id": "employee_jordan-avery", "type": "employee", "person_id": "jordan-avery", "name": "Jordan Avery"},
            {"_id": "employee_no-client", "type": "employee", "person_id": "no-client", "name": "No Client"},
        ],
    )
    monkeypatch.setattr(
        clients,
        "load_capture_client_docs",
        lambda: [
            {
                "_id": "cap-client",
                "person_id": "jordan-avery",
                "person_name": "Jordan Avery",
                "captured_at": "2026-06-27T10:00:00-04:00",
                "client": {"kind": "pwa", "platform": "ios", "os_version": "iOS 17.5", "app_version": "0.9.0"},
            }
        ],
    )

    rendered = clients.render(SimpleNamespace(query={}, runtime_root=None, route_path="/clients"))

    assert "Clients / Devices" in rendered
    assert "Jordan Avery" in rendered
    assert "No Client" in rendered
    assert "pwa" in rendered
    assert "&mdash;" in rendered


def test_clients_dashboard_filters_pwa_ios_invite_list(monkeypatch) -> None:
    monkeypatch.setattr(
        clients,
        "load_employees",
        lambda: [
            {"_id": "employee_ios-pwa", "type": "employee", "person_id": "ios-pwa", "name": "iOS PWA"},
            {"_id": "employee_ios-native", "type": "employee", "person_id": "ios-native", "name": "iOS Native"},
            {"_id": "employee_android-pwa", "type": "employee", "person_id": "android-pwa", "name": "Android PWA"},
        ],
    )
    monkeypatch.setattr(
        clients,
        "load_capture_client_docs",
        lambda: [
            {"person_id": "ios-pwa", "person_name": "iOS PWA", "created_at": "2026-06-27T10:00:00Z", "client": {"kind": "pwa", "platform": "ios"}},
            {"person_id": "ios-native", "person_name": "iOS Native", "created_at": "2026-06-27T10:00:00Z", "client": {"kind": "native", "platform": "ios"}},
            {"person_id": "android-pwa", "person_name": "Android PWA", "created_at": "2026-06-27T10:00:00Z", "client": {"kind": "pwa", "platform": "android"}},
        ],
    )

    rendered = clients.render(SimpleNamespace(query={"kind": ["pwa"], "platform": ["ios"]}, runtime_root=None, route_path="/clients"))

    assert "iOS PWA" in rendered
    assert "iOS Native" not in rendered
    assert "Android PWA" not in rendered
