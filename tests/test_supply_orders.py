from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supply_order_markdown import (
    build_supply_order_markdown,
    supply_order_filename,
    supply_order_markdown_path,
    write_supply_order_markdown,
)
from supply_orders import (
    SupplyOrder,
    calculate_budget_variance,
    load_site_metadata,
    parse_staples_email,
    resolve_site,
    site_metadata_by_id,
)


STAPLES_HTML = """
<html>
  <body>
    <h1>Thanks for your Staples order</h1>
    <div>Order Number: 99887766</div>
    <div>Order Date: 04/20/2026</div>
    <div>PO Number: PO-7080-APR</div>
    <h2>Shipping Address</h2>
    <div>Apex Powdered Metals</div>
    <div>700 Martha St</div>
    <div>Springfield, PA 00000</div>
    <table>
      <tr>
        <th>Item #</th>
        <th>Description</th>
        <th>Qty</th>
        <th>Unit Price</th>
        <th>Amount</th>
      </tr>
      <tr>
        <td>123456</td>
        <td>Trash Liners 56 Gal</td>
        <td>2</td>
        <td>$18.49</td>
        <td>$36.98</td>
      </tr>
      <tr>
        <td>998100</td>
        <td>Foaming Hand Soap</td>
        <td>4</td>
        <td>$6.25</td>
        <td>$25.00</td>
      </tr>
    </table>
    <table>
      <tr><td>Subtotal</td><td>$61.98</td></tr>
      <tr><td>Tax</td><td>$3.72</td></tr>
      <tr><td>Shipping</td><td>$0.00</td></tr>
      <tr><td>Total</td><td>$65.70</td></tr>
    </table>
  </body>
</html>
"""


STAPLES_HTML_NO_PO = """
<html>
  <body>
    <div>Staples Order # 11122233</div>
    <div>Order Date: 04/18/2026</div>
    <div>Shipping Address</div>
    <div>Unknown Plant</div>
    <div>12 Mystery Rd</div>
    <div>Nowhere, PA 10000</div>
    <table>
      <tr><th>SKU</th><th>Description</th><th>Quantity</th><th>Price</th><th>Extended</th></tr>
      <tr><td>ABC123</td><td>Paper Towels</td><td>1</td><td>$19.99</td><td>$19.99</td></tr>
    </table>
    <div>Subtotal</div><div>$19.99</div>
    <div>Tax</div><div>$1.20</div>
    <div>Total</div><div>$21.19</div>
  </body>
</html>
"""


class RecordingLocationStore:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.get_optional_calls: list[str] = []
        self.find_location_docs_calls = 0

    def find_location_docs(self) -> list[dict[str, Any]]:
        self.find_location_docs_calls += 1
        return [dict(doc) for doc in self.docs if doc.get("type") == "location"]

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        self.get_optional_calls.append(doc_id)
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None


@dataclass(frozen=True)
class MarkdownSite:
    about_path: Path
    monthly_supply_budget: float | None


def location_doc(
    *,
    account: str,
    location: str,
    job: str,
    address: str,
    site_aliases: object = (),
    aliases: object = (),
    budget: str | None = "250.00",
) -> dict[str, Any]:
    return {
        "_id": f"location_{job}",
        "type": "location",
        "account": account,
        "location": location,
        "job": job,
        "address": address,
        "site_aliases": site_aliases,
        "aliases": aliases,
        "monthly_supply_budget": budget,
        "budget_basis": "monthly_actual",
    }


def make_order() -> SupplyOrder:
    return SupplyOrder(
        type="supply_order",
        vendor="Staples",
        order_number="7678969426",
        order_date="2026-04-20",
        po_number="1152",
        site_name_raw="Brookline Health",
        site_id="1152",
        account="Brookline",
        subtotal=101.00,
        tax=6.07,
        shipping=0.00,
        total=107.07,
        items=[
            parse_staples_email(
                """
                <html>
                  <body>
                    <div>Order Number: 7678969426</div>
                    <div>Order Date: 04/20/2026</div>
                    <div>Shipping Address</div>
                    <div>Brookline Health</div>
                    <table>
                      <tr><th>Item #</th><th>Description</th><th>Qty</th><th>Unit Price</th><th>Amount</th></tr>
                      <tr><td>219296</td><td>Microfiber Cloths Red</td><td>2</td><td>$11.63</td><td>$23.26</td></tr>
                    </table>
                    <div>Total</div><div>$107.07</div>
                  </body>
                </html>
                """,
                "subject",
                datetime(2026, 4, 20, 8, 15, tzinfo=timezone.utc),
            ).items[0]
        ],
        source_email_subject="Staples order confirmation 7678969426",
        source_email_date="2026-04-20T08:15:00+00:00",
        extraction_confidence=1.0,
        unresolved_reason=None,
    )


def test_parse_staples_email_extracts_realistic_order() -> None:
    order = parse_staples_email(
        STAPLES_HTML,
        "Staples order confirmation 99887766",
        datetime(2026, 4, 20, 8, 15, tzinfo=timezone.utc),
    )

    assert order.order_number == "99887766"
    assert order.order_date == "2026-04-20"
    assert order.po_number == "PO-7080-APR"
    assert order.site_name_raw == "Apex Powdered Metals"
    assert order.subtotal == 61.98
    assert order.tax == 3.72
    assert order.shipping == 0.0
    assert order.total == 65.70
    assert len(order.items) == 2
    assert order.items[0].sku == "123456"
    assert order.items[0].description == "Trash Liners 56 Gal"
    assert order.items[0].quantity == 2
    assert order.items[1].extended_price == 25.00
    assert order.extraction_confidence == 1.0


def test_parse_staples_email_extracts_multiple_line_items() -> None:
    order = parse_staples_email(
        STAPLES_HTML,
        "Staples order confirmation 99887766",
        datetime(2026, 4, 20, 8, 15, tzinfo=timezone.utc),
    )

    assert [item.description for item in order.items] == ["Trash Liners 56 Gal", "Foaming Hand Soap"]


def test_parse_staples_email_missing_po_number_lowers_confidence() -> None:
    order = parse_staples_email(
        STAPLES_HTML_NO_PO,
        "Staples order confirmation 11122233",
        datetime(2026, 4, 18, 11, 0, tzinfo=timezone.utc),
    )

    assert order.order_number == "11122233"
    assert order.po_number is None
    assert order.total == 21.19
    assert order.extraction_confidence == 1.0


def test_load_site_metadata_reads_canonical_location_docs() -> None:
    store = RecordingLocationStore(
        [
            location_doc(
                account="Apexco",
                location="Apex Powdered Metals",
                job="7080",
                address="700 Martha St, Springfield, PA 00000",
                site_aliases=["Apex"],
                aliases="[Apex PM]",
                budget="250.00",
            ),
            location_doc(
                account="Summitsteel",
                location="Summit Wire",
                job="7050",
                address="100 Summit Ave, Riverton, PA 00000",
                budget="374.59",
            ),
        ]
    )

    sites = load_site_metadata(store)

    assert [site.site_id for site in sites] == ["7050", "7080"]
    apex = sites[1]
    assert apex.canonical_name == "Apex Powdered Metals"
    assert apex.account == "Apexco"
    assert apex.address == "700 Martha St, Springfield, PA 00000"
    assert apex.monthly_supply_budget == 250.0
    assert apex.budget_basis == "monthly_actual"
    assert apex.aliases == sorted(["7080", "Apex Powdered Metals", "7080 - Apex Powdered Metals", "Apex", "Apex PM"])


def test_resolve_site_success() -> None:
    store = RecordingLocationStore(
        [
            location_doc(
                account="Apexco",
                location="Apex Powdered Metals",
                job="7080",
                address="700 Martha St, Springfield, PA 00000",
                site_aliases="[Apex Powdered Metals, Apex]",
            )
        ]
    )

    site_id, confidence = resolve_site(
        "Apex",
        "Apex Powdered Metals\n700 Martha St\nSpringfield, PA 00000",
        store,
    )

    assert site_id == "7080"
    assert confidence >= 0.7


def test_resolve_site_failure() -> None:
    store = RecordingLocationStore(
        [
            location_doc(
                account="Apexco",
                location="Apex Powdered Metals",
                job="7080",
                address="700 Martha St, Springfield, PA 00000",
                site_aliases="[Apex Powdered Metals, Apex]",
            )
        ]
    )

    site_id, confidence = resolve_site(
        "Mystery Warehouse",
        "12 Unknown Street\nNowhere, PA 10000",
        store,
    )

    assert site_id is None
    assert confidence < 0.7


def test_site_metadata_by_id_reads_single_canonical_doc() -> None:
    store = RecordingLocationStore(
        [
            location_doc(
                account="Apexco",
                location="Apex Powdered Metals",
                job="7080",
                address="700 Martha St, Springfield, PA 00000",
                budget="250.00",
            )
        ]
    )

    site = site_metadata_by_id("7080", store)

    assert site is not None
    assert site.site_id == "7080"
    assert site.monthly_supply_budget == 250.0
    assert store.get_optional_calls == ["location_7080"]
    assert store.find_location_docs_calls == 0


def test_calculate_budget_variance(tmp_path: Path) -> None:
    store = RecordingLocationStore(
        [
            location_doc(
                account="Apexco",
                location="Apex Powdered Metals",
                job="7080",
                address="700 Martha St, Springfield, PA 00000",
                site_aliases="[Apex Powdered Metals, Apex]",
                budget="250.00",
            )
        ]
    )
    data_root = tmp_path / "data"
    month_dir = data_root / "supply_orders" / "2026" / "04"
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / "99887766.json").write_text(
        json.dumps(
            {
                "type": "supply_order",
                "vendor": "Staples",
                "order_number": "99887766",
                "order_date": "2026-04-20",
                "po_number": "PO-7080-APR",
                "site_name_raw": "Apex Powdered Metals",
                "site_id": "7080",
                "account": "Apexco",
                "subtotal": 61.98,
                "tax": 3.72,
                "shipping": 0.0,
                "total": 65.70,
                "items": [],
                "source_email_subject": "subject",
                "source_email_date": "2026-04-20T08:15:00+00:00",
                "extraction_confidence": 1.0,
                "unresolved_reason": None,
            }
        ),
        encoding="utf-8",
    )

    variance = calculate_budget_variance("7080", "2026-04", store, data_root)

    assert variance["total_actual_spend"] == 65.70
    assert variance["budgeted_amount"] == 250.0
    assert variance["variance"] == -184.3
    assert variance["variance_percent"] == -73.72


def test_supply_order_markdown_file_creation_and_content(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    order = make_order()
    site = MarkdownSite(
        about_path=vault_root / "Accounts" / "Brookline" / "Locations" / "1152 - Brookline Health" / "about.md",
        monthly_supply_budget=250.0,
    )

    path = write_supply_order_markdown(order, site)

    assert path is not None
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "type: supply_order" in text
    assert "site_id: 1152" in text
    assert "# Supply Order — Staples" in text
    assert "**Monthly Budget:** $250.00" in text
    assert "**Order Impact:** -$142.93 vs monthly budget" in text
    assert "- 2x Microfiber Cloths Red — $23.26" in text
    assert "Auto-generated from supply order pipeline." in text


def test_supply_order_markdown_uses_stable_filename(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    order = make_order()
    site = MarkdownSite(
        about_path=vault_root / "Accounts" / "Brookline" / "Locations" / "1152 - Brookline Health" / "about.md",
        monthly_supply_budget=None,
    )

    assert supply_order_filename(order) == "2026-04-20 - staples-order-7678969426.md"
    assert supply_order_markdown_path(order, site) == (
        vault_root
        / "Accounts"
        / "Brookline"
        / "Locations"
        / "1152 - Brookline Health"
        / "Supplies"
        / "2026-04-20 - staples-order-7678969426.md"
    )


def test_supply_order_markdown_overwrites_existing_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    order = make_order()
    site = MarkdownSite(
        about_path=vault_root / "Accounts" / "Brookline" / "Locations" / "1152 - Brookline Health" / "about.md",
        monthly_supply_budget=None,
    )
    path = supply_order_markdown_path(order, site)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale content\n", encoding="utf-8")

    updated_path = write_supply_order_markdown(order, site)

    assert updated_path == path
    assert path.read_text(encoding="utf-8") != "stale content\n"
    assert "stale content" not in path.read_text(encoding="utf-8")


def test_supply_order_markdown_missing_site_returns_none() -> None:
    order = make_order().with_resolution(site_id=None, account=None, unresolved_reason="unresolved_site")

    assert write_supply_order_markdown(order, None) is None


def test_build_supply_order_markdown_renders_none_for_empty_items() -> None:
    order = make_order()
    empty_order = SupplyOrder(
        **{**order.__dict__, "items": []},
    )

    text = build_supply_order_markdown(empty_order, None)

    assert "## Items" in text
    assert "- None" in text
