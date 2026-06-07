#!/usr/bin/env bash
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/project/docs/queue_authoring_guide.md"
DST="${BTPIPELINE_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline}/specs/QUEUE_JOB_SPEC.md"

mkdir -p "$(dirname "$DST")"
{
  printf '%s\n\n' '> **This file is a generated mirror of `project/docs/queue_authoring_guide.md` in the BT-Pipeline repository. Do not hand-edit. Updates land via `scripts/sync_btpipeline_spec.sh`.**'
  cat "$SRC"
} > "$DST"

echo "Synced $SRC -> $DST"
