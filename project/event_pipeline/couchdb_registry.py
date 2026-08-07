from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.site_registry_data import (
    BUILTIN_SANDBOX_SITE,
    BUILTIN_SANDBOX_VISION_CONTEXT,
)


DEFAULT_VAULT_DB = couchdb_config.DEFAULT_VAULT_DB


class CouchDBRegistryError(Exception):
    pass


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def word_overlap_ratio(text: str, alias: str) -> float:
    text_words = set(normalize_for_match(text).split())
    alias_words = set(normalize_for_match(alias).split())
    if not alias_words:
        return 0.0
    return len(text_words & alias_words) / len(alias_words)


@dataclass(frozen=True)
class SiteRow:
    alias: str
    site_id: str
    canonical: str
    note_path: str | None
    vision_context: dict[str, Any] | None


def _builtin_alias_rows() -> list[SiteRow]:
    """SANDBOX rows for when the views carry no SANDBOX doc.

    The demo persona is code-canonical: it must resolve (fc site picker, token
    authorization) without a location_SANDBOX doc polluting btq_vault. A real
    view row for the same site_id overrides these."""
    return [
        SiteRow(
            alias=normalize_for_match(str(alias)),
            site_id=str(BUILTIN_SANDBOX_SITE["site_id"]),
            canonical=str(BUILTIN_SANDBOX_SITE["canonical"]),
            note_path=str(BUILTIN_SANDBOX_SITE["note_path"]),
            vision_context=dict(BUILTIN_SANDBOX_VISION_CONTEXT),
        )
        for alias in BUILTIN_SANDBOX_SITE["aliases"]
    ]


def _builtin_site_id_row(site_id: str) -> SiteRow | None:
    if site_id != str(BUILTIN_SANDBOX_SITE["site_id"]):
        return None
    return _builtin_alias_rows()[0]


class CouchDBSiteRegistry:
    """
    Queries the btq_vault sites_by_alias / sites_by_site_id views for site
    resolution. Site registration lives on the canonical ``location_<site_id>``
    docs (fields: site_id, aliases, capture_active, vision_context, note_path,
    capture_guidance, display_categories) — the old sites database is retired.

    SANDBOX is the one code-canonical site: the demo persona is deliberately
    kept out of btq_vault (mirroring site_detail._builtin_location_doc), so a
    builtin row backs it whenever the views do not provide one.

    Thread-safe for read access. Results are cached for the duration
    of one watcher cycle (caller controls cache lifetime by instantiating
    a new registry per cycle, or by calling invalidate()).

    Environment configuration:
    BTQ_COUCHDB_URL defaults to http://127.0.0.1:5984.
    BTQ_COUCHDB_USER and BTQ_COUCHDB_PASSWORD default to empty, meaning no auth.
    BTQ_COUCHDB_VAULT_DB defaults to btq_vault.
    BTQ_COUCHDB_TIMEOUT defaults to 10 seconds.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        timeout: float | None = None,
    ) -> None:
        try:
            config = couchdb_config.from_env(
                base_url_override=base_url,
                username_override=username,
                password_override=password,
                timeout_override=timeout,
            )
        except couchdb_config.CouchDBConfigError as exc:
            raise CouchDBRegistryError(str(exc)) from exc
        self.base_url = config.base_url
        self.username = config.username
        self.password = config.password
        self.database = couchdb_config.vault_database(database)
        self.timeout = config.timeout
        self._auth_header = config.auth_header()
        self._lock = threading.RLock()
        self._alias_rows: list[SiteRow] | None = None
        self._site_id_rows: dict[str, SiteRow | None] = {}

    def resolve_site(self, text: str) -> tuple[str, str]:
        """
        Same contract as sites.resolve_site().
        Returns (canonical_name, confidence) where confidence is "high",
        "medium", or "low".

        Resolution order:
        1. Exact alias match -> "high"
        2. Alias substring in text -> "medium"
        3. Word overlap >= 0.7 -> "medium"
        4. No match -> ("unknown", "low")

        Queries the by_alias view. All rows are fetched once per instance
        and cached. Subsequent calls use the cache.
        """
        normalized_text = normalize_for_match(text)
        rows = self._get_alias_rows()

        for row in rows:
            if normalized_text == row.alias:
                return row.canonical, "high"

        for row in rows:
            if row.alias and row.alias in normalized_text:
                return row.canonical, "medium"

        for row in rows:
            if word_overlap_ratio(normalized_text, row.alias) >= 0.7:
                return row.canonical, "medium"

        return "unknown", "low"

    def resolve_site_id(self, site_name: str) -> str | None:
        """Same contract as sites.resolve_site_id()."""
        normalized_name = normalize_for_match(site_name)
        for row in self._get_alias_rows():
            if normalize_for_match(row.canonical) == normalized_name:
                return row.site_id
        return None

    def resolve_site_note_path(self, site_name: str) -> str | None:
        """Same contract as sites.resolve_site_note_path()."""
        normalized_name = normalize_for_match(site_name)
        for row in self._get_alias_rows():
            if normalize_for_match(row.canonical) == normalized_name:
                return row.note_path
        return None

    def get_vision_context(self, site_id: str) -> dict[str, Any] | None:
        """
        Returns the vision_context sub-object for the given site_id string,
        or None if not found. Queries by_site_id view.
        """
        row = self._get_site_id_row(str(site_id))
        if row is None:
            return None
        return row.vision_context

    def get_capture_guidance(self, site_id: str) -> str | None:
        """Return the per-site capture_guidance string from the location doc, or None if absent/empty."""
        doc = self._get_site_doc(str(site_id))
        guidance = doc.get("capture_guidance") if isinstance(doc, dict) else None
        if isinstance(guidance, str) and guidance.strip():
            return guidance.strip()
        return None

    def get_display_categories(self, site_id: str) -> list[dict[str, str]] | None:
        """Return the per-site display_categories list from the location doc, or None if absent/empty/malformed."""
        doc = self._get_site_doc(str(site_id))
        categories = doc.get("display_categories") if isinstance(doc, dict) else None
        if not isinstance(categories, list) or not categories:
            return None
        cleaned: list[dict[str, str]] = []
        for item in categories:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            canonical = str(item.get("canonical") or "").strip()
            if label and canonical:
                cleaned.append({"label": label, "canonical": canonical})
        return cleaned or None

    def resolve_canonical(self, site_id: str) -> str | None:
        """
        Returns the canonical site name for the given site_id string,
        or None if not found. Queries by_site_id view.
        """
        row = self._get_site_id_row(str(site_id))
        if row is None:
            return None
        return row.canonical

    def list_sites(self) -> list[dict[str, str]]:
        """Public: distinct sites as {site_id, canonical}, sorted by canonical name."""
        by_id: dict[str, str] = {}
        for row in self._get_alias_rows():
            if row.site_id and row.canonical:
                by_id[str(row.site_id)] = str(row.canonical)
        return [
            {"site_id": site_id, "canonical": canonical}
            for site_id, canonical in sorted(by_id.items(), key=lambda item: item[1].lower())
        ]

    def invalidate(self) -> None:
        """Clear the cached view results."""
        with self._lock:
            self._alias_rows = None
            self._site_id_rows = {}

    def _get_alias_rows(self) -> list[SiteRow]:
        with self._lock:
            if self._alias_rows is None:
                payload = self._request_json(self._view_url("sites_by_alias"))
                rows = self._parse_alias_rows(payload)
                view_site_ids = {row.site_id for row in rows}
                rows.extend(row for row in _builtin_alias_rows() if row.site_id not in view_site_ids)
                self._alias_rows = rows
            return list(self._alias_rows)

    def _get_site_id_row(self, site_id: str) -> SiteRow | None:
        with self._lock:
            if site_id not in self._site_id_rows:
                query = parse.urlencode({"key": json.dumps(site_id)})
                payload = self._request_json(f"{self._view_url('sites_by_site_id')}?{query}")
                rows = payload.get("rows")
                if not isinstance(rows, list) or not rows:
                    self._site_id_rows[site_id] = _builtin_site_id_row(site_id)
                else:
                    self._site_id_rows[site_id] = self._row_from_value(str(rows[0].get("key", site_id)), rows[0].get("value"))
            return self._site_id_rows[site_id]

    def _view_url(self, view_name: str) -> str:
        db = parse.quote(self.database, safe="")
        view = parse.quote(view_name, safe="")
        return f"{self.base_url}/{db}/_design/btq_vault/_view/{view}"

    def _doc_url(self, doc_id: str) -> str:
        db = parse.quote(self.database, safe="")
        doc = parse.quote(doc_id, safe="")
        return f"{self.base_url}/{db}/{doc}"

    def _get_site_doc(self, site_id: str) -> dict[str, Any] | None:
        doc_id = f"location_{site_id.strip()}"
        try:
            return self._request_json(self._doc_url(doc_id))
        except CouchDBRegistryError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def _request_json(self, url: str) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        headers.update(self._auth_header)
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                status = getattr(response, "status", getattr(response, "code", 200))
                body = response.read()
        except error.HTTPError as exc:
            raise CouchDBRegistryError(f"CouchDB request failed with HTTP {exc.code}: {url}") from exc
        except (error.URLError, OSError) as exc:
            raise CouchDBRegistryError(f"CouchDB request failed: {url}") from exc
        if not 200 <= int(status) < 300:
            raise CouchDBRegistryError(f"CouchDB request failed with HTTP {status}: {url}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBRegistryError(f"CouchDB returned invalid JSON: {url}") from exc
        if not isinstance(payload, dict):
            raise CouchDBRegistryError(f"CouchDB returned non-object JSON: {url}")
        return payload

    def _parse_alias_rows(self, payload: dict[str, Any]) -> list[SiteRow]:
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise CouchDBRegistryError("CouchDB by_alias view returned no rows list")
        parsed: list[SiteRow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed.append(self._row_from_value(str(row.get("key", "")), row.get("value")))
        return parsed

    def _row_from_value(self, alias: str, value: Any) -> SiteRow:
        if not isinstance(value, dict):
            raise CouchDBRegistryError("CouchDB site view row has non-object value")
        site_id = str(value.get("site_id", "")).strip()
        canonical = str(value.get("canonical", "")).strip()
        note_path_raw = value.get("note_path")
        note_path = str(note_path_raw).strip() if note_path_raw is not None else None
        vision_context_raw = value.get("vision_context")
        vision_context = vision_context_raw if isinstance(vision_context_raw, dict) else None
        if not site_id or not canonical:
            raise CouchDBRegistryError("CouchDB site view row is missing site_id or canonical")
        return SiteRow(
            alias=normalize_for_match(alias),
            site_id=site_id,
            canonical=canonical,
            note_path=note_path,
            vision_context=vision_context,
        )
