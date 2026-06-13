# Unified Capture Deploy

This runbook prepares `fc.example.com` beside the existing public capture
apps. It does not retire or modify `photos.example.com` or
`voice.example.com`; keep both live during gradual cutover.

## Serving Model

- App directory: `/srv/btq/apps/unified-capture`
- Static PWA root: `/srv/btq/apps/unified-capture/dist`
- Systemd service: `btq-unified-capture.service`
- Loopback API: `127.0.0.1:8081`
- Token DB: `/srv/btq/data/field_capture_tokens.sqlite3`
- Person/site resolution: CouchDB (`btq_vault` / `btq_sites`) via `BTQ_COUCHDB_*`
- Upload root: `/srv/btq/runtime/uploads`
- Optional service environment: `/etc/btq/unified-capture.env`

The unified app uses the same token database, CouchDB connection, and upload root
as field capture. Worker tokens and the existing post-upload pipeline continue to
operate on the same data.

## Edge Prerequisites

The VPS edge must have `fcuser` available and reachable CouchDB, matching the
field-capture edge assumptions. Do not expose the hostname until CouchDB is
reachable for token authorization and site lookup.

The service runs as user/group `btq-field` — the same identity as the field
capture app — so it shares the token DB, CouchDB connection, and upload root with
no ownership changes or new user provisioning. (The unified app is interchangeable
with field capture by design; running it as `btq-field` keeps shared-data
permissions identical and avoids a separate service account.)

`fc.example.com` is behind Cloudflare and is already pointed at the same
Cloudflare address pair as `photos.example.com` and `voice.example.com`.
Per prompt 260, clients that fetch through the public Cloudflare front must use
a descriptive, non-bot `User-Agent`; automation should prefer the loopback or
tailnet URL for operational checks.

## Caddy Vhost

Install the site block only during the operator deploy step. The API is proxied
to the unified service on port `8081`; static files are served from `dist`.

```caddyfile
fc.example.com {
	root * /srv/btq/apps/unified-capture/dist

	handle /api/* {
		reverse_proxy 127.0.0.1:8081
	}

	handle {
		try_files {path} /index.html
		file_server
	}
}
```

Validate and reload Caddy only after the app directory, `dist`, and systemd
unit are installed.

## Deploy Wrapper

From a separate BTQ checkout on the VPS, run:

```bash
./scripts/deploy-unified-capture-app-on-vps
```

The wrapper stages the checkout, backs up `/srv/btq/apps/unified-capture`,
installs into that directory, materializes `dist/` from
`project/unified_capture/public/`, writes shared PWA assets for the
`unified_capture` service worker, restarts `btq-unified-capture.service`, and
smokes `GET /api/health` on `127.0.0.1:8081`.

The wrapper touches only the unified app directory and
`btq-unified-capture.service`. It does not deploy, restart, or reconfigure
field capture or voice memo.

## Gradual Cutover

First public exposure is a later Build G operator step. During cutover:

1. Keep `photos.example.com` and `voice.example.com` running.
2. Deploy unified capture beside them on `/srv/btq/apps/unified-capture`.
3. Add the `fc.example.com` Caddy vhost and verify `/api/health`.
4. Move selected users to `https://fc.example.com` with their existing worker
   tokens.
5. Retire old apps only in a later, explicit decommission step.
