from __future__ import annotations

from typing import Any


class RecordingVaultStore:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.patch_status_calls: list[tuple[str, str, dict[str, Any] | None, bool]] = []
        self.patch_fields_calls: list[tuple[str, dict[str, Any], bool]] = []

    def upsert(self, doc: dict[str, Any]) -> None:
        doc_id = doc.get("_id")
        for index, existing in enumerate(self.docs):
            if existing.get("_id") == doc_id:
                merged = dict(doc)
                if existing.get("created_at") and not doc.get("created_at"):
                    merged["created_at"] = existing["created_at"]
                elif existing.get("created_at") and doc.get("created_at"):
                    merged["created_at"] = existing["created_at"]
                existing_job_ids = _string_list(existing.get("btq_job_ids"))
                for job_id in _string_list(doc.get("btq_job_ids")):
                    if job_id not in existing_job_ids:
                        existing_job_ids.append(job_id)
                if existing_job_ids:
                    merged["btq_job_ids"] = existing_job_ids
                self.docs[index] = merged
                return
        self.docs.append(dict(doc))

    def iter_by_type(self, type_name: str):
        for doc in self.docs:
            if doc.get("type") == type_name:
                yield doc

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        employees = [dict(doc) for doc in self.docs if doc.get("type") == "employee"]
        return employees[:limit]

    def find_visit_docs(self, site_id: str, date: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        visits = [
            dict(doc)
            for doc in self.docs
            if doc.get("type") == "visit" and str(doc.get("site_id")) == site_id and str(doc.get("date")) == date
        ]
        return visits[:limit]

    def find_open_site_issue_docs(self, site_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        site_id_variants = {str(site_id), f'"{site_id}"'}
        matches = [
            {
                "_id": doc.get("_id"),
                "issue_id": doc.get("issue_id"),
                "title": doc.get("title"),
                "status": doc.get("status"),
            }
            for doc in self.docs
            if doc.get("type") == "site_issue"
            and str(doc.get("site_id")) in site_id_variants
            and str(doc.get("status") or "").strip() == "open"
        ]
        return matches[:limit]

    def find_supply_need_docs_by_supply_id(self, supply_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        matches = [
            dict(doc)
            for doc in self.docs
            if doc.get("type") == "supply_need" and str(doc.get("supply_id")) == str(supply_id)
        ]
        return matches[:limit]

    def find_equipment_request_docs_by_equipment_id(self, equipment_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        matches = [
            dict(doc)
            for doc in self.docs
            if doc.get("type") == "equipment_request" and str(doc.get("equipment_id")) == str(equipment_id)
        ]
        return matches[:limit]

    def patch_status(
        self,
        doc_id: str,
        status: str,
        extra_fields: dict[str, Any] | None = None,
        *,
        require_existing: bool = False,
    ) -> None:
        self.patch_status_calls.append((doc_id, status, extra_fields, require_existing))
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                doc["status"] = status
                if extra_fields:
                    doc.update(extra_fields)
                return
        if require_existing:
            raise RuntimeError(f"missing required doc: {doc_id}")

    def patch_fields(self, doc_id: str, fields: dict[str, Any], *, require_existing: bool = False) -> None:
        self.patch_fields_calls.append((doc_id, dict(fields), require_existing))
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                doc.update(fields)
                return
        if require_existing:
            raise RuntimeError(f"missing required doc: {doc_id}")

    def delete_fields(self, doc_id: str, field_names: list[str], *, require_existing: bool = False) -> None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                for field in field_names:
                    doc.pop(field, None)
                return
        if require_existing:
            raise RuntimeError(f"missing required doc: {doc_id}")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
