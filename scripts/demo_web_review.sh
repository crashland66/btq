#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BTQ="${SCRIPT_DIR}/btq"
INPUT="project/demo/web_review_demo.md"
JOURNAL_DIR="${REPO_ROOT}/runtime/journal"

cd "${REPO_ROOT}"

echo "== Step 1: compose structured skill prompt and queue preview =="
"${BTQ}" skill run web-review \
  --version v2 \
  --input "${INPUT}" \
  --structured \
  --to-queue-dry-run \
  --out /tmp/demo_output.md \
  --out-queue /tmp/demo_queue.json

echo "Wrote /tmp/demo_output.md"
echo "Wrote /tmp/demo_queue.json"

mkdir -p "${JOURNAL_DIR}"
BEFORE_FILE="$(mktemp /tmp/btq-demo-before.XXXXXX)"
find "${JOURNAL_DIR}" -maxdepth 1 -type f -name "*.json" -print | sort > "${BEFORE_FILE}"

echo "== Step 2: run guarded safe execution =="
"${BTQ}" skill run web-review \
  --version v2 \
  --input "${INPUT}" \
  --structured \
  --to-queue-dry-run \
  --execute \
  --mode auto-safe

AFTER_FILE="$(mktemp /tmp/btq-demo-after.XXXXXX)"
find "${JOURNAL_DIR}" -maxdepth 1 -type f -name "*.json" -print | sort > "${AFTER_FILE}"
LATEST_JOURNAL="$(comm -13 "${BEFORE_FILE}" "${AFTER_FILE}" | tail -n 1)"

if [[ -z "${LATEST_JOURNAL}" ]]; then
  LATEST_JOURNAL="$(find "${JOURNAL_DIR}" -maxdepth 1 -type f -name "*.json" -print | sort | tail -n 1)"
fi

if [[ -z "${LATEST_JOURNAL}" ]]; then
  echo "No journal file was produced." >&2
  exit 1
fi

echo "Latest journal: ${LATEST_JOURNAL}"
echo "== Step 3: replay journal with diff =="
"${BTQ}" replay "${LATEST_JOURNAL}" --diff
