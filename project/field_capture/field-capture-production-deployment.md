# Field Capture Production Deployment

This document describes the safe process for promoting the field-capture app to
the VPS. Keep auth data, app code, and service configuration as separate
mutation classes.

## Production Topology

- Production host: `deploy@vps.example.com`
- Public URL: `https://photos.example.com`
- Caddy route: static PWA files from `/srv/btq/apps/field-capture/dist`;
  `/api/*` -> `127.0.0.1:8080`
- systemd service: `btq-field-capture.service`
- App path: `/srv/btq/apps/field-capture`
- Static dist path: `/srv/btq/apps/field-capture/dist`
- BTQ source mirror path: `/home/deploy/field-capture-source`
- Data path: `/srv/btq/data`
- Runtime path: `/srv/btq/runtime`
- Token DB: `/srv/btq/data/field_capture_tokens.sqlite3`
- Vault mirror: `/srv/btq/data/vault-readonly`
- Production user/group: `btq-field:btq-field`

The app should bind only to `127.0.0.1:8080` and serve API/viewer/media routes
only. Caddy is the public HTTPS ingress and serves the installable PWA shell.

## App Directory Permissions

App code and runtime data are separate permission domains. The deploy user only
needs write access to `/srv/btq/apps/field-capture` and
`/srv/btq/backups/field-capture` for app-code deploys. Runtime directories under
`/srv/btq/runtime` are used by the running service and should not be repaired by
an app-code deploy unless a runtime setup task explicitly calls for it.

For non-sudo deploys, the live app tree must be writable by the deploy user or
one of the deploy user's groups. Directories should be group-writable and setgid
so new files keep the deploy group. Repair the live app tree with:

```bash
sudo chgrp -R <deploy-group> /srv/btq/apps/field-capture
sudo chmod -R g+rwX /srv/btq/apps/field-capture
sudo find /srv/btq/apps/field-capture -type d -exec chmod g+s {} +
```

Apply the same group-write/setgid model to `/srv/btq/backups/field-capture` if
the deploy user should create backups without sudo.

## Hard Safety Rules

- Do not change Caddy unless routing is proven wrong.
- Do not delete old backups.
- Do not touch `/srv/btq/data` during UI-only deploys.
- Do not print raw tokens or secrets.
- Stop if vault or token DB paths are unclear.
- Change one mutation class at a time: auth data, app code, or service config.
- Restart only `btq-field-capture.service` for field-capture app deploys.

## Promotion Steps

1. On the Mac, inspect and test the BTQ repo changes that should reach
   production. The Mac repo is the source of truth; this app does not use a
   GitHub remote deploy path.

   ```bash
   cd /Users/operator/btq
   git status
   ./project/.venv/bin/python -m pytest tests/test_field_capture_auth.py tests/test_field_capture_ui.py
   ./scripts/lint-markdown
   ```

1. Copy the Mac source tree to the VPS source mirror. Do not copy runtime data,
   local virtualenvs, git internals, caches, or vault contents. The prompt-12
   deploy script handles this copy:

   ```bash
   cd /Users/operator/btq/project/field_capture
   ./deploy/push-and-deploy.sh
   ```

   For the initial static-serving migration, stage the static files before
   reloading Caddy, then deploy the API-only service after Caddy has been
   verified:

   ```bash
   ./deploy/push-and-deploy.sh --stage-static-only
   # deploy example.com Caddyfile
   ./deploy/push-and-deploy.sh --python-and-systemd
   ```

1. On the VPS, enter the copied BTQ source mirror. Do not use the
   `example.com` static site source tree for this app.

   ```bash
   cd /home/deploy/field-capture-source
   test -f project/field_capture/server.py
   test -x project/field_capture/deploy/deploy.sh
   ```

1. Run focused tests before deploying from the copied source mirror when
   practical. If the VPS does not have the test environment installed, rely on
   the Mac test run from step 1 and continue with the deploy script.

   ```bash
   ./project/.venv/bin/python -m pytest tests/test_field_capture_auth.py tests/test_field_capture_ui.py
   ./scripts/lint-markdown
   ```

1. Generate or update production auth data only when person/site/token data has changed:

   ```bash
   ./scripts/field-capture-prod-auth-data \
     --output-root /private/tmp/btq-field-capture-prod-auth
   ```

1. Deploy auth data separately from app code when needed:

   ```bash
   scp -r /private/tmp/btq-field-capture-prod-auth/srv/btq/data \
     deploy@vps.example.com:/tmp/btq-field-capture-data
   ```

   Then on the VPS as root:

   ```bash
   install -d -o btq-field -g btq-field -m 0750 /srv/btq/data
   rsync -a --delete /tmp/btq-field-capture-data/ /srv/btq/data/
   chown -R btq-field:btq-field /srv/btq/data
   chmod 0750 /srv/btq/data /srv/btq/data/vault-readonly
   find /srv/btq/data/vault-readonly -type d -exec chmod 0750 {} +
   find /srv/btq/data/vault-readonly -type f -exec chmod 0640 {} +
   chmod 0640 /srv/btq/data/field_capture_tokens.sqlite3
   ```

1. Deploy app code from the BTQ source mirror on the VPS:

   ```bash
   ./project/field_capture/deploy/deploy.sh
   ```

   If the deploy user has group write access to the app and backup trees, deploy
   without sudo:

   ```bash
   FIELD_CAPTURE_USE_SUDO=0 ./scripts/deploy-field-capture-app-on-vps
   ```

   Non-sudo mode runs a preflight before touching the live app tree. It verifies
   the target app directory, `project/field_capture` target directory, and backup
   path are writable and that the runtime directories already exist. It also
   disables rsync owner/group and permission preservation with
   `--no-owner --no-group --no-perms --omit-dir-times` so the deploy does not
   require `chown`, `chgrp`, or `chmod` privileges on existing app files.
   Service management still uses the narrow noninteractive sudoers path:
   `sudo -n /usr/bin/systemctl restart btq-field-capture.service` and
   `sudo -n /usr/bin/systemctl is-active btq-field-capture.service`. If those
   sudoers rules are missing, the deploy fails with a clear service-management
   error instead of prompting interactively.

   This script stages `project/field_capture/public/` into the static dist
   directory, rsyncs source into `/srv/btq/apps/field-capture`, installs
   `project/field_capture/config.production.json` as the production
   `config.json`, installs the systemd unit, and restarts only
   `btq-field-capture.service`.

1. If manually deploying instead of using the script, backup the current
   production app and service file before app or service changes:

   ```bash
   TS="$(date -u +%Y%m%dT%H%M%SZ)"
   cp -a /srv/btq/apps/field-capture "/srv/btq/apps/field-capture.backup.${TS}"
   cp -a /etc/systemd/system/btq-field-capture.service \
     "/etc/systemd/system/btq-field-capture.service.backup.${TS}"
   ```

1. Copy the current app files to `/srv/btq/apps/field-capture`.

   Use a staged release directory and `rsync -a --delete` from the staged release
   into `/srv/btq/apps/field-capture`. Preserve `/srv/btq/data`; it is not part
   of the app release. Install
   `project/field_capture/config.production.json` as
   `/srv/btq/apps/field-capture/config.json` so upload and queue defaults resolve
   under `/srv/btq/runtime`. Static PWA files are installed separately from
   `project/field_capture/public/` into `/srv/btq/apps/field-capture/dist/`.

1. Confirm the service command includes:

   ```text
   --db /srv/btq/data/field_capture_tokens.sqlite3
   --vault-root /srv/btq/data/vault-readonly
   serve --host 127.0.0.1 --port 8080
   ```

   The unit should also allow SQLite token metadata updates:

   ```text
   ReadWritePaths=/srv/btq/runtime /srv/btq/data
   ```

1. Reload systemd only if the service file changed:

   ```bash
   systemctl daemon-reload
   ```

1. Restart only field-capture:

   ```bash
   systemctl restart btq-field-capture.service
   systemctl status --no-pager btq-field-capture.service
   ```

1. Verify localhost `/api/session` from the VPS.

   Prefer an `Authorization: Bearer` header or a hidden shell prompt for the
   token. Do not put raw tokens into command history or logs.

1. Verify public `/api/session` from a normal client network:

   ```text
   https://photos.example.com/api/session?token=<TOKEN>
   ```

   The response should include the expected `person_id`, `token_id`, and site
   list.

1. Hard-refresh the browser and verify UI copy and behavior:

   - The form starts with Site and Area / QC Category selectors.
   - There is one prominent `Add Photo` button.
   - There is no camera preview panel, `Start camera` button, or round capture
     button.
   - The submit button posts to `/api/submit`; production `app.js` should contain
     `submitCapture` and should not contain `downloadJob`.
   - `/site/7050` renders the read-only Summit Wire upload viewer once uploads
     exist.

## Rollback

If the app or service fails verification:

1. Restore the app backup:

   ```bash
   rsync -a --delete /srv/btq/apps/field-capture.backup.<timestamp>/ \
     /srv/btq/apps/field-capture/
   ```

1. Restore the service file backup if the service file changed:

   ```bash
   cp -a /etc/systemd/system/btq-field-capture.service.backup.<timestamp> \
     /etc/systemd/system/btq-field-capture.service
   systemctl daemon-reload
   ```

1. Restart only field-capture:

   ```bash
   systemctl restart btq-field-capture.service
   systemctl status --no-pager btq-field-capture.service
   ```

Do not delete the failed release or backups until the cause is understood.
