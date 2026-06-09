from __future__ import annotations

import json
from typing import Any, Callable
from urllib import error, parse, request

from event_pipeline import couchdb_config


CANONICAL_EMPLOYEE_FIELDS: frozenset[str] = frozenset({
    "additional_jobs",
    "assignments",
    "employee_id",
    "employment_type",
    "first",
    "hire_date",
    "job",
    "last",
    "wage",
    "role",
    "notes",
    "phone",
})

EMPLOYEE_DUPLICATE_QUERY_LIMIT = 10000
VISIT_DEDUP_QUERY_LIMIT = 10000
UNKNOWN_CAPTURE_QUERY_LIMIT = 10000


class CouchDBEntityStoreError(Exception):
    pass


class CouchDBEntityStore:
    """Wraps btq_vault CouchDB writes with _rev CAS and 409 retry."""

    def __init__(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        database: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_headers = dict(auth_headers)
        self.database = database
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "CouchDBEntityStore":
        """Construct from BTQ_COUCHDB_* env vars."""
        config = couchdb_config.from_env()
        return cls(
            base_url=config.base_url,
            auth_headers=dict(config.auth_header()),
            database=couchdb_config.vault_database(),
            timeout=config.timeout,
        )

    @classmethod
    def for_database_from_env(cls, database: str) -> "CouchDBEntityStore":
        """Construct from BTQ_COUCHDB_* env vars for an explicit database."""
        config = couchdb_config.from_env()
        return cls(
            base_url=config.base_url,
            auth_headers=dict(config.auth_header()),
            database=database,
            timeout=config.timeout,
        )

    @staticmethod
    def enabled() -> bool:
        return True

    def upsert(self, doc: dict) -> None:
        """
        Upsert a document into btq_vault using GET-then-PUT with _rev CAS.
        Retries once on 409 Conflict. Raises CouchDBEntityStoreError on
        non-transient failures.
        doc must include '_id'. All other fields are written as-is.
        """
        doc_id = str(doc.get("_id") or "").strip()
        if not doc_id:
            raise CouchDBEntityStoreError("Cannot upsert CouchDB document without _id")

        for attempt in range(2):
            existing = self._get_doc(doc_id)
            outgoing = dict(doc)
            if existing is not None and existing.get("_rev"):
                outgoing["_rev"] = existing["_rev"]
            try:
                self._put_doc(doc_id, outgoing)
                return
            except error.HTTPError as exc:
                if exc.code == 409 and attempt == 0:
                    continue
                raise CouchDBEntityStoreError(f"CouchDB upsert failed for {doc_id} with HTTP {exc.code}") from exc
        raise CouchDBEntityStoreError(f"CouchDB upsert conflict for {doc_id}")

    def find_employee_docs(self, *, limit: int = EMPLOYEE_DUPLICATE_QUERY_LIMIT) -> list[dict[str, Any]]:
        """
        Return canonical employee docs needed for duplicate checks.

        The processor applies name normalization locally because historical
        documents may not have a precomputed normalized-name field.
        """
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": {"type": "employee"},
                "fields": [
                    "_id",
                    "type",
                    "name",
                    "first",
                    "last",
                    "employee_id",
                    "person_id",
                    "status",
                    "site_ids",
                    "job",
                    "additional_jobs",
                    "sites",
                    "vault_path",
                    "location",
                    "canonical",
                    "aliases",
                ],
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB employee duplicate query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def find_employee_docs_by_person_id(self, person_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return canonical employee docs whose person_id field matches exactly."""
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": {"type": "employee", "person_id": person_id},
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB employee person_id query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def find_visit_docs(self, site_id: str, date: str, *, limit: int = VISIT_DEDUP_QUERY_LIMIT) -> list[dict[str, Any]]:
        """Return canonical visit docs for a site/date needed for dedup checks."""
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": {"type": "visit", "site_id": site_id, "date": date},
                "fields": ["_id", "type", "site_id", "date", "evidence", "btq_job_ids"],
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB visit dedup query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def find_supply_need_docs_by_supply_id(self, supply_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return canonical supply_need docs whose supply_id field matches exactly.

        Canonical ``_id`` values are path-derived
        (``supply_need_accounts_.._<supply_id>_<slug>``), so a transition job
        carrying only the bare ``supply_id`` cannot reconstruct the ``_id`` by
        prefixing. This lookup mirrors ``locate_supply_file_by_id`` on the
        Markdown side.
        """
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": {"type": "supply_need", "supply_id": supply_id},
                "fields": ["_id", "type", "supply_id", "status"],
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB supply_need supply_id query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def find_equipment_request_docs_by_equipment_id(self, equipment_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return canonical equipment_request docs whose equipment_id field matches exactly.

        Same path-derived ``_id`` problem as supply needs; see
        ``find_supply_need_docs_by_supply_id``.
        """
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": {"type": "equipment_request", "equipment_id": equipment_id},
                "fields": ["_id", "type", "equipment_id", "status"],
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB equipment_request equipment_id query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def find_unknown_capture_docs(self, status: str | None = "unresolved", *, limit: int = UNKNOWN_CAPTURE_QUERY_LIMIT) -> list[dict[str, Any]]:
        """Return canonical unknown_capture docs for unresolved scanning."""
        selector = {"type": "unknown_capture"}
        if status is not None:
            selector["status"] = status
        response = self._request_json(
            "POST",
            "_find",
            {
                "selector": selector,
                "fields": [
                    "_id",
                    "type",
                    "status",
                    "source_unknown_id",
                    "timestamp",
                    "audio_file",
                    "retry_count",
                    "last_attempted",
                    "btq_job_ids",
                ],
                "limit": limit,
            },
        )
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise CouchDBEntityStoreError("CouchDB unknown_capture query returned no docs list")
        return [doc for doc in docs if isinstance(doc, dict)]

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        """Return the canonical doc (incl. _rev) or None if absent."""
        return self._get_doc(doc_id)

    def get_required(self, doc_id: str) -> dict[str, Any]:
        """Return the canonical doc or raise CouchDBEntityStoreError if absent."""
        doc = self.get_optional(doc_id)
        if doc is None:
            raise CouchDBEntityStoreError(f"CouchDB required document not found: {doc_id}")
        return doc

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        """
        PUT doc with the supplied expected revision.

        This is a single CAS attempt. Conflict HTTPError(409) is intentionally
        allowed to surface so callers with read-modify-write loops can refetch
        and retry.
        """
        doc_id = str(doc.get("_id") or "").strip()
        if not doc_id:
            raise CouchDBEntityStoreError("Cannot PUT CouchDB document without _id")

        outgoing = dict(doc)
        if expected_rev is None:
            outgoing.pop("_rev", None)
        else:
            outgoing["_rev"] = expected_rev

        try:
            response = self._put_doc(doc_id, outgoing)
        except error.HTTPError as exc:
            if exc.code == 409:
                raise
            raise CouchDBEntityStoreError(f"CouchDB put_with_rev failed for {doc_id} with HTTP {exc.code}") from exc

        stored = dict(outgoing)
        new_rev = response.get("rev")
        if new_rev:
            stored["_rev"] = str(new_rev)
        return stored

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        """
        Canonical read-modify-write with _rev CAS.

        Returning None from transform is a no-op and returns the unchanged
        current doc. A 409 conflict refetches and retries transform up to
        max_conflict_retries times.
        """
        for attempt in range(max_conflict_retries + 1):
            current = self.get_optional(doc_id)
            if current is None:
                if require_existing:
                    raise CouchDBEntityStoreError(f"CouchDB required document not found for update_doc: {doc_id}")
                seed = create() if create is not None else None
            else:
                seed = current

            outgoing = transform(seed)
            if outgoing is None:
                if current is not None:
                    return current
                raise CouchDBEntityStoreError(f"CouchDB update_doc no-op has no document to return: {doc_id}")

            expected_rev = current.get("_rev") if current is not None else None
            try:
                return self.put_with_rev(outgoing, expected_rev=expected_rev)
            except error.HTTPError as exc:
                if exc.code == 409 and attempt < max_conflict_retries:
                    continue
                if exc.code == 409:
                    raise CouchDBEntityStoreError(f"CouchDB update_doc conflict for {doc_id}") from exc
                raise CouchDBEntityStoreError(f"CouchDB update_doc failed for {doc_id} with HTTP {exc.code}") from exc
        raise CouchDBEntityStoreError(f"CouchDB update_doc conflict for {doc_id}")

    def patch_status(
        self,
        doc_id: str,
        status: str,
        extra_fields: dict | None = None,
        *,
        require_existing: bool = False,
    ) -> None:
        """
        Fetch the existing doc by doc_id, update its 'status' field (and any
        extra_fields), then PUT back with _rev CAS. Retries once on 409.
        Silently no-ops if the doc is not found unless require_existing is true.
        """
        fields = {"status": status}
        if extra_fields:
            fields.update(extra_fields)
        self.patch_fields(doc_id, fields, require_existing=require_existing)

    def patch_fields(self, doc_id: str, fields: dict, *, require_existing: bool = False) -> None:
        """
        Fetch the existing doc by doc_id, update the supplied fields, then PUT
        back with _rev CAS. Retries once on 409. Silently no-ops if the doc is
        not found unless require_existing is true.
        """
        for attempt in range(2):
            existing = self._get_doc(doc_id)
            if existing is None:
                if require_existing:
                    raise CouchDBEntityStoreError(f"CouchDB required document not found for patch_fields: {doc_id}")
                return
            outgoing = dict(existing)
            outgoing.update(fields)
            try:
                self._put_doc(doc_id, outgoing)
                return
            except error.HTTPError as exc:
                if exc.code == 409 and attempt == 0:
                    continue
                raise CouchDBEntityStoreError(f"CouchDB patch_fields failed for {doc_id} with HTTP {exc.code}") from exc
        raise CouchDBEntityStoreError(f"CouchDB patch_fields conflict for {doc_id}")

    def delete_fields(self, doc_id: str, field_names: list[str], *, require_existing: bool = False) -> None:
        """
        Fetch the existing doc by doc_id, remove the supplied top-level fields,
        then PUT back with _rev CAS. Retries once on 409. Silently no-ops if the
        doc is not found unless require_existing is true.
        """
        fields = [str(name) for name in field_names if str(name)]
        if not fields:
            return
        for attempt in range(2):
            existing = self._get_doc(doc_id)
            if existing is None:
                if require_existing:
                    raise CouchDBEntityStoreError(f"CouchDB required document not found for delete_fields: {doc_id}")
                return
            outgoing = dict(existing)
            for field in fields:
                outgoing.pop(field, None)
            try:
                self._put_doc(doc_id, outgoing)
                return
            except error.HTTPError as exc:
                if exc.code == 409 and attempt == 0:
                    continue
                raise CouchDBEntityStoreError(f"CouchDB delete_fields failed for {doc_id} with HTTP {exc.code}") from exc
        raise CouchDBEntityStoreError(f"CouchDB delete_fields conflict for {doc_id}")

    def _get_doc(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return self._request_json("GET", doc_id)
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise CouchDBEntityStoreError(f"CouchDB GET failed for {doc_id} with HTTP {exc.code}") from exc

    def _put_doc(self, doc_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("PUT", doc_id, doc)

    def _request_json(self, method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        headers.update(self.auth_headers)
        data = None
        if payload is not None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self._doc_url(doc_id), data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
        except error.HTTPError:
            raise
        except (error.URLError, OSError) as exc:
            raise CouchDBEntityStoreError(f"CouchDB request failed for {doc_id}") from exc
        if not 200 <= status < 300:
            raise CouchDBEntityStoreError(f"CouchDB request failed for {doc_id} with HTTP {status}")
        if not raw:
            return {}
        try:
            payload_obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBEntityStoreError("CouchDB returned invalid JSON") from exc
        if not isinstance(payload_obj, dict):
            raise CouchDBEntityStoreError("CouchDB returned non-object JSON")
        return payload_obj

    def _doc_url(self, doc_id: str) -> str:
        database = parse.quote(self.database, safe="")
        quoted_doc_id = "/".join(parse.quote(part, safe="") for part in doc_id.split("/"))
        return f"{self.base_url}/{database}/{quoted_doc_id}"
