# Site Photo Viewer deployment

The viewer is a dedicated, read-only public service on loopback port 8084. It
authenticates against `/srv/btq/data/field_capture_tokens.sqlite3`, reads the
VPS-local `btq_field_captures`, `btq_photo_vision`, and `btq_vault` databases,
and redirects verified photo requests to fresh presigned R2 URLs.
Authentication writes only the token row's `last_used_at`; the service does not
mutate CouchDB or operational data.

## Machine-local environment

Create `/etc/gregstoltz/viewer.env` as a root-owned, non-repository file. It must
set `BTQ_MEDIA_STORE=s3`, complete `BTQ_R2_ENDPOINT_URL`, `BTQ_R2_BUCKET`,
`BTQ_R2_ACCESS_KEY_ID`, and `BTQ_R2_SECRET_ACCESS_KEY` values, plus the read-only
`BTQ_COUCHDB_URL`, `BTQ_COUCHDB_USER`, and `BTQ_COUCHDB_PASSWORD` values. Set
`BTQ_COUCHDB_FIELD_CAPTURES_DB`, `BTQ_COUCHDB_PHOTO_VISION_DB`, and
`BTQ_COUCHDB_VAULT_DB` only when their names differ from the repository defaults.

Set `SITE_PHOTO_VIEWER_PUBLIC_URL` in `/etc/btq/deploy.env`. The dedicated deploy
wrapper refuses to deploy when it is absent; a placeholder hostname is not a
valid smoke test.

Run the deployment from a repository checkout:

```bash
./scripts/deploy-site-photo-viewer-on-vps
```

The wrapper backs up the existing app, installs a flattened copy of `project/`,
refreshes the systemd unit, restarts only `btq-site-photo-viewer.service`, then
checks the loopback health signature and the public unauthenticated 401 gate.

## External Caddy configuration

The Caddy configuration is operator-owned and remains outside this repository.
Set `BTQ_SITE_PHOTO_VIEWER_HOST` in Caddy's machine-local environment to the
settled hostname, then add this block exactly to the directory imported by the
active Caddyfile:

```caddyfile
{$BTQ_SITE_PHOTO_VIEWER_HOST} {
	encode zstd gzip

	@token_query query token=*
	log_skip @token_query

	header {
		-Server
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		X-Robots-Tag "noindex, nofollow, noarchive"
		Permissions-Policy "camera=(), microphone=(), geolocation=()"
	}

	reverse_proxy 127.0.0.1:8084
}
```

Operator sequence:

1. Confirm the installed Caddy version supports `log_skip`.
2. Back up the active Caddy configuration.
3. Add the block to the directory already imported by the active Caddyfile.
4. Run `caddy validate --config <active-config> --adapter caddyfile`.
5. Reload Caddy only after validation succeeds.
6. Confirm token-bearing viewer requests are absent from the access log.
7. If `log_skip` is unavailable, disable access logging for this entire vhost
   before exposing it. Do not temporarily log raw tokens.
8. Verify the unauthenticated 401 and authenticated flows against the public
   HTTPS URL, not only loopback.

Before pinning a group link, run the existing read-only media coverage audit
against VPS-local CouchDB and production R2. Create or edit viewer tokens only
through the supported token admin, using `read_only`, `can_view_site=true`, and
one explicit site ID per shared link. Never issue a wildcard viewer token.
