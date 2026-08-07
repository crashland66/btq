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
- Person/site resolution: CouchDB (`btq_vault`) via `BTQ_COUCHDB_*`
- Upload root: `/srv/btq/runtime/uploads`
- Photo cap: `--max-images 25` in the systemd `ExecStart`
- Optional service environment: `/etc/btq/unified-capture.env`

The unified app uses the same token database, CouchDB connection, and upload root
as field capture. Worker tokens and the existing post-upload pipeline continue to
operate on the same data.

The checked-in systemd unit sets `BTQ_COUCHDB_URL=http://127.0.0.1:5984` as a
local fallback before loading optional environment files. Keep that fallback in
the unit so a clean install can restart without depending on a machine-local env
file for the CouchDB URL; host-specific env files may still override it and must
continue to provide any CouchDB credentials outside version control.

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

## Canonical Deploy Path

From the operator BTQ checkout, run:

```bash
cd project/unified_capture
BTQ_VPS_SSH_TARGET=deploy@vps.example.com ./deploy/push-and-deploy.sh
```

`push-and-deploy.sh` syncs the repo root to the VPS source directory, excluding
git metadata, local virtualenvs, runtime directories, and Python cache files,
then runs `project/unified_capture/deploy/deploy.sh` from that synced source.
The default remote source is the public-safe deploy-host placeholder
`/home/deploy/unified-capture-source` <!-- # sanitization-ok: public-safe deploy-host placeholder -->
(set `UNIFIED_CAPTURE_REMOTE_SOURCE` when the host uses a different path).

The remote `deploy.sh` installs the full source tree into
`/srv/btq/apps/unified-capture` while preserving the live `dist/` directory and
machine-local `config.json`, materializes `dist/` from
`project/unified_capture/public/`, writes shared PWA assets for the
`unified_capture` service worker, installs `btq-unified-capture.service`,
restarts it, and smokes `GET /api/session` on `127.0.0.1:8081`. A healthy
unauthenticated service returns `401` for that route, which proves the API is up
and enforcing the token gate.

The installed service runs `unified_capture.server` with `--max-images 25`, so
regular capture tokens can submit larger photo batches without relying on a
systemd drop-in.

The scripts touch only the unified app directory and
`btq-unified-capture.service`. They do not deploy, restart, or reconfigure field
capture or voice memo, and they do not clobber the operator-owned
`/srv/btq/apps/unified-capture/config.json`.

For a two-phase rollout, matching the field-capture deploy convention:

```bash
cd project/unified_capture
./deploy/push-and-deploy.sh --stage-static-only
./deploy/push-and-deploy.sh --python-and-systemd
```

If already on the VPS inside the synced source directory, the underlying command
is:

```bash
./project/unified_capture/deploy/deploy.sh
```

## Legacy Manual Reference

The older VPS-local helper remains available for historical reference:

```bash
./scripts/deploy-unified-capture-app-on-vps
```

Prefer the canonical deploy scripts above for current maintenance because they
sync the full BTQ source tree before restart and avoid stale vendored copies.

## Gradual Cutover

First public exposure is a later Build G operator step. During cutover:

1. Keep `photos.example.com` and `voice.example.com` running.
2. Deploy unified capture beside them on `/srv/btq/apps/unified-capture`.
3. Add the `fc.example.com` Caddy vhost and verify `/api/health`.
4. Move selected users to `https://fc.example.com` with their existing worker
   tokens.
5. Retire old apps only in a later, explicit decommission step.
