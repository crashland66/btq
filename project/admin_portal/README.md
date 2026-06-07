# BTQ Admin Portal Proposal

V1 admin shell for `admin.example.com`.

This portal is intentionally static and read-only. It does not approve content,
stage queue jobs, run processors, sync VPS uploads, or mutate the BTQ vault.

## Proposed File Structure

```text
project/admin_portal/
  README.md
  caddy/
    admin.example.com.Caddyfile
  public/
    index.html
    ops-dashboard.html
    docs.html
    sites.html
    content-moderation.html
    runtime-health.html
    styles.css
```

## Architecture

The hosting node can serve this static shell for `admin.example.com`. The
processing node remains the BTQ runtime and processing authority.

```text
admin.example.com
  -> protected static admin shell
  -> protected link to runtime ops dashboard over private networking
  -> optional protected reverse proxy to http://processing-node.example:8765/

Processing node
  -> field_capture/intake
  -> transcription and semantic artifacts
  -> review artifacts
  -> deliberate queue staging
  -> deterministic queue processor
  -> vault mutation
```

## Current VPS Deployment

Stage 27 deployed Option A to the VPS:

- host: `deploy@vps.example.com`
- destination: `/srv/www/admin.example.com`
- Caddy config: `/etc/caddy/Caddyfile`, between `BTQ ADMIN PORTAL` markers
- protection: Caddy basic auth
- basic-auth secret location: VPS Caddy config only, outside this repository
- runtime ops dashboard: link-only to `http://processing-node.example:8765/`

The deployed Caddy route serves static files only. It does not reverse proxy the
runtime ops dashboard.

## V1 Proxy Choice

V1 defaults to a protected link to the runtime ops dashboard:

```text
http://processing-node.example:8765/
```

Reason: a link keeps the admin shell read-only and avoids exposing the runtime
runtime dashboard through the public domain before access control has been
confirmed end to end.

The Caddy example includes an optional protected reverse proxy block for a
later operator-approved deployment. Do not enable the proxy unless
`admin.example.com` is already protected by Cloudflare Access, Tailscale-only
networking, Caddy basic auth, or equivalent controls.

## Security Recommendation

Recommended V1 protection order:

1. Cloudflare Access in front of `admin.example.com`, restricted to approved
   operator identities.
2. Tailscale-only exposure where practical, so the admin hostname is reachable
   only from the tailnet.
3. Caddy basic auth as a fallback or defense-in-depth layer, using a hash
   supplied outside the repo.

Do not store raw passwords, bearer tokens, Cloudflare secrets, Tailscale auth
keys, or Caddy password hashes in this repository.

## Documentation Exposure

The admin docs page links to these source-of-truth repository documents:

- `project/docs/runbook.md`
- `project/docs/architecture/shared_processing_spine.md`
- `project/docs/architecture/system_overview.md`
- `project/docs/architecture/runtime_flow.md`
- `project/field_capture/README.md`

For a static VPS deployment, copy rendered or raw Markdown versions of those
documents into a protected static docs directory at deploy time. This proposal
does not add a new docs build pipeline.

## Manual Deploy Notes

Deployment remains manual and pending explicit approval.

Suggested manual shape:

1. Put `project/admin_portal/public/` on the VPS under a protected web root.
2. Configure `admin.example.com` with Cloudflare Access or equivalent
   protection before DNS or public routing goes live.
3. Install the Caddy site block only after replacing placeholder paths and
   confirming access protection.
4. Keep the Mac ops dashboard bound to the Mac runtime. Do not move BTQ
   processing to the VPS.
5. If enabling the optional reverse proxy, confirm the VPS can reach
   the processing node over private networking and that unauthenticated requests are
   blocked before the proxy.

## VPS Install Helper

`deploy/install-admin-portal-on-vps.sh` is a VPS-side helper for Option A. It
expects a tar archive of `public/`, installs it under
`/srv/www/admin.example.com`, prompts for a Caddy basic-auth password, hashes
that password on the VPS, updates `/etc/caddy/Caddyfile` between marked admin
portal comments, validates the new Caddy config, and reloads Caddy only after
validation passes.

The helper does not store the raw password. The resulting bcrypt hash lives only
in the VPS Caddy config, outside this repository.

## Read-Only Boundary

V1 must not include:

- mutation buttons
- approval UI
- publish UI
- queue staging controls
- processor execution controls
- VPS pull controls
- vault edit forms
