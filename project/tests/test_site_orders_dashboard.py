from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from ops_dashboard.app import route_response
from ops_dashboard.sections import inbox, site_orders


def request_text(path: str, runtime_root: Path) -> tuple[HTTPStatus, str]:
    status, _content_type, body = route_response("GET", path, runtime_root)
    return status, body.decode("utf-8")


def install_reference_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_reference_find_all(selector: dict[str, object], **_kwargs) -> list[dict[str, object]]:
        doc_type = selector.get("type")
        if doc_type == "staples_usage_ship_to_summary":
            return [
                {
                    "ship_to_name": "JWF INDUSTRIES",
                    "ship_to_numbers": ["JWF"],
                    "quantity": 199,
                    "estimated_monthly_quantity": 34.31,
                    "adjusted_gross_sales": 2839.67,
                    "sku_count": 20,
                    "line_count": 20,
                },
                {
                    "ship_to_name": "PROFORM POWDERED METALS INC",
                    "ship_to_numbers": ["PROFORM POWDERE"],
                    "quantity": 16,
                    "estimated_monthly_quantity": 2.76,
                    "adjusted_gross_sales": 428.47,
                    "sku_count": 9,
                    "line_count": 9,
                },
            ]
        if doc_type == "staples_usage_ship_to_sku_summary":
            assert selector.get("ship_to_name") == "PROFORM POWDERED METALS INC"
            return [
                {
                    "sku_number": "24631531",
                    "website_item_description": "Diversey Crew Clinging Plus Toilet Bowl Cleaner, Citrus Scent, 32 fl. oz., 12/Carton (9302467)",
                    "quantity": 2,
                    "estimated_monthly_quantity": 0.34,
                    "average_selling_price_values": [37.24],
                    "adjusted_gross_sales": 74.48,
                    "line_count": 1,
                }
            ]
        if doc_type == "staples_usage_row":
            assert selector.get("ship_to_name") == "PROFORM POWDERED METALS INC"
            return [
                {
                    "row_index": 313,
                    "ship_to_number": "PROFORM POWDERE",
                    "sku_number": "24631531",
                    "website_item_description": "Diversey Crew Clinging Plus Toilet Bowl Cleaner, Citrus Scent, 32 fl. oz., 12/Carton (9302467)",
                    "quantity": 2,
                    "average_selling_price": 37.24,
                    "adjusted_gross_sales": 74.48,
                }
            ]
        if doc_type == "dashboard_work_item":
            return []
        return []

    monkeypatch.setattr(site_orders, "reference_find_all", fake_reference_find_all)


def test_site_orders_route_lists_sites(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_reference_fixture(monkeypatch)

    status, body = request_text("/site-orders", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Site Orders" in body
    assert "JWF INDUSTRIES" in body
    assert "PROFORM POWDERED METALS INC" in body
    assert "/site-orders?ship_to_name=PROFORM%20POWDERED%20METALS%20INC" in body


def test_site_orders_route_filters_to_selected_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_reference_fixture(monkeypatch)

    status, body = request_text("/site-orders?ship_to_name=PROFORM%20POWDERED%20METALS%20INC", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert '<option value="PROFORM POWDERED METALS INC" selected>' in body
    assert "Diversey Crew Clinging Plus Toilet Bowl Cleaner" in body
    assert "24631531" in body
    assert "$74.48" in body
    assert "Order Lines" in body


def test_inbox_dashboard_work_items_card_reads_reference_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://127.0.0.1:5984")
    monkeypatch.setattr(
        site_orders,
        "reference_find_all",
        lambda _selector, **_kwargs: [
            {
                "_id": "dashboard_work_site_orders_2026-06-25",
                "title": "Add site-order reference view",
                "route": "/site-orders",
                "created_at": "2026-06-25T12:00:00Z",
            }
        ],
    )

    count, rows = inbox.dashboard_work_item_rows()

    assert count == 1
    assert rows[0]["summary"] == "Add site-order reference view"
    assert rows[0]["deep_link"] == "/site-orders"
