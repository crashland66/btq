from __future__ import annotations

import html
import os
from urllib.parse import quote

from admin_reporting import staples_reads
from ops_dashboard.common import first_query_value, render_table
from ops_dashboard.layout import html_page


REFERENCES_DB = staples_reads.REFERENCES_DB
STAPLES_REPORT_ID = staples_reads.STAPLES_REPORT_ID
DASHBOARD_WORK_ITEM_TYPE = "dashboard_work_item"
PAGE_LIMIT = staples_reads.PAGE_LIMIT
reference_find_all = staples_reads.reference_find_all


def render(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    selected_site = first_query_value(query, "ship_to_name").strip()
    body = render_body(selected_site)
    return html_page("Site Orders", body, active_section="site_orders")


def render_body(selected_site: str = "") -> str:
    report = load_site_orders_report(selected_site=selected_site)
    if report.get("available") is False:
        reason = str(report.get("reason") or "Reference database unavailable.")
        return f"""
        <header>
          <h1>Site Orders</h1>
        </header>
        <section class="error"><p>{html.escape(reason)}</p></section>
        """

    summaries = report["site_summaries"] if isinstance(report.get("site_summaries"), list) else []
    selected_summary = report.get("selected_summary") if isinstance(report.get("selected_summary"), dict) else None
    sku_rows = report["sku_summaries"] if isinstance(report.get("sku_summaries"), list) else []
    detail_rows = report["detail_rows"] if isinstance(report.get("detail_rows"), list) else []

    if selected_site and selected_summary is None:
        selected_notice = f'<section class="error"><p>Site not found in Staples usage report: {html.escape(selected_site)}</p></section>'
    else:
        selected_notice = ""

    return f"""
    <header class="compact-list-header">
      <h1>Site Orders</h1>
    </header>
    <section class="list-toolbar" aria-label="Site order filters">
      {render_site_filter(summaries, selected_site)}
    </section>
    {selected_notice}
    {render_selected_site(selected_summary, sku_rows, detail_rows) if selected_summary else render_site_summary_table(summaries)}
    """


def load_site_orders_report(*, selected_site: str = "") -> dict[str, object]:
    try:
        summaries = site_summaries()
        selected_summary = next((item for item in summaries if str(item.get("ship_to_name") or "") == selected_site), None) if selected_site else None
        sku_rows = site_sku_summaries(selected_site) if selected_site else []
        detail_rows = site_detail_rows(selected_site) if selected_site else []
        return {
            "available": True,
            "site_summaries": summaries,
            "selected_summary": selected_summary,
            "sku_summaries": sku_rows,
            "detail_rows": detail_rows,
        }
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade, not 500.
        return {"available": False, "reason": f"Reference database unavailable: {exc.__class__.__name__}"}


def site_summaries() -> list[dict[str, object]]:
    return staples_reads.site_summaries(reference_find_all_func=reference_find_all)


def site_sku_summaries(ship_to_name: str) -> list[dict[str, object]]:
    return staples_reads.site_sku_summaries(ship_to_name, reference_find_all_func=reference_find_all)


def site_detail_rows(ship_to_name: str) -> list[dict[str, object]]:
    return staples_reads.site_detail_rows(ship_to_name, reference_find_all_func=reference_find_all)


def dashboard_work_items(status: str = "open", limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        return 0, []
    try:
        docs = reference_find_all(
            {
                "type": DASHBOARD_WORK_ITEM_TYPE,
                "status": status,
            },
            fields=["_id", "title", "summary", "route", "created_at", "requested_by"],
            limit=max(limit, 1) * 4,
            page_cap=2,
        )
    except Exception:  # noqa: BLE001 - advisory inbox card only.
        return 0, []
    docs.sort(key=lambda item: str(item.get("created_at") or item.get("_id") or ""), reverse=True)
    rows = [
        {
            "summary": str(item.get("title") or item.get("summary") or item.get("_id") or "Dashboard work item"),
            "age_seconds": str(item.get("created_at") or ""),
            "deep_link": str(item.get("route") or "/site-orders"),
        }
        for item in docs[:limit]
    ]
    return len(docs), rows


def render_site_filter(summaries: list[dict[str, object]], selected_site: str) -> str:
    options = ['<option value="">All sites</option>']
    for summary in summaries:
        name = str(summary.get("ship_to_name") or "").strip()
        if not name:
            continue
        selected = " selected" if name == selected_site else ""
        numbers = summary.get("ship_to_numbers")
        number_text = ", ".join(str(item) for item in numbers if str(item).strip()) if isinstance(numbers, list) else ""
        label = f"{name} ({number_text})" if number_text else name
        options.append(f'<option value="{html.escape(name, quote=True)}"{selected}>{html.escape(label)}</option>')
    return f"""<form method="get" action="/site-orders" data-submit-on-change>
        <label class="toolbar-field" for="ship_to_name">
          <span>Site</span>
          <select id="ship_to_name" name="ship_to_name">{"".join(options)}</select>
        </label>
      </form>"""


def render_site_summary_table(summaries: list[dict[str, object]]) -> str:
    return f"""
    <section class="list-results">
      <h2>Sites</h2>
      {render_table(site_summary_rows(summaries), site_summary_columns(), empty_text="No Staples site usage found.")}
    </section>
    """


def render_selected_site(
    selected_summary: dict[str, object],
    sku_rows: list[dict[str, object]],
    detail_rows: list[dict[str, object]],
) -> str:
    site_name = str(selected_summary.get("ship_to_name") or "Selected site")
    return f"""
    <section>
      <h2>{html.escape(site_name)}</h2>
      {render_metric_strip(selected_summary)}
    </section>
    <section class="list-results">
      <h2>Items</h2>
      {render_table(sku_summary_rows(sku_rows), sku_summary_columns(), empty_text="No item usage found for this site.")}
    </section>
    <section class="list-results">
      <h2>Order Lines</h2>
      {render_table(detail_table_rows(detail_rows), detail_columns(), empty_text="No order lines found for this site.")}
    </section>
    """


def render_metric_strip(summary: dict[str, object]) -> str:
    metrics = (
        ("Quantity", format_quantity(summary.get("quantity"))),
        ("Est/month", format_quantity(summary.get("estimated_monthly_quantity"))),
        ("SKUs", format_quantity(summary.get("sku_count"))),
        ("Spend", format_money(summary.get("adjusted_gross_sales"))),
    )
    badges = "".join(
        f'<span class="stat-badge"><strong>{html.escape(value)}</strong><span>{html.escape(label)}</span></span>'
        for label, value in metrics
    )
    return f'<div class="stat-strip" aria-label="Selected site order totals">{badges}</div>'


def site_summary_rows(summaries: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for summary in summaries:
        name = str(summary.get("ship_to_name") or "")
        rows.append(
            {
                "ship_to_name": name,
                "ship_to_numbers": ", ".join(str(item) for item in summary.get("ship_to_numbers", []) if str(item).strip())
                if isinstance(summary.get("ship_to_numbers"), list)
                else "",
                "quantity": format_quantity(summary.get("quantity")),
                "estimated_monthly_quantity": format_quantity(summary.get("estimated_monthly_quantity")),
                "sku_count": format_quantity(summary.get("sku_count")),
                "line_count": format_quantity(summary.get("line_count")),
                "adjusted_gross_sales": format_money(summary.get("adjusted_gross_sales")),
            }
        )
    return rows


def sku_summary_rows(docs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "sku_number": str(doc.get("sku_number") or ""),
            "website_item_description": str(doc.get("website_item_description") or ""),
            "quantity": format_quantity(doc.get("quantity")),
            "estimated_monthly_quantity": format_quantity(doc.get("estimated_monthly_quantity")),
            "average_selling_price_values": format_price_values(doc.get("average_selling_price_values")),
            "adjusted_gross_sales": format_money(doc.get("adjusted_gross_sales")),
            "line_count": format_quantity(doc.get("line_count")),
        }
        for doc in docs
    ]


def detail_table_rows(docs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "row_index": str(doc.get("row_index") or ""),
            "ship_to_number": str(doc.get("ship_to_number") or ""),
            "sku_number": str(doc.get("sku_number") or ""),
            "website_item_description": str(doc.get("website_item_description") or ""),
            "quantity": format_quantity(doc.get("quantity")),
            "average_selling_price": format_money(doc.get("average_selling_price")),
            "adjusted_gross_sales": format_money(doc.get("adjusted_gross_sales")),
        }
        for doc in docs
    ]


def site_summary_columns() -> list[dict[str, object]]:
    return [
        {"key": "ship_to_name", "label": "Site", "format": _site_link, "priority": 1},
        {"key": "ship_to_numbers", "label": "Ship-To", "priority": 2},
        {"key": "quantity", "label": "Qty", "nowrap": True, "priority": 1},
        {"key": "estimated_monthly_quantity", "label": "Est/mo", "nowrap": True, "priority": 1},
        {"key": "sku_count", "label": "SKUs", "nowrap": True, "priority": 2},
        {"key": "line_count", "label": "Lines", "nowrap": True, "priority": 2},
        {"key": "adjusted_gross_sales", "label": "Spend", "nowrap": True, "priority": 1},
    ]


def sku_summary_columns() -> list[dict[str, object]]:
    return [
        {"key": "sku_number", "label": "SKU", "nowrap": True, "priority": 1},
        {"key": "website_item_description", "label": "Item", "priority": 1},
        {"key": "quantity", "label": "Qty", "nowrap": True, "priority": 1},
        {"key": "estimated_monthly_quantity", "label": "Est/mo", "nowrap": True, "priority": 1},
        {"key": "average_selling_price_values", "label": "Avg Price", "nowrap": True, "priority": 2},
        {"key": "adjusted_gross_sales", "label": "Spend", "nowrap": True, "priority": 1},
        {"key": "line_count", "label": "Lines", "nowrap": True, "priority": 3},
    ]


def detail_columns() -> list[dict[str, object]]:
    return [
        {"key": "row_index", "label": "Row", "nowrap": True, "priority": 3},
        {"key": "ship_to_number", "label": "Ship-To", "nowrap": True, "priority": 2},
        {"key": "sku_number", "label": "SKU", "nowrap": True, "priority": 1},
        {"key": "website_item_description", "label": "Item", "priority": 1},
        {"key": "quantity", "label": "Qty", "nowrap": True, "priority": 1},
        {"key": "average_selling_price", "label": "Avg", "nowrap": True, "priority": 2},
        {"key": "adjusted_gross_sales", "label": "Spend", "nowrap": True, "priority": 1},
    ]


def _site_link(value: object, _item: dict[str, object]) -> str:
    name = str(value or "")
    return f'<a href="/site-orders?ship_to_name={quote(name)}">{html.escape(name)}</a>'


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_quantity(value: object) -> str:
    number = _float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def format_money(value: object) -> str:
    return f"${_float(value):,.2f}"


def format_price_values(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(format_money(item) for item in value)
    return format_money(value)
