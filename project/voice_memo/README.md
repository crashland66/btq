# voice_memo

Foreground-only voice memo PWA and intake server for channel 3 of the capture
pipeline. The browser records audio, POSTs it to `voice.example.com`, stores
audio bytes on the VPS filesystem, and writes metadata to CouchDB.

This is the channel-3 capture intake for BTQ. Source moved to
`btq/project/voice_memo/` from `WebsiteProjects/voice_memo/` on 2026-05-10
(prompt 08).

## Deploy

```sh
./deploy/push-and-deploy.sh
```

The deploy syncs this local project to `/home/deploy/voice-memo-source/` and
runs the remote deploy script. The Caddy vhost is managed from
`example.com/deploy/Caddyfile.proposed`, so deploy `example.com` after
changing that file.

## Environment

Secrets live on the VPS in `/etc/btq/voice-memo.env`.

Required:

- `VOICE_INTAKE_TOKEN`

Optional:

- `VOICE_COUCHDB_URL` - default `http://127.0.0.1:5984`
- `VOICE_COUCHDB_DB` - default `btq_voice_memos`
- `VOICE_COUCHDB_USER`
- `VOICE_COUCHDB_PASSWORD`
- `VOICE_COUCHDB_TIMEOUT` - default `10.0`

Audio bytes live under `/srv/voice.example.com/data`.

To revoke a token, change `VOICE_INTAKE_TOKEN` in the env file and restart:

```sh
sudo systemctl restart btq-voice-memo.service
```
