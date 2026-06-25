from __future__ import annotations

import os
from collections.abc import Callable

from event_pipeline import couchdb_config
from voice_memo.couchdb import query_couchdb_find


REFERENCES_DB = "btq_references"
STAPLES_REPORT_ID = os.environ.get(
    "BTQ_STAPLES_USAGE_REPORT_ID",
    "staples_usage_report_2026-01-01_2026-06-25",
)
PAGE_LIMIT = 5000

CouchDBFind = Callable[[str, dict[str, object]], dict[str, object]]
ReferenceFindAll = Callable[..., list[dict[str, object]]]


def references_database() -> str:
    return (os.environ.get("BTQ_REFERENCES_DB") or REFERENCES_DB).strip() or REFERENCES_DB


def site_summaries(
    *,
    couchdb_find: CouchDBFind | None = None,
    reference_find_all_func: ReferenceFindAll | None = None,
) -> list[dict[str, object]]:
    finder = reference_find_all_func or reference_find_all
    docs = finder(
        {
            "type": "staples_usage_ship_to_summary",
            "report_id": STAPLES_REPORT_ID,
        },
        fields=[
            "ship_to_name",
            "ship_to_numbers",
            "quantity",
            "estimated_monthly_quantity",
            "adjusted_gross_sales",
            "sku_count",
            "line_count",
        ],
        couchdb_find=couchdb_find,
    )
    return sorted(docs, key=lambda item: str(item.get("ship_to_name") or ""))


def site_sku_summaries(
    ship_to_name: str,
    *,
    couchdb_find: CouchDBFind | None = None,
    reference_find_all_func: ReferenceFindAll | None = None,
) -> list[dict[str, object]]:
    finder = reference_find_all_func or reference_find_all
    docs = finder(
        {
            "type": "staples_usage_ship_to_sku_summary",
            "report_id": STAPLES_REPORT_ID,
            "ship_to_name": ship_to_name,
        },
        fields=[
            "sku_number",
            "website_item_description",
            "quantity",
            "estimated_monthly_quantity",
            "average_selling_price_values",
            "adjusted_gross_sales",
            "line_count",
        ],
        couchdb_find=couchdb_find,
    )
    return sorted(docs, key=lambda item: (-_float(item.get("adjusted_gross_sales")), str(item.get("website_item_description") or "")))


def site_detail_rows(
    ship_to_name: str,
    *,
    couchdb_find: CouchDBFind | None = None,
    reference_find_all_func: ReferenceFindAll | None = None,
) -> list[dict[str, object]]:
    finder = reference_find_all_func or reference_find_all
    docs = finder(
        {
            "type": "staples_usage_row",
            "report_id": STAPLES_REPORT_ID,
            "ship_to_name": ship_to_name,
        },
        fields=[
            "row_index",
            "ship_to_number",
            "sku_number",
            "website_item_description",
            "quantity",
            "average_selling_price",
            "adjusted_gross_sales",
        ],
        couchdb_find=couchdb_find,
    )
    return sorted(docs, key=lambda item: int(item.get("row_index") or 0))


def report_metadata(
    *,
    couchdb_find: CouchDBFind | None = None,
    reference_find_all_func: ReferenceFindAll | None = None,
) -> dict[str, object]:
    finder = reference_find_all_func or reference_find_all
    docs = finder(
        {
            "type": "staples_usage_report",
            "report_id": STAPLES_REPORT_ID,
        },
        fields=["report_id", "vendor", "period_start", "period_end", "source_file", "imported_at"],
        limit=1,
        page_cap=1,
        couchdb_find=couchdb_find,
    )
    metadata = dict(docs[0]) if docs else {}
    metadata.setdefault("report_id", STAPLES_REPORT_ID)
    metadata.setdefault("vendor", "Staples")
    return metadata


def reference_find_all(
    selector: dict[str, object],
    *,
    fields: list[str] | None = None,
    limit: int = PAGE_LIMIT,
    page_cap: int = 20,
    couchdb_find: CouchDBFind | None = None,
) -> list[dict[str, object]]:
    finder = couchdb_find or _default_couchdb_find
    docs: list[dict[str, object]] = []
    bookmark: object = None
    for _ in range(page_cap):
        mango: dict[str, object] = {"selector": selector, "limit": limit}
        if fields:
            mango["fields"] = fields
        if bookmark:
            mango["bookmark"] = bookmark
        response = finder(references_database(), mango)
        page = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(page, list):
            break
        docs.extend(item for item in page if isinstance(item, dict))
        bookmark = response.get("bookmark")
        if len(page) < limit or not bookmark:
            break
    return docs


def _default_couchdb_find(database: str, selector: dict[str, object]) -> dict[str, object]:
    return query_couchdb_find(couchdb_config.from_env(), database, selector)


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

