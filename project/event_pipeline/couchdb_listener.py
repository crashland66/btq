from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


class CouchDBListenerError(Exception):
    pass


class CouchDBChangesListener:
    """
    Subscribes to a CouchDB _changes continuous feed and yields claimed
    documents for processing.

    The caller is responsible for processing the yielded document and calling
    mark_processing(), mark_complete(), or mark_failed() on the listener to
    update the document state in CouchDB.

    Thread-safe: each instance owns one HTTP connection. Do not share instances
    across threads.
    """

    def __init__(
        self,
        *,
        database: str,
        state_field: str,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        heartbeat_ms: int | None = None,
        max_reconnect_attempts: int = 60,
    ) -> None:
        self.database = database
        self.state_field = state_field
        try:
            config = couchdb_config.from_env(
                base_url_override=base_url,
                username_override=username,
                password_override=password,
                timeout_override=timeout,
                heartbeat_ms_override=heartbeat_ms,
                timeout_default=couchdb_config.DEFAULT_LISTENER_TIMEOUT_SECONDS,
            )
        except couchdb_config.CouchDBConfigError as exc:
            raise CouchDBListenerError(str(exc)) from exc
        self.base_url = config.base_url
        self.username = config.username
        self.password = config.password
        self.timeout = config.timeout
        self.heartbeat_ms = config.heartbeat_ms
        self._auth_header = config.auth_header()
        self.max_reconnect_attempts = max_reconnect_attempts
        self._last_seq: Any = 0
        self._stopped = False
        self._selector_available = True

    def listen(self) -> Iterator[dict[str, Any]]:
        """
        Subscribe to _changes and yield each claimed document.

        Reconnects automatically after a heartbeat timeout or connection drop
        using the last-seen sequence number (since=<last_seq>) so no changes
        are missed.

        Stops iteration when stop() is called.

        Raises CouchDBListenerError on unrecoverable errors (auth failure,
        database not found, repeated reconnection failures).
        """
        reconnect_attempts = 0
        while not self._stopped:
            try:
                with self._open_changes_feed() as response:
                    reconnect_attempts = 0
                    while not self._stopped:
                        line = response.readline()
                        if line == b"":
                            raise error.URLError("changes feed ended")
                        if not line.strip():
                            continue
                        change = self._decode_change(line)
                        if "seq" in change:
                            self._last_seq = change["seq"]
                        doc_id = change.get("id")
                        if not doc_id:
                            continue
                        claimed = self._claim_if_pending(str(doc_id))
                        if claimed is not None:
                            yield claimed
            except error.HTTPError as exc:
                if exc.code in {400, 404} and self._selector_available:
                    self._selector_available = False
                    continue
                if exc.code in {401, 403, 404}:
                    raise CouchDBListenerError(f"CouchDB changes feed failed with HTTP {exc.code}") from exc
                reconnect_attempts = self._next_reconnect_attempt(reconnect_attempts, exc)
            except (error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                reconnect_attempts = self._next_reconnect_attempt(reconnect_attempts, exc)

    def mark_processing(self, doc: dict[str, Any], state: str = "processing") -> dict[str, Any]:
        """
        Update state_field to the processing state using optimistic CAS.
        Returns the updated doc with the new _rev.
        Raises CouchDBListenerError on conflict or HTTP error.
        """
        return self._update_state(doc, state)

    def mark_complete(self, doc: dict[str, Any], state: str = "complete") -> None:
        """Update state_field to the complete state."""
        self._update_state(doc, state)

    def mark_failed(self, doc: dict[str, Any], reason: str, state: str = "failed") -> None:
        """
        Update state_field to the failed state.
        Write reason into doc["error_reason"] in the same update.
        """
        self._update_state(doc, state, {"error_reason": reason})

    def stop(self) -> None:
        """Signal the listen() loop to stop after the current document."""
        self._stopped = True

    def _open_changes_feed(self) -> Any:
        params = {
            "feed": "continuous",
            "include_docs": "false",
            "heartbeat": str(self.heartbeat_ms),
            "since": json.dumps(self._last_seq) if not isinstance(self._last_seq, str) else self._last_seq,
        }
        if self._selector_available:
            params["filter"] = "_selector"
            payload = {"selector": {self.state_field: "pending"}}
            return self._open("POST", "_changes", params=params, payload=payload)
        return self._open("GET", "_changes", params=params)

    def _claim_if_pending(self, doc_id: str) -> dict[str, Any] | None:
        doc = self._request_json("GET", doc_id)
        if doc.get(self.state_field) != "pending":
            return None
        claimed_doc = dict(doc)
        claimed_doc[self.state_field] = "claimed"
        try:
            result = self._request_json("PUT", doc_id, claimed_doc)
        except error.HTTPError as exc:
            if exc.code == 409:
                return None
            raise
        rev = result.get("rev")
        if rev:
            claimed_doc["_rev"] = rev
        return claimed_doc

    def _update_state(self, doc: dict[str, Any], state: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if not doc.get("_id") or not doc.get("_rev"):
            raise CouchDBListenerError("Cannot update CouchDB document without _id and _rev")
        updated = dict(doc)
        updated[self.state_field] = state
        if extra:
            updated.update(extra)
        try:
            result = self._request_json("PUT", str(updated["_id"]), updated)
        except error.HTTPError as exc:
            if exc.code == 409:
                raise CouchDBListenerError("CouchDB document update conflict") from exc
            raise CouchDBListenerError(f"CouchDB document update failed with HTTP {exc.code}") from exc
        rev = result.get("rev")
        if rev:
            updated["_rev"] = rev
        return updated

    def _request_json(self, method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with self._open(method, doc_id, payload=payload) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise CouchDBListenerError(f"CouchDB request failed with HTTP {exc.code}") from exc
            raise
        except (error.URLError, OSError) as exc:
            raise CouchDBListenerError("CouchDB request failed") from exc
        if not 200 <= status < 300:
            raise CouchDBListenerError(f"CouchDB request failed with HTTP {status}")
        try:
            payload_obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBListenerError("CouchDB returned invalid JSON") from exc
        if not isinstance(payload_obj, dict):
            raise CouchDBListenerError("CouchDB returned non-object JSON")
        return payload_obj

    def _open(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        db = parse.quote(self.database, safe="")
        quoted_path = "/".join(parse.quote(part, safe="") for part in path.split("/"))
        url = f"{self.base_url}/{db}/{quoted_path}"
        if params:
            url = f"{url}?{parse.urlencode(params)}"
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(self._auth_header)
        req = request.Request(url, data=data, headers=headers, method=method)
        return request.urlopen(req, timeout=self.timeout)

    def _decode_change(self, line: bytes) -> dict[str, Any]:
        try:
            change = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBListenerError("CouchDB changes feed returned invalid JSON") from exc
        if not isinstance(change, dict):
            raise CouchDBListenerError("CouchDB changes feed returned non-object JSON")
        return change

    def _next_reconnect_attempt(self, current_attempts: int, exc: BaseException) -> int:
        next_attempts = current_attempts + 1
        if next_attempts > self.max_reconnect_attempts:
            raise CouchDBListenerError("CouchDB changes feed reconnection attempts exhausted") from exc
        time.sleep(min(2 ** (next_attempts - 1), 30))
        return next_attempts
