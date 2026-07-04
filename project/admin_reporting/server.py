from __future__ import annotations

import argparse
import html
import logging
import os
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from token_store import TokenRecord, TokenStore

from . import staples_reads


DEFAULT_TOKEN_DB = Path("/srv/btq/data/field_capture_tokens.sqlite3")
COOKIE_NAME = "btq_admin_reporting_token"

# Self-contained concept demo (Site Inventory & Ordering). Static, baked data —
# served behind the same token gate as every other view but NOT wrapped in the
# reporting chrome (it is a full standalone document).
DEMO_HTML_PATH = Path(__file__).resolve().parent / "demo" / "order_sheet_demo.html"
_DEMO_HTML_CACHE: bytes | None = None


def load_demo_html() -> bytes:
    global _DEMO_HTML_CACHE
    if _DEMO_HTML_CACHE is None:
        try:
            _DEMO_HTML_CACHE = DEMO_HTML_PATH.read_bytes()
        except OSError:
            _DEMO_HTML_CACHE = (
                b"<!doctype html><meta charset=\"utf-8\"><title>Demo unavailable</title>"
                b"<p>The order-sheet demo asset is missing.</p>"
            )
    return _DEMO_HTML_CACHE


def demo_href(token: str) -> str:
    return f"/order-demo?{urlencode({'token': token})}" if token else "/order-demo"


class AdminReportingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token_store: TokenStore,
        *,
        couchdb_find: staples_reads.CouchDBFind | None = None,
    ) -> None:
        super().__init__(address, AdminReportingHandler)
        self.token_store = token_store
        self.couchdb_find = couchdb_find


class AdminReportingHandler(BaseHTTPRequestHandler):
    server: AdminReportingServer

    def do_GET(self) -> None:
        status, content_type, body, headers = route_response(
            "GET",
            self.path,
            self.server.token_store,
            couchdb_find=self.server.couchdb_find,
            cookie_header=self.headers.get("Cookie", ""),
        )
        self.write_bytes(body, status, content_type, headers=headers)

    def do_POST(self) -> None:
        status, content_type, body, headers = route_response(
            "POST",
            self.path,
            self.server.token_store,
            couchdb_find=self.server.couchdb_find,
            cookie_header=self.headers.get("Cookie", ""),
        )
        self.write_bytes(body, status, content_type, headers=headers)

    def write_html(self, body: str, status: HTTPStatus, *, headers: dict[str, str] | None = None) -> None:
        self.write_bytes(body.encode("utf-8"), status, "text/html; charset=utf-8", headers=headers)

    def write_bytes(
        self,
        body: bytes,
        status: HTTPStatus,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            logging.info("client disconnected while writing response")

    def log_message(self, format: str, *args: object) -> None:
        return


class AuthResult:
    def __init__(self, record: TokenRecord | None, status: HTTPStatus, message: str, query_token: str = "") -> None:
        self.record = record
        self.status = status
        self.message = message
        self.query_token = query_token


def route_response(
    method: str,
    path: str,
    token_store: TokenStore,
    *,
    couchdb_find: staples_reads.CouchDBFind | None = None,
    cookie_header: str = "",
) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    if method != "GET":
        return (
            HTTPStatus.METHOD_NOT_ALLOWED,
            "application/json; charset=utf-8",
            b'{"error":"read_only"}\n',
            {},
        )

    parsed = urlsplit(path)
    if parsed.path == "/api/health":
        return HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"ok"}\n', {}
    if parsed.path not in {"/", "/site-orders", "/order-demo"}:
        return (
            HTTPStatus.NOT_FOUND,
            "text/html; charset=utf-8",
            render_page("Not found", "<main><h1>Not found</h1></main>").encode("utf-8"),
            {},
        )

    auth = authenticate_request(
        cookie_header,
        parsed.query,
        token_store,
    )
    if auth.record is None:
        return auth.status, "text/html; charset=utf-8", render_error_page(auth.message).encode("utf-8"), {}

    if parsed.path == "/order-demo":
        headers = {}
        if auth.query_token:
            headers["Set-Cookie"] = render_auth_cookie(auth.query_token)
        return HTTPStatus.OK, "text/html; charset=utf-8", load_demo_html(), headers

    try:
        if parsed.path == "/":
            body = render_landing(couchdb_find, auth.query_token)
        else:
            query = parse_qs(parsed.query, keep_blank_values=True)
            ship_to_name = first_query_value(query, "ship_to_name").strip()
            body = render_site_orders(ship_to_name, couchdb_find, auth.query_token)
    except Exception:
        logging.exception("admin reporting render failed path=%s", parsed.path)
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            "text/html; charset=utf-8",
            render_error_page("Reference reporting data is unavailable.").encode("utf-8"),
            {},
        )

    headers = {}
    if auth.query_token:
        headers["Set-Cookie"] = render_auth_cookie(auth.query_token)
    return HTTPStatus.OK, "text/html; charset=utf-8", render_page("Site Orders", body).encode("utf-8"), headers


def authenticate_request(
    cookie_header: str,
    query_string: str,
    token_store: TokenStore,
) -> AuthResult:
    query = parse_qs(query_string, keep_blank_values=True)
    query_token = first_query_value(query, "token").strip()
    token = query_token or cookie_token(cookie_header)
    if not token:
        return AuthResult(None, HTTPStatus.UNAUTHORIZED, "A reporting token is required.")
    record = token_store.authenticate(token)
    if record is None:
        return AuthResult(None, HTTPStatus.UNAUTHORIZED, "The reporting token is invalid or expired.")
    if not record.can_view_site or record.role not in {"site_admin", "read_only"}:
        return AuthResult(None, HTTPStatus.FORBIDDEN, "The token is not authorized for this reporting view. A site_admin token is required.")
    return AuthResult(record, HTTPStatus.OK, "", query_token=query_token)


def cookie_token(cookie_header: str) -> str:
    if not cookie_header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return ""
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value.strip() if morsel is not None else ""


def render_auth_cookie(token: str) -> str:
    cookie = SimpleCookie()
    cookie[COOKIE_NAME] = token
    cookie[COOKIE_NAME]["path"] = "/"
    cookie[COOKIE_NAME]["httponly"] = True
    cookie[COOKIE_NAME]["samesite"] = "Lax"
    return cookie.output(header="").strip()


def render_landing(couchdb_find: staples_reads.CouchDBFind | None, token: str) -> str:
    summaries = staples_reads.site_summaries(couchdb_find=couchdb_find)
    metadata = staples_reads.report_metadata(couchdb_find=couchdb_find)
    options = ['<option value="">Select a site</option>']
    rows = []
    for summary in summaries:
        name = str(summary.get("ship_to_name") or "").strip()
        if not name:
            continue
        numbers = ship_to_numbers_text(summary)
        label = f"{name} ({numbers})" if numbers else name
        options.append(f'<option value="{html.escape(name, quote=True)}">{html.escape(label)}</option>')
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(site_orders_href(name, token), quote=True)}\">{html.escape(name)}</a></td>"
            f"<td>{html.escape(numbers)}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(summary.get('quantity')))}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(summary.get('estimated_monthly_quantity')))}</td>"
            f"<td class=\"num\">{html.escape(format_money(summary.get('adjusted_gross_sales')))}</td>"
            "</tr>"
        )
    hidden_token = f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">' if token else ""
    table = "".join(rows) or '<tr><td colspan="5" class="empty">No Staples site usage found.</td></tr>'
    return f"""
    <main>
      <header class="page-head">
        <div>
          <p class="eyebrow">Read-only reporting</p>
          <h1>Staples Site Orders</h1>
          <p class="muted">Site-level usage imported from CouchDB references.</p>
        </div>
        {render_metadata(metadata)}
      </header>
      <section class="toolbar" aria-label="Site selector">
        <form method="get" action="/site-orders">
          {hidden_token}
          <label for="ship_to_name">Site</label>
          <select id="ship_to_name" name="ship_to_name">{"".join(options)}</select>
          <button type="submit">Open report</button>
          <a class="secondary" href="{html.escape(demo_href(token), quote=True)}">Inventory &amp; ordering concept &rarr;</a>
        </form>
      </section>
      <section>
        <h2>Sites</h2>
        <table>
          <thead><tr><th>Site</th><th>Ship-To</th><th class="num">Qty</th><th class="num">Est/mo</th><th class="num">Spend</th></tr></thead>
          <tbody>{table}</tbody>
        </table>
      </section>
    </main>
    """


def render_site_orders(ship_to_name: str, couchdb_find: staples_reads.CouchDBFind | None, token: str) -> str:
    summaries = staples_reads.site_summaries(couchdb_find=couchdb_find)
    selected_summary = next((item for item in summaries if str(item.get("ship_to_name") or "") == ship_to_name), None)
    metadata = staples_reads.report_metadata(couchdb_find=couchdb_find)
    selector = render_site_nav(summaries, ship_to_name, token)
    if not ship_to_name:
        return f"""
        <main>
          <header class="page-head">
            <div>
              <p class="eyebrow">Read-only reporting</p>
              <h1>Staples Site Orders</h1>
              <p class="muted">Choose a site to view item usage and order-line detail.</p>
            </div>
            {render_metadata(metadata)}
          </header>
          {selector}
        </main>
        """
    if selected_summary is None:
        return f"""
        <main>
          <header class="page-head">
            <div>
              <p class="eyebrow">Read-only reporting</p>
              <h1>Staples Site Orders</h1>
            </div>
            {render_metadata(metadata)}
          </header>
          {selector}
          <section class="notice">Site not found in Staples usage report: {html.escape(ship_to_name)}</section>
        </main>
        """

    sku_rows = staples_reads.site_sku_summaries(ship_to_name, couchdb_find=couchdb_find)
    detail_rows = staples_reads.site_detail_rows(ship_to_name, couchdb_find=couchdb_find)
    return f"""
    <main>
      <header class="page-head">
        <div>
          <p class="eyebrow">Read-only reporting</p>
          <h1>{html.escape(ship_to_name)}</h1>
          <p class="muted">SKU usage, estimated monthly quantity, spend, and source order-line detail.</p>
        </div>
        {render_metadata(metadata)}
      </header>
      {selector}
      {render_metric_strip(selected_summary)}
      <section>
        <h2>Item Usage</h2>
        {render_sku_table(sku_rows)}
      </section>
      <section>
        <h2>Order-Line Detail</h2>
        {render_detail_table(detail_rows)}
      </section>
    </main>
    """


def render_site_nav(summaries: list[dict[str, object]], selected_site: str, token: str) -> str:
    options = ['<option value="">Select a site</option>']
    for summary in summaries:
        name = str(summary.get("ship_to_name") or "").strip()
        if not name:
            continue
        selected = " selected" if name == selected_site else ""
        numbers = ship_to_numbers_text(summary)
        label = f"{name} ({numbers})" if numbers else name
        options.append(f'<option value="{html.escape(name, quote=True)}"{selected}>{html.escape(label)}</option>')
    hidden_token = f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">' if token else ""
    return f"""
    <section class="toolbar" aria-label="Site selector">
      <form method="get" action="/site-orders">
        {hidden_token}
        <label for="ship_to_name">Site</label>
        <select id="ship_to_name" name="ship_to_name">{"".join(options)}</select>
        <button type="submit">Open report</button>
        <a class="secondary" href="{html.escape(root_href(token), quote=True)}">All sites</a>
      </form>
    </section>
    """


def render_metadata(metadata: dict[str, object]) -> str:
    fields = [
        ("Vendor", metadata.get("vendor")),
        ("Period", period_text(metadata)),
        ("Report", metadata.get("report_id")),
    ]
    values = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(str(value or ''))}</dd></div>"
        for label, value in fields
        if str(value or "").strip()
    )
    return f'<dl class="metadata">{values}</dl>'


def render_metric_strip(summary: dict[str, object]) -> str:
    metrics = (
        ("Quantity", format_quantity(summary.get("quantity"))),
        ("Est/month", format_quantity(summary.get("estimated_monthly_quantity"))),
        ("SKUs", format_quantity(summary.get("sku_count"))),
        ("Lines", format_quantity(summary.get("line_count"))),
        ("Spend", format_money(summary.get("adjusted_gross_sales"))),
    )
    return '<section class="metrics">' + "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>" for label, value in metrics
    ) + "</section>"


def render_sku_table(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('sku_number') or ''))}</td>"
            f"<td>{html.escape(str(row.get('website_item_description') or ''))}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(row.get('quantity')))}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(row.get('estimated_monthly_quantity')))}</td>"
            f"<td class=\"num\">{html.escape(format_price_values(row.get('average_selling_price_values')))}</td>"
            f"<td class=\"num\">{html.escape(format_money(row.get('adjusted_gross_sales')))}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(row.get('line_count')))}</td>"
            "</tr>"
        )
    table_body = "".join(body) or '<tr><td colspan="7" class="empty">No item usage found for this site.</td></tr>'
    return f"""
    <table>
      <thead><tr><th>SKU</th><th>Item</th><th class="num">Qty</th><th class="num">Est/mo</th><th class="num">Avg Price</th><th class="num">Spend</th><th class="num">Lines</th></tr></thead>
      <tbody>{table_body}</tbody>
    </table>
    """


def render_detail_table(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td class=\"num\">{html.escape(str(row.get('row_index') or ''))}</td>"
            f"<td>{html.escape(str(row.get('ship_to_number') or ''))}</td>"
            f"<td>{html.escape(str(row.get('sku_number') or ''))}</td>"
            f"<td>{html.escape(str(row.get('website_item_description') or ''))}</td>"
            f"<td class=\"num\">{html.escape(format_quantity(row.get('quantity')))}</td>"
            f"<td class=\"num\">{html.escape(format_money(row.get('average_selling_price')))}</td>"
            f"<td class=\"num\">{html.escape(format_money(row.get('adjusted_gross_sales')))}</td>"
            "</tr>"
        )
    table_body = "".join(body) or '<tr><td colspan="7" class="empty">No order lines found for this site.</td></tr>'
    return f"""
    <table>
      <thead><tr><th class="num">Row</th><th>Ship-To</th><th>SKU</th><th>Item</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Spend</th></tr></thead>
      <tbody>{table_body}</tbody>
    </table>
    """


def render_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #ffffff;
      --card: #ffffff;
      --soft: #f5f7fa;
      --ink: #17202a;
      --muted: #5d6875;
      --line: #d8dee6;
      --input-border: #b9c2cc;
      --th-ink: #34404d;
      --accent: #1d6f5f;
      --accent-dark: #15564a;
      --on-accent: #ffffff;
      --link: #15564a;
      --danger: #8a1f11;
      --notice-bg: #fff6f3;
      --notice-border: #f0c8bd;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0f1216;
        --card: #171c23;
        --soft: #10151c;
        --ink: #e8edf2;
        --muted: #94a3b8;
        --line: #2a323d;
        --input-border: #2a323d;
        --th-ink: #9fb4cc;
        --accent: #1f8f78;
        --accent-dark: #169c83;
        --on-accent: #ffffff;
        --link: #6ee7c2;
        --danger: #f98a7a;
        --notice-bg: #2a1713;
        --notice-border: #6b2b1c;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 0; font-weight: 700; letter-spacing: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 17px; margin: 24px 0 10px; }}
    a {{ color: var(--link); }}
    .page-head {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: flex-start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 16px;
    }}
    .eyebrow {{ margin: 0 0 4px; color: var(--accent-dark); font-size: 12px; font-weight: 700; text-transform: uppercase; }}
    .muted {{ color: var(--muted); margin: 6px 0 0; }}
    .metadata {{
      display: grid;
      grid-template-columns: repeat(3, max-content);
      gap: 10px 18px;
      margin: 0;
      color: var(--muted);
      text-align: right;
      white-space: nowrap;
    }}
    .metadata dt {{ font-size: 11px; font-weight: 700; text-transform: uppercase; }}
    .metadata dd {{ margin: 1px 0 0; color: var(--ink); }}
    .toolbar {{
      margin: 18px 0;
      padding: 12px;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    form {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    label {{ font-weight: 700; color: var(--muted); }}
    select {{
      min-width: min(520px, 100%);
      max-width: 100%;
      padding: 8px 10px;
      border: 1px solid var(--input-border);
      border-radius: 4px;
      background: var(--card);
      color: var(--ink);
    }}
    button, .secondary {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 7px 12px;
      border-radius: 4px;
      border: 1px solid var(--accent-dark);
      background: var(--accent);
      color: var(--on-accent);
      font-weight: 700;
      text-decoration: none;
    }}
    .secondary {{ background: var(--card); color: var(--link); }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(110px, 1fr));
      gap: 8px;
      margin: 16px 0 4px;
    }}
    .metrics div {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; }}
    .metrics span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 700; }}
    .metrics strong {{ display: block; margin-top: 2px; font-size: 20px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      border: 1px solid var(--line);
      background: var(--card);
    }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ background: var(--soft); color: var(--th-ink); font-size: 12px; text-align: left; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .num {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    .empty, .notice {{ color: var(--muted); }}
    .notice {{ border: 1px solid var(--notice-border); background: var(--notice-bg); color: var(--danger); border-radius: 6px; padding: 12px; }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      .page-head {{ display: block; }}
      .metadata {{ margin-top: 14px; text-align: left; grid-template-columns: 1fr; white-space: normal; }}
      form {{ align-items: stretch; }}
      label, select, button, .secondary {{ width: 100%; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>{body}</body>
</html>"""


def render_error_page(message: str) -> str:
    return render_page("Unauthorized", f"<main><section class=\"notice\">{html.escape(message)}</section></main>")


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or [""]
    return values[0] if values else ""


def site_orders_href(ship_to_name: str, token: str) -> str:
    query = {"ship_to_name": ship_to_name}
    if token:
        query["token"] = token
    return f"/site-orders?{urlencode(query)}"


def root_href(token: str) -> str:
    return f"/?{urlencode({'token': token})}" if token else "/"


def ship_to_numbers_text(summary: dict[str, object]) -> str:
    numbers = summary.get("ship_to_numbers")
    if not isinstance(numbers, list):
        return ""
    return ", ".join(str(item) for item in numbers if str(item).strip())


def period_text(metadata: dict[str, object]) -> str:
    start = str(metadata.get("period_start") or "").strip()
    end = str(metadata.get("period_end") or "").strip()
    if start and end:
        return f"{start} to {end}"
    return start or end


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


def run(host: str, port: int, token_db: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not token_db.exists():
        logging.error("token database not found: %s", token_db)
        return 1
    server = AdminReportingServer((host, port), token_store=TokenStore(token_db))
    logging.info("admin reporting listening on http://%s:%s", host, port)
    server.serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only admin reporting server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument(
        "--token-db",
        type=Path,
        default=Path(os.environ.get("BTQ_ADMIN_REPORTING_TOKEN_DB", str(DEFAULT_TOKEN_DB))),
        help="Path to the shared field-capture token database.",
    )
    args = parser.parse_args()
    return run(args.host, args.port, args.token_db)


if __name__ == "__main__":
    raise SystemExit(main())
