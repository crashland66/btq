from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from btq_vault.couch_store import CouchDBEntityStore
from config import get_config
from io_atomic import atomic_write_text
from supply_order_markdown import write_supply_order_markdown
from vault_markdown import frontmatter_float, frontmatter_list


CURRENCY_PATTERN = re.compile(r"\$?\s*([0-9][0-9,]*\.\d{2})")
DATE_PATTERNS = (
    re.compile(r"order\s+date[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", re.IGNORECASE),
    re.compile(r"placed\s+on[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", re.IGNORECASE),
)
ORDER_NUMBER_PATTERNS = (
    re.compile(r"order\s*(?:number|#)[:\s]+([A-Z0-9-]+)", re.IGNORECASE),
    re.compile(r"staples(?:\.com)?\s+order\s*#?\s*([A-Z0-9-]+)", re.IGNORECASE),
)
PO_NUMBER_PATTERNS = (
    re.compile(r"po\s*(?:number|#)[:\s]+([A-Z0-9-]+)", re.IGNORECASE),
    re.compile(r"purchase\s+order[:\s]+([A-Z0-9-]+)", re.IGNORECASE),
)
TOTAL_LABELS = {
    "subtotal": re.compile(r"^subtotal$", re.IGNORECASE),
    "tax": re.compile(r"^(estimated\s+)?tax$", re.IGNORECASE),
    "shipping": re.compile(r"^(shipping|delivery)$", re.IGNORECASE),
    "total": re.compile(r"^(order\s+)?total$", re.IGNORECASE),
}
STOP_ADDRESS_HEADERS = {
    "billing address",
    "payment method",
    "items ordered",
    "order summary",
    "delivery method",
    "shipping method",
    "subtotal",
    "total",
}


@dataclass(frozen=True)
class SupplyItem:
    sku: str
    description: str
    quantity: float
    unit_price: float
    extended_price: float

    def to_record(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "description": self.description,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "extended_price": self.extended_price,
        }


@dataclass(frozen=True)
class SupplyOrder:
    type: str
    vendor: str
    order_number: str
    order_date: str
    po_number: str | None
    site_name_raw: str
    site_id: str | None
    account: str | None
    subtotal: float
    tax: float
    shipping: float
    total: float
    items: list[SupplyItem]
    source_email_subject: str
    source_email_date: str
    extraction_confidence: float
    unresolved_reason: str | None
    _address_block: str = field(default="", repr=False, compare=False)

    def to_record(self) -> dict[str, object]:
        return {
            "type": self.type,
            "vendor": self.vendor,
            "order_number": self.order_number,
            "order_date": self.order_date,
            "po_number": self.po_number,
            "site_name_raw": self.site_name_raw,
            "site_id": self.site_id,
            "account": self.account,
            "subtotal": self.subtotal,
            "tax": self.tax,
            "shipping": self.shipping,
            "total": self.total,
            "items": [item.to_record() for item in self.items],
            "source_email_subject": self.source_email_subject,
            "source_email_date": self.source_email_date,
            "extraction_confidence": self.extraction_confidence,
            "unresolved_reason": self.unresolved_reason,
        }

    def with_resolution(self, site_id: str | None, account: str | None, unresolved_reason: str | None) -> "SupplyOrder":
        return SupplyOrder(
            type=self.type,
            vendor=self.vendor,
            order_number=self.order_number,
            order_date=self.order_date,
            po_number=self.po_number,
            site_name_raw=self.site_name_raw,
            site_id=site_id,
            account=account,
            subtotal=self.subtotal,
            tax=self.tax,
            shipping=self.shipping,
            total=self.total,
            items=self.items,
            source_email_subject=self.source_email_subject,
            source_email_date=self.source_email_date,
            extraction_confidence=self.extraction_confidence,
            unresolved_reason=unresolved_reason,
            _address_block=self._address_block,
        )


@dataclass(frozen=True)
class SiteMetadata:
    site_id: str
    canonical_name: str
    account: str | None
    aliases: list[str]
    address: str
    monthly_supply_budget: float | None
    budget_basis: str | None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def normalize_identifier(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def parse_currency(value: str) -> float:
    match = CURRENCY_PATTERN.search(value.replace(",", ""))
    if match is None:
        return 0.0
    return float(match.group(1).replace(",", ""))


def parse_email_date(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone().isoformat()


def parse_order_date(value: str) -> str:
    return datetime.strptime(value, "%m/%d/%Y").date().isoformat()


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    remainder = text[4:]
    closing = remainder.find("\n---\n")
    if closing == -1:
        return {}
    frontmatter = remainder[:closing]
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def parse_frontmatter_list(value: str | None) -> list[str]:
    if value is None:
        return []
    stripped = value.strip()
    if not stripped:
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        parts = [item.strip().strip("'\"") for item in inner.split(",")]
        return [part for part in parts if part]
    if "," in stripped:
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return [stripped]


def parse_optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def extract_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    for line in soup.get_text("\n").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return lines


def extract_first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return match.group(1).strip()
    return None


def extract_shipping_address_block(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if normalize_text(line) != "shipping address":
            continue
        collected: list[str] = []
        for next_line in lines[index + 1 :]:
            if normalize_text(next_line) in STOP_ADDRESS_HEADERS:
                break
            if next_line in collected:
                continue
            collected.append(next_line)
        return "\n".join(collected).strip()
    return ""


def extract_totals(lines: list[str]) -> dict[str, float]:
    totals = {"subtotal": 0.0, "tax": 0.0, "shipping": 0.0, "total": 0.0}
    for index, line in enumerate(lines):
        normalized = normalize_text(line)
        for label, pattern in TOTAL_LABELS.items():
            if not pattern.match(normalized):
                continue
            amount = 0.0
            if index + 1 < len(lines):
                amount = parse_currency(lines[index + 1])
            if amount == 0.0:
                amount = parse_currency(line)
            totals[label] = amount
    return totals


def detect_item_header(row: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(row):
        normalized = normalize_text(cell)
        if "sku" in normalized or "item #" in normalized or normalized == "item":
            mapping["sku"] = index
        elif "description" in normalized or "item description" in normalized:
            mapping["description"] = index
        elif "qty" in normalized or "quantity" in normalized:
            mapping["quantity"] = index
        elif "unit price" in normalized or normalized == "price":
            mapping["unit_price"] = index
        elif "amount" in normalized or "extended" in normalized or "total" == normalized:
            mapping["extended_price"] = index
    required = {"description", "quantity"}
    if not required.issubset(mapping.keys()):
        return None
    return mapping


def parse_float_token(value: str) -> float:
    cleaned = re.sub(r"[^0-9.]", "", value)
    if not cleaned:
        return 0.0
    return float(cleaned)


def parse_item_from_row(row: list[str], mapping: dict[str, int]) -> SupplyItem | None:
    description = row[mapping["description"]].strip() if mapping["description"] < len(row) else ""
    if not description:
        return None
    quantity_value = row[mapping["quantity"]] if mapping["quantity"] < len(row) else "0"
    quantity = parse_float_token(quantity_value)
    sku = row[mapping.get("sku", -1)].strip() if mapping.get("sku", -1) < len(row) and mapping.get("sku", -1) >= 0 else ""
    unit_price = parse_currency(row[mapping["unit_price"]]) if mapping.get("unit_price", -1) < len(row) and mapping.get("unit_price", -1) >= 0 else 0.0
    extended_price = parse_currency(row[mapping["extended_price"]]) if mapping.get("extended_price", -1) < len(row) and mapping.get("extended_price", -1) >= 0 else 0.0
    if extended_price == 0.0 and quantity and unit_price:
        extended_price = round(quantity * unit_price, 2)
    if unit_price == 0.0 and quantity and extended_price:
        unit_price = round(extended_price / quantity, 2)
    if quantity == 0.0 and extended_price and unit_price:
        quantity = round(extended_price / unit_price, 2)
    return SupplyItem(
        sku=sku,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        extended_price=extended_price,
    )


def extract_items(html: str) -> list[SupplyItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[SupplyItem] = []
    for table in soup.find_all("table"):
        rows = [
            [re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip() for cell in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        header_index = None
        mapping = None
        for index, row in enumerate(rows):
            header_mapping = detect_item_header(row)
            if header_mapping is not None:
                header_index = index
                mapping = header_mapping
                break
        if header_index is None or mapping is None:
            continue
        for row in rows[header_index + 1 :]:
            if not any(cell.strip() for cell in row):
                continue
            first_cell = normalize_text(row[0])
            if first_cell in {"subtotal", "tax", "shipping", "total"}:
                break
            item = parse_item_from_row(row, mapping)
            if item is not None:
                items.append(item)
        if items:
            return items
    return items


def fallback_order_number(subject: str, date: datetime, html: str) -> str:
    digest = hashlib.sha256(f"{subject}|{date.isoformat()}|{normalize_text(html)}".encode("utf-8")).hexdigest()[:12]
    return f"unknown-{digest}"


def parse_staples_email(html: str, subject: str, date: datetime) -> SupplyOrder:
    lines = extract_text_lines(html)
    full_text = "\n".join(lines)
    order_number = extract_first_match(ORDER_NUMBER_PATTERNS, full_text)
    order_date_raw = extract_first_match(DATE_PATTERNS, full_text)
    po_number = extract_first_match(PO_NUMBER_PATTERNS, full_text)
    totals = extract_totals(lines)
    items = extract_items(html)
    address_block = extract_shipping_address_block(lines)
    site_name_raw = address_block.splitlines()[0].strip() if address_block else ""

    missing_fields: list[str] = []
    if order_number is None:
        missing_fields.append("order_number")
        order_number = fallback_order_number(subject, date, html)
    if order_date_raw is None:
        missing_fields.append("order_date")
        order_date = date.date().isoformat()
    else:
        order_date = parse_order_date(order_date_raw)
    if not address_block:
        missing_fields.append("shipping_address")
    if not items:
        missing_fields.append("items")
    if totals["total"] == 0.0:
        missing_fields.append("total")

    confidence = max(0.0, 1.0 - (0.15 * len(missing_fields)))
    unresolved_reason = None if not missing_fields else ",".join(sorted(missing_fields))

    return SupplyOrder(
        type="supply_order",
        vendor="Staples",
        order_number=order_number,
        order_date=order_date,
        po_number=po_number,
        site_name_raw=site_name_raw,
        site_id=None,
        account=None,
        subtotal=totals["subtotal"],
        tax=totals["tax"],
        shipping=totals["shipping"],
        total=totals["total"],
        items=items,
        source_email_subject=subject,
        source_email_date=parse_email_date(date),
        extraction_confidence=confidence,
        unresolved_reason=unresolved_reason,
        _address_block=address_block,
    )


def _location_doc_site_id(doc: dict[str, Any]) -> str:
    return str(doc.get("job") or str(doc["_id"]).removeprefix("location_"))


def _location_doc_budget(doc: dict[str, Any]) -> float | None:
    budget = frontmatter_float(doc, "monthly_supply_budget")
    if budget is not None:
        return budget
    return frontmatter_float(doc, "supply_budget_monthly")


def _site_metadata_from_location_doc(doc: dict[str, Any]) -> SiteMetadata:
    site_id = _location_doc_site_id(doc)
    canonical_name = str(doc.get("location") or "")
    aliases = {
        site_id,
        canonical_name,
        f"{site_id} - {canonical_name}",
        *frontmatter_list(doc.get("site_aliases")),
        *frontmatter_list(doc.get("aliases")),
    }
    return SiteMetadata(
        site_id=site_id,
        canonical_name=canonical_name,
        account=doc.get("account"),
        aliases=sorted(alias for alias in aliases if alias),
        address=str(doc.get("address") or ""),
        monthly_supply_budget=_location_doc_budget(doc),
        budget_basis=doc.get("budget_basis"),
    )


def load_site_metadata(store: Any | None = None) -> list[SiteMetadata]:
    resolved_store = CouchDBEntityStore.from_env() if store is None else store
    return sorted(
        (_site_metadata_from_location_doc(doc) for doc in resolved_store.find_location_docs()),
        key=lambda site: site.site_id,
    )


def overlap_ratio(left: str, right: str) -> float:
    left_words = set(normalize_text(left).split())
    right_words = set(normalize_text(right).split())
    if not left_words or not right_words:
        return 0.0
    intersection = len(left_words & right_words)
    return intersection / max(len(left_words), len(right_words))


def resolve_site(site_name_raw: str, address_block: str, store: Any | None = None) -> tuple[str | None, float]:
    normalized_name = normalize_text(site_name_raw)
    normalized_address = normalize_text(address_block)
    best_site_id: str | None = None
    best_score = 0.0

    for site in load_site_metadata(store):
        site_score = 0.0
        for alias in site.aliases:
            normalized_alias = normalize_text(alias)
            if not normalized_alias:
                continue
            if normalized_name and normalized_name == normalized_alias:
                site_score = max(site_score, 1.0)
            elif normalized_name and (normalized_alias in normalized_name or normalized_name in normalized_alias):
                site_score = max(site_score, 0.85)
            elif normalized_name:
                site_score = max(site_score, overlap_ratio(normalized_name, normalized_alias))
        if normalized_address and site.address:
            address_score = overlap_ratio(normalized_address, site.address)
            if normalize_text(site.address) in normalized_address:
                address_score = max(address_score, 0.8)
            site_score = max(site_score, address_score)
        if site_score > best_score:
            best_score = site_score
            best_site_id = site.site_id

    if best_score < 0.7:
        return None, best_score
    return best_site_id, best_score


def site_metadata_by_id(site_id: str, store: Any | None = None) -> SiteMetadata | None:
    resolved_store = CouchDBEntityStore.from_env() if store is None else store
    doc = resolved_store.get_optional(f"location_{site_id}")
    if doc is None:
        return None
    return _site_metadata_from_location_doc(doc)


def data_root(project_root: Path) -> Path:
    return project_root.parent / "data"


def record_output_path(base_dir: Path, order: SupplyOrder) -> Path:
    year, month, _day = order.order_date.split("-")
    return base_dir / "supply_orders" / year / month / f"{normalize_identifier(order.order_number)}.json"


def quarantine_output_path(base_dir: Path, order_identifier: str, order_date: str) -> Path:
    year, month, _day = order_date.split("-")
    return base_dir / "supply_orders" / "quarantine" / year / month / f"{normalize_identifier(order_identifier)}.json"


def persist_supply_order(order: SupplyOrder, project_root: Path, vault_root: Path, store: Any | None = None) -> tuple[Path, Path | None]:
    base_dir = data_root(project_root)
    json_path = record_output_path(base_dir, order)
    site = site_metadata_by_id(order.site_id, store) if order.site_id is not None else None
    atomic_write_text(json_path, json.dumps(order.to_record(), indent=2, sort_keys=True) + "\n")
    markdown_path = None if site is None or getattr(site, "about_path", None) is None else write_supply_order_markdown(order, site)
    return json_path, markdown_path


def quarantine_record(payload: dict[str, object], project_root: Path, identifier: str, order_date: str) -> Path:
    path = quarantine_output_path(data_root(project_root), identifier, order_date)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def calculate_budget_variance(site_id: str, month: str, store: Any | None = None, base_dir: Path | None = None) -> dict[str, float | None]:
    resolved_site = site_metadata_by_id(site_id, store)
    if resolved_site is None:
        raise ValueError(f"Unknown site_id: {site_id}")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise ValueError(f"Invalid month: {month}")
    year, month_value = month.split("-")
    root = data_root(get_config().project_dir) if base_dir is None else base_dir
    month_dir = root / "supply_orders" / year / month_value
    actual_spend = 0.0
    if month_dir.exists():
        for path in sorted(month_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            if str(record.get("site_id") or "") != str(site_id):
                continue
            actual_spend += float(record.get("total", 0.0))
    budgeted_amount = resolved_site.monthly_supply_budget
    variance = None if budgeted_amount is None else actual_spend - budgeted_amount
    variance_percent = None if budgeted_amount in (None, 0.0) else round((variance / budgeted_amount) * 100.0, 2)
    return {
        "total_actual_spend": round(actual_spend, 2),
        "budgeted_amount": None if budgeted_amount is None else round(budgeted_amount, 2),
        "variance": None if variance is None else round(variance, 2),
        "variance_percent": variance_percent,
    }
