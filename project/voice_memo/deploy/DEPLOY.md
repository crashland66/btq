# Voice Memo Deploy

## Service

- Domain: `voice.example.com`
- Port: `127.0.0.1:8092`
- Source: `/srv/voice.example.com/source`
- Static dist: `/srv/voice.example.com/dist`
- Audio data: `/srv/voice.example.com/data`
- Unit: `btq-voice-memo.service`

## Authentication

Voice memo authenticates against the **shared field-capture token store** —
the same per-person tokens issued from the ops dashboard `/tokens` page. A
request needs a token with submit permission; submissions are attributed to
the token's `person_id`. There is no longer a separate shared intake secret.

The token database defaults to `/srv/btq/data/field_capture_tokens.sqlite3`
(passed via `--token-db` in the systemd unit). The service runs under
`ProtectSystem=strict`, so that directory is granted in `ReadWritePaths` —
the server writes `last_used_at` on each authentication.

## Environment File

Create `/etc/btq/voice-memo.env` as `root:root` with mode `640`.
Format is plain `KEY=VALUE`, one per line, no quotes.

All keys are optional (CouchDB user/password are needed if CouchDB requires
auth). `VOICE_INTAKE_TOKEN` is no longer used and can be removed.

- `VOICE_COUCHDB_URL` - default `http://127.0.0.1:5984`
- `VOICE_COUCHDB_DB` - default `btq_voice_memos`
- `VOICE_COUCHDB_USER`
- `VOICE_COUCHDB_PASSWORD`
- `VOICE_COUCHDB_TIMEOUT` - default `10.0`
- `VOICE_MEMO_TOKEN_DB` - override the token database path (default above)

Example:

```text
VOICE_COUCHDB_URL=http://127.0.0.1:5984
VOICE_COUCHDB_USER=voice_memo
VOICE_COUCHDB_PASSWORD=replace-me
VOICE_COUCHDB_DB=btq_voice_memos
```

After editing:

```bash
sudo systemctl restart btq-voice-memo.service
```

## One-Time CouchDB Bootstrap

Use CouchDB admin credentials out-of-band. Do not commit them.

```bash
ADMIN_USER=admin
ADMIN_PASS=replace-me
VOICE_USER=voice_memo
VOICE_PASS=replace-me
COUCH=http://127.0.0.1:5984

curl -u "$ADMIN_USER:$ADMIN_PASS" -X PUT "$COUCH/_users/org.couchdb.user:$VOICE_USER" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$VOICE_USER\",\"password\":\"$VOICE_PASS\",\"roles\":[],\"type\":\"user\"}"

curl -u "$ADMIN_USER:$ADMIN_PASS" -X PUT "$COUCH/btq_voice_memos"

curl -u "$ADMIN_USER:$ADMIN_PASS" -X PUT "$COUCH/btq_voice_memos/_security" \
  -H 'Content-Type: application/json' \
  -d "{\"admins\":{\"names\":[],\"roles\":[]},\"members\":{\"names\":[\"$VOICE_USER\"],\"roles\":[]}}"
```

The `_security` payload grants the channel user reader and writer membership on
`btq_voice_memos` without making it a server admin.

## Picker Permissions

The voice-memo runtime user also needs reader access on `btq_sites` and
`btq_vault` so `/api/sites` and `/api/employees` can populate the PWA pickers
(employees read straight from the canonical vault since the btq_people
mirror was retired).

As CouchDB admin, on the VPS:

```bash
VOICE_USER="$(grep ^VOICE_COUCHDB_USER /etc/btq/voice-memo.env | cut -d= -f2)"

for db in btq_sites btq_vault; do
  curl -u admin:$ADMIN_PASS -X PUT \
    "http://127.0.0.1:5984/${db}/_security" \
    -H 'Content-Type: application/json' \
    -d "{\"members\": {\"names\": [\"${VOICE_USER}\"], \"roles\": []}, \"admins\": {\"names\": [], \"roles\": []}}"
done
```

If a `_security` document already exists, fetch it first, add the voice-memo
user to `members.names`, then PUT the merged document back. CouchDB overwrites
`_security` on PUT; it does not merge.

## Always Deploy From Local

Run this from `/Users/operator/btq/project/voice_memo/`:

```bash
./deploy/push-and-deploy.sh
```

The wrapper rsyncs the local source tree to the VPS first. Running
`deploy/deploy.sh` directly over SSH rebuilds from whatever source is already
on the VPS.

## Mac-Side Consumer In Existing Whisper Watcher

Voice memo intake is folded into the existing transcription watcher; there is
no separate launchd process for voice memos. Before each whisper inbox scan,
`transcription_pipeline.main` can poll `btq_voice_memos`, scp newly pending
audio into the configured `audio_inbox_dir`, and write a sibling
`vm-*.metadata.json` sidecar.

Enable it in the existing `com.operator.btq-whisper-watch` LaunchAgent by adding
these environment variables to that plist, then restart the watcher:

```xml
<key>VOICE_MEMO_CONSUMER_ENABLED</key>
<string>1</string>
<key>VOICE_MEMO_COUCHDB_URL</key>
<string>REPLACE_ME</string>
<key>VOICE_MEMO_COUCHDB_USER</key>
<string>REPLACE_ME</string>
<key>VOICE_MEMO_COUCHDB_PASSWORD</key>
<string>REPLACE_ME</string>
<key>VOICE_MEMO_REMOTE_HOST</key>
<string>deploy@vps.example.com</string>
```

Manual run for testing:

```bash
~/btq/scripts/btq watch-couchdb-voice-memos --once --json
```

Dry run:

```bash
~/btq/scripts/btq watch-couchdb-voice-memos --once --dry-run --json
```

Verify the existing watcher is running:

```bash
launchctl list | grep btq-whisper-watch
tail -f ~/btq_runtime/logs/whisper-watch.{out,err}.log
```
