# Field Capture Deploy

Field Capture now follows the same public serving pattern as Voice Memo:

- Caddy serves static PWA files from `/srv/btq/apps/field-capture/dist`.
- Caddy reverse-proxies `/api/*` to `127.0.0.1:8080`.
- Python serves API, `/site/<site_id>`, and `/media/...` only.
- Optional service environment lives at `/etc/btq/field-capture.env`.
- The systemd unit bakes in `BTQ_COUCHDB_URL=http://127.0.0.1:5984` as a local fallback; a
  correctly set environment file can still override it.

Use the two-phase rollout for the first prompt-12 deploy:

```bash
cd /Users/operator/btq/project/field_capture
./deploy/push-and-deploy.sh --stage-static-only

cd /Users/operator/WebsiteProjects/example.com
./deploy/push-and-deploy.sh

cd /Users/operator/btq/project/field_capture
./deploy/push-and-deploy.sh --python-and-systemd
```

Do not touch `/srv/btq/data` during a UI or serving-layout deploy. The token
database and vault mirror are production data, not app release artifacts.

The default `./deploy/push-and-deploy.sh` runs both phases for fresh installs
or post-migration maintenance deploys.
