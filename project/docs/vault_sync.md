# Vault Sync

Historical note: this document describes the legacy vault-to-CouchDB bootstrap
path. Post-C2, CouchDB is the canonical store for sites, people, and operational
entities. The Markdown vault is an opt-in projection/export and must not be
treated as the source of truth for new runtime paths.

## One-Time Setup

Create the people database on `btq-vps` with CouchDB admin credentials:

```bash
curl -u admin:$ADMIN_PASS -X PUT http://btq-vps:5984/btq_people
curl -u admin:$ADMIN_PASS -X PUT http://btq-vps:5984/btq_people/_security \
  -H 'Content-Type: application/json' \
  -d '{"members": {"names": ["btq-runtime"], "roles": []}, "admins": {"names": [], "roles": []}}'
```

Adjust `btq-runtime` to match the existing `btq_sites` channel user if needed.
That user needs reader and writer membership on both `btq_sites` and
`btq_people`.

## Run The Sync

From the Mac:

```bash
~/btq/scripts/btq refresh-vault --dry-run --json
~/btq/scripts/btq refresh-vault --json
```

Run the dry run first to surface duplicate ids or malformed projection
frontmatter before touching CouchDB.

The sync is on-demand legacy/bootstrap tooling only, not the normal mutation
path.

## Conflict Policy

For this legacy bootstrap path only, imported vault records can overwrite docs
marked `synced_from_vault: true`. Normal operation treats CouchDB as canonical;
Markdown projection edits do not define authoritative entity state.
