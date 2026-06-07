from __future__ import annotations

from pathlib import Path
from typing import Protocol

from io_atomic import atomic_write_text


class SupplyItemLike(Protocol):
    description: str
    quantity: float
    extended_price: float


class SupplyOrderLike(Protocol):
    vendor: str
    order_number: str
    order_date: str
    site_id: str | None
    total: float
    po_number: str | None
    items: list[SupplyItemLike]


class SiteMetadataLike(Protocol):
    about_path: Path
    monthly_supply_budget: float | None


def normalize_identifier(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def supply_order_filename(order: SupplyOrderLike) -> str:
    vendor_slug = normalize_identifier(order.vendor)
    order_slug = normalize_identifier(order.order_number)
    return f"{order.order_date} - {vendor_slug}-order-{order_slug}.md"


def supply_order_supplies_dir(site: SiteMetadataLike) -> Path:
    return site.about_path.parent / "Supplies"


def supply_order_markdown_path(order: SupplyOrderLike, site: SiteMetadataLike | None) -> Path | None:
    if order.site_id is None or site is None:
        return None
    return supply_order_supplies_dir(site) / supply_order_filename(order)


def format_currency(value: float) -> str:
    return f"${value:.2f}"


def format_signed_currency(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def format_quantity(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def build_item_line(item: SupplyItemLike) -> str:
    return f"- {format_quantity(item.quantity)}x {item.description} — {format_currency(item.extended_price)}"


def build_budget_lines(order: SupplyOrderLike, site: SiteMetadataLike | None) -> list[str]:
    if site is None or site.monthly_supply_budget is None:
        return []
    impact = round(order.total - site.monthly_supply_budget, 2)
    return [
        f"**Monthly Budget:** {format_currency(site.monthly_supply_budget)}  ",
        f"**Order Impact:** {format_signed_currency(impact)} vs monthly budget",
        "",
    ]


def build_supply_order_markdown(order: SupplyOrderLike, site: SiteMetadataLike | None) -> str:
    frontmatter = [
        "---",
        "type: supply_order",
        f"site_id: {order.site_id or ''}",
        f"vendor: {order.vendor}",
        f"order_number: {order.order_number}",
        f"order_date: {order.order_date}",
        f"total: {order.total:.2f}",
        f"po_number: {order.po_number or ''}",
        "source: pipeline",
        "---",
        "",
        f"# Supply Order — {order.vendor}",
        "",
        f"**Order Number:** {order.order_number}  ",
        f"**Date:** {order.order_date}  ",
        f"**Total:** {format_currency(order.total)}  ",
        "",
    ]
    body = ["## Items", ""]
    if order.items:
        body.extend(build_item_line(item) for item in order.items)
    else:
        body.append("- None")
    body.extend(["", "## Notes", "", "Auto-generated from supply order pipeline.", ""])
    lines = frontmatter[:]
    budget_lines = build_budget_lines(order, site)
    if budget_lines:
        lines.extend(budget_lines)
    lines.extend(body)
    return "\n".join(lines)


def write_supply_order_markdown(order: SupplyOrderLike, site: SiteMetadataLike | None) -> Path | None:
    path = supply_order_markdown_path(order, site)
    if path is None:
        return None
    atomic_write_text(path, build_supply_order_markdown(order, site))
    return path
