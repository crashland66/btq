from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib import error, parse, request

from config import get_config, repo_root
from io_atomic import atomic_write_text

from btq_cli.shift_report_sc_mapping import (
    SC_BASE,
    build_prefill_payload,
    parse_shift_report,
)


SAFETYCULTURE_TOKEN_KEY = "btq-shift-report"
DEFAULT_STATE_RELATIVE_PATH = Path("safetyculture") / "shift-report-drafts.json"


class JsonHttpClient(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        ...

    def put_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        ...


class UrllibJsonHttpClient:
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return self._json_request("POST", url, payload, headers)

    def put_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return self._json_request("PUT", url, payload, headers)

    def _json_request(self, method: str, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8")
        outgoing_headers = {"Content-Type": "application/json", **headers}
        req = request.Request(url, data=body, headers=outgoing_headers, method=method)
        try:
            with request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                return response.status, _decode_json_object(response_body)
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            return exc.code, _decode_json_object(response_body)


class SafetyCultureSubmitError(RuntimeError):
    pass


class SafetyCultureArchiveError(RuntimeError):
    pass


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "shift-report-to-sc",
        help="Create an unsigned SafetyCulture Area Manager Shift Report draft from a closeday report.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report-path", type=Path, help="Path to YYYY-MM-DD-shift-report.md.")
    source.add_argument("--date", help="Report date YYYY-MM-DD; resolved under BTQ_JOURNAL_DIR.")
    parser.add_argument("--dry-run", action="store_true", help="Print the SafetyCulture prefill payload without reading a token or calling the API.")
    parser.add_argument("--force", action="store_true", help="Create a new draft even if this date already has a recorded draft.")
    parser.add_argument("--state-path", type=Path, help=argparse.SUPPRESS)
    parser.set_defaults(func=handle_shift_report_to_sc)


def handle_shift_report_to_sc(args: argparse.Namespace) -> int:
    try:
        report_date = resolve_report_date(args)
        report_path = resolve_report_path(args, report_date)
        md_text = report_path.read_text(encoding="utf-8")
        payload = build_prefill_payload(parse_shift_report(md_text), report_date)
    except Exception as exc:
        raise SystemExit(f"shift-report-to-sc: {exc}") from exc

    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    state_path = resolve_state_path(args.state_path)
    state = load_draft_state(state_path)
    existing_audit_id = existing_draft_id(state, report_date)
    if existing_audit_id and not args.force:
        print(f"shift-report-to-sc: draft already exists for {report_date}: audit_id={existing_audit_id}")
        print(inspection_link(existing_audit_id))
        return 0

    try:
        token = load_safetyculture_token()
        audit_id = submit_prefill_payload(payload, token)
        record_draft_state(state_path, state, report_date, audit_id, report_path)
    except Exception as exc:
        raise SystemExit(f"shift-report-to-sc: {exc}") from exc

    print(f"shift-report-to-sc: created draft for {report_date}: audit_id={audit_id}")
    print(inspection_link(audit_id))
    return 0


def resolve_report_date(args: argparse.Namespace) -> str:
    if args.date:
        return _validate_date(args.date)
    if args.report_path is None:
        raise ValueError("--report-path or --date is required")
    name = args.report_path.expanduser().name
    if len(name) < 10:
        raise ValueError(f"cannot infer report date from filename: {name}")
    return _validate_date(name[:10])


def resolve_report_path(args: argparse.Namespace, report_date: str) -> Path:
    if args.report_path is not None:
        return args.report_path.expanduser()
    journal_dir = os.environ.get("BTQ_JOURNAL_DIR", "").strip()
    if not journal_dir:
        raise ValueError("--date requires BTQ_JOURNAL_DIR")
    return Path(journal_dir).expanduser() / f"{report_date}-shift-report.md"


def resolve_state_path(override: Path | None = None) -> Path:
    if override is not None:
        return override.expanduser()
    return get_config().project_runtime_root.expanduser() / DEFAULT_STATE_RELATIVE_PATH


def load_draft_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"drafts": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"state file must contain a JSON object: {path}")
    drafts = raw.setdefault("drafts", {})
    if not isinstance(drafts, dict):
        raise ValueError(f"state file drafts must be a JSON object: {path}")
    return raw


def existing_draft_id(state: dict[str, Any], report_date: str) -> str | None:
    drafts = state.get("drafts")
    if not isinstance(drafts, dict):
        return None
    entry = drafts.get(report_date)
    if not isinstance(entry, dict):
        return None
    audit_id = str(entry.get("audit_id") or "").strip()
    return audit_id or None


def record_draft_state(path: Path, state: dict[str, Any], report_date: str, audit_id: str, report_path: Path) -> None:
    drafts = state.setdefault("drafts", {})
    if not isinstance(drafts, dict):
        raise ValueError("state drafts must be a JSON object")
    drafts[report_date] = {
        "audit_id": audit_id,
        "inspection_url": inspection_link(audit_id),
        "report_path": str(report_path),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_safetyculture_token(token_path: Path | None = None) -> str:
    path = token_path.expanduser() if token_path is not None else repo_root() / "secrets" / "safetyculture"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"SafetyCulture token file not found: {path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key.strip() == SAFETYCULTURE_TOKEN_KEY:
            token = value.strip().strip('"').strip("'")
            if token:
                return token
            raise ValueError(f"SafetyCulture token is empty for key {SAFETYCULTURE_TOKEN_KEY}")
    raise ValueError(f"SafetyCulture token key not found in {path}: {SAFETYCULTURE_TOKEN_KEY}")


def submit_prefill_payload(
    payload: dict[str, Any],
    token: str,
    *,
    http_client: JsonHttpClient | None = None,
    base_url: str = SC_BASE,
) -> str:
    client = http_client or UrllibJsonHttpClient()
    status, response_body = client.post_json(
        f"{base_url.rstrip('/')}/audits",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    if status != 201:
        raise SafetyCultureSubmitError(f"POST /audits failed with status {status}: {json.dumps(response_body, sort_keys=True)}")
    audit_id = str(response_body.get("audit_id") or "").strip()
    if not audit_id:
        raise SafetyCultureSubmitError(f"POST /audits returned 201 without audit_id: {json.dumps(response_body, sort_keys=True)}")
    return audit_id


def archive_inspection(
    audit_id: str,
    token: str,
    *,
    http_client: JsonHttpClient | None = None,
    base_url: str = SC_BASE,
) -> None:
    normalized_id = str(audit_id or "").strip()
    if not normalized_id:
        raise SafetyCultureArchiveError("archive_inspection requires audit_id")
    client = http_client or UrllibJsonHttpClient()
    status, response_body = client.put_json(
        f"{base_url.rstrip('/')}/audits/{parse.quote(normalized_id, safe='')}",
        {"archived": True},
        {"Authorization": f"Bearer {token}"},
    )
    if status < 200 or status >= 300:
        raise SafetyCultureArchiveError(f"PUT /audits/{normalized_id} failed with status {status}: {json.dumps(response_body, sort_keys=True)}")


def inspection_link(audit_id: str) -> str:
    return f"https://app.safetyculture.com/inspection/{audit_id}"


def _validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"report date must be YYYY-MM-DD: {value}") from exc
    return value


def _decode_json_object(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    decoded = json.loads(value)
    if isinstance(decoded, dict):
        return decoded
    return {"response": decoded}
