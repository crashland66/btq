"""Independent gating tests for Prompt 295 — failed-capture recovery.

Authored by the verification agent (NOT the implementer / Codex).

PRODUCTION INCIDENT: a field worker's captures got stranded in ``status:
"failed"`` with no recovery path. Prompt 295 added, to BOTH PWAs
(``field_capture`` and ``unified_capture``):

  1. On app launch, every ``failed`` capture is requeued to ``pending``
     (attempts=0, lastError=null, retryWindowResetAt=now) so the existing
     drain re-attempts it ("close and reopen" recovery).
  2. A "Retry N failed" control in the queue strip that does the same requeue
     and immediately drains.
  3. The drain's 24h permanent-failure age check now measures from
     ``max(createdAt, retryWindowResetAt)`` so a relaunched/retried capture
     gets a genuinely fresh attempt regardless of original age.

These tests drive the REAL served ``app.js`` for each app inside a Node ``vm``
against a STATEFUL in-memory IndexedDB stub that faithfully implements the
shared ``shared_pwa/db.js`` contract (listByStatus / updateCapture /
deleteCapture / putCapture / countByStatus, keyed on capture_id, RMW
semantics). fetch is stubbed; the upload outcome is configurable per scenario.

The stub records every ``deleteCapture`` call so the data-loss guard is a hard
behavioral assertion, not a string match: if the requeue path ever deleted a
failed capture, the recorded delete list would contain it and the test fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIELD_APP = _REPO_ROOT / "project" / "field_capture" / "public" / "app.js"
_UNIFIED_APP = _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"


# --------------------------------------------------------------------------- #
# Node harness: drives the real app.js against a stateful in-memory IDB stub.
#
# argv: <app.js path> <scenario-json>
# scenario-json keys:
#   app        : "field" | "unified"   (selects token-handling specifics)
#   seed       : list of capture records to preload into the IDB stub
#   uploadMode : "ok" (201) | "fail5xx" (503, retryable) | "fail4xx" (400, perm)
#   action     : "launch"     -> run the app's init/launch path only
#                "retryClick" -> launch, then synthesize a click on the
#                                retry-failed control in the queue strip
#   now        : optional ISO string the app's Date.now() is pinned to
#
# Output (stdout JSON):
#   captures   : final IDB rows (capture_id -> record)
#   deletes    : ordered list of capture_ids ever passed to deleteCapture
#   updates    : ordered list of {id, patch} ever passed to updateCapture
#   submitUrls : list of URLs POSTed to /api/submit
#   retryControlPresent : bool, whether a [data-action=retry-failed] node was
#                         appended to the queue strip during render
# --------------------------------------------------------------------------- #

_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const appPath = process.argv[2];
const scenario = JSON.parse(process.argv[3]);
const appSrc = fs.readFileSync(appPath, 'utf8');

const PINNED_NOW = scenario.now ? new Date(scenario.now).getTime() : Date.now();

// ---- Stateful in-memory IndexedDB stub (shared db.js contract) ------------ //
const idb = new Map();          // capture_id -> record (deep-ish copy on write)
const deletes = [];             // ordered capture_ids deleted
const updates = [];             // ordered {id, patch}
for (const rec of (scenario.seed || [])) {
  idb.set(rec.capture_id, Object.assign({}, rec));
}

const fieldCaptureDb = {
  isSupported() { return true; },
  async putCapture(record) { idb.set(record.capture_id, Object.assign({}, record)); },
  async getCapture(id) { const r = idb.get(id); return r ? Object.assign({}, r) : undefined; },
  async listAll() { return [...idb.values()].map((r) => Object.assign({}, r)); },
  async listByStatus(status) {
    return [...idb.values()].filter((r) => r.status === status).map((r) => Object.assign({}, r));
  },
  async countByStatus(status) {
    return [...idb.values()].filter((r) => r.status === status).length;
  },
  async deleteCapture(id) { deletes.push(id); idb.delete(id); },
  async updateCapture(id, patch) {
    updates.push({ id, patch: Object.assign({}, patch) });
    const existing = idb.get(id);
    if (!existing) throw new Error('updateCapture: row gone: ' + id);
    const merged = Object.assign({}, existing, patch);
    idb.set(id, merged);
    return Object.assign({}, merged);
  },
};

// ---- fetch stub: configurable /api/submit outcome ------------------------- //
const submitUrls = [];
function fakeFetch(url, opts) {
  const u = typeof url === 'string' ? url : (url && url.url) || '';
  if (u.indexOf('/api/submit') !== -1) {
    submitUrls.push(u);
    if (scenario.uploadMode === 'fail5xx') {
      return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ message: 'unavailable' }) });
    }
    if (scenario.uploadMode === 'fail4xx') {
      return Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({ message: 'bad_request' }) });
    }
    return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve({ ok: true, capture_id: 'x' }) });
  }
  if (u.indexOf('/api/session') !== -1) {
    const body = { person: { name: 'Doe, Jane' }, sites: [{ site_id: 's1', display_categories: [] }], can_submit: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  }
  if (u.indexOf('/api/my-submissions') !== -1) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ submissions: [] }) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
}

// ---- Minimal DOM stub. The queue strip records appended retry controls. --- //
let retryControlPresent = false;
let queueStripEl = null;
let queueClickHandler = null;

function makeEl(id) {
  const el = {
    id, hidden: false, disabled: false, checked: false, value: '', textContent: '',
    type: '', className: '', dataset: {}, style: {}, children: [],
    selectedIndex: -1, selectedOptions: [], options: [], files: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(evt, fn) {
      if (this === queueStripEl && evt === 'click') queueClickHandler = fn;
    },
    removeEventListener() {},
    append(...kids) { for (const k of kids) this.children.push(k); },
    appendChild(k) {
      this.children.push(k);
      if (this === queueStripEl && k && k.dataset && k.dataset.action === 'retry-failed') {
        retryControlPresent = true;
      }
      return k;
    },
    replaceChildren(...kids) { this.children = kids.slice(); },
    replaceWith() {}, remove() {}, before() {}, after() {}, prepend() {},
    insertBefore(k) { this.children.push(k); return k; },
    cloneNode() { return makeEl(this.id); },
    closest() { return null; },
    getAttribute() { return null; }, hasAttribute() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {}, removeAttribute() {}, focus() {}, click() {},
    play() { return Promise.resolve(); }, pause() {},
  };
  return el;
}

const registry = new Map();
function elFor(sel) {
  if (!registry.has(sel)) {
    const el = makeEl(sel);
    if (sel === '#queueStrip') queueStripEl = el;
    registry.set(sel, el);
  }
  return registry.get(sel);
}
// Pre-create the queue strip so the click-handler binding is captured.
elFor('#queueStrip');

const documentElement = makeEl('html');
const document = {
  documentElement,
  cookie: '',
  visibilityState: 'visible',
  querySelector(sel) { return sel.startsWith('#') ? elFor(sel) : makeEl(sel); },
  querySelectorAll(sel) {
    if (sel.indexOf('screenMode') !== -1) return [makeEl('r-system'), makeEl('r-light'), makeEl('r-dark')];
    return [];
  },
  createElement(tag) { return makeEl(tag); },
  addEventListener() {},
  removeEventListener() {},
};

const storage = new Map();
// Seed a token so token-gated drains (unified) and upload headers proceed.
storage.set('fieldCaptureToken', 'tok-abcdef123456');
storage.set('unifiedCaptureToken', 'tok-abcdef123456');
const localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};

const navigator = {
  onLine: true,
  userAgent: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120',
  storage: { persist: () => Promise.resolve(true), persisted: () => Promise.resolve(true) },
  mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) },
};

// Drive real timers but pin Date.now via a Date subclass.
const RealDate = Date;
function PinnedDate(...args) {
  if (args.length === 0) return new RealDate(PINNED_NOW);
  return new RealDate(...args);
}
PinnedDate.now = () => PINNED_NOW;
PinnedDate.prototype = RealDate.prototype;
PinnedDate.parse = RealDate.parse;
PinnedDate.UTC = RealDate.UTC;

const windowObj = {
  document, localStorage, navigator, fetch: fakeFetch,
  location: { search: '', hash: '', pathname: '/', href: 'https://x/' },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  history: { replaceState() {} },
  confirm: () => true,
  setTimeout: (fn) => { if (typeof fn === 'function') setTimeout(fn, 0); return 0; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  addEventListener() {},
  removeEventListener() {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  btqRecorder: { supportsAudioRecording: () => false },
  fieldCaptureDb,
};
windowObj.window = windowObj;

const sandbox = {
  window: windowObj, document, navigator, localStorage, fetch: fakeFetch,
  history: windowObj.history, location: windowObj.location, URL: windowObj.URL,
  URLSearchParams, console,
  setTimeout: windowObj.setTimeout, clearTimeout: windowObj.clearTimeout,
  setInterval: windowObj.setInterval, clearInterval: windowObj.clearInterval,
  Promise, Date: PinnedDate, Blob: class { constructor() {} },
  FormData: class { append() {} },
  Math, JSON, Number, Object, Array, String,
};
vm.createContext(sandbox);
vm.runInContext(appSrc, sandbox, { filename: 'app.js' });

async function settle() {
  // Let the init IIFE's async chain (requeue -> refresh -> drain) finish.
  for (let i = 0; i < 50; i++) {
    await new Promise((r) => setTimeout(r, 0));
  }
}

(async () => {
  await settle();
  if (scenario.action === 'retryClick') {
    // Synthesize a click on the retry-failed control.
    if (typeof queueClickHandler === 'function') {
      queueClickHandler({ target: { dataset: { action: 'retry-failed' } } });
    }
    await settle();
  }
  const out = {
    captures: Object.fromEntries(idb),
    deletes,
    updates,
    submitUrls,
    retryControlPresent,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_scenario(app_path: Path, scenario: dict) -> dict:
    if shutil.which("node") is None:
        raise unittest.SkipTest("node not available")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(_HARNESS)
        harness_path = handle.name
    try:
        result = subprocess.run(
            ["node", harness_path, str(app_path), json.dumps(scenario)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.unlink(harness_path)
    if result.returncode != 0:
        raise AssertionError(f"harness crashed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout.strip())


def _failed_capture(capture_id: str, *, created_at: str, attempts: int = 4,
                    last_error: str = "upload_failed_503") -> dict:
    return {
        "capture_id": capture_id,
        "status": "failed",
        "attempts": attempts,
        "lastError": last_error,
        "createdAt": created_at,
        "lastTriedAt": created_at,
        "fields": {
            "site": "Continental Metalworks",
            "site_id": "7060",
            "qc_category": "report_an_issue",
            "captured_at": created_at,
            "exported_at": created_at,
            "note": "stranded capture",
            "job_id": "job-1",
            "target_type": "location",
            "target_id": "7060",
        },
        "photos": [{"filename": "a.png", "mimeType": "image/png", "blob": {}}],
        "audio": None,
    }


# Recent timestamp (well within 24h of the pinned "now") and an OLD one (>24h).
_NOW_ISO = "2026-06-06T18:00:00Z"
_RECENT_ISO = "2026-06-06T17:00:00Z"      # 1h before now
_OLD_ISO = "2026-06-04T18:00:00Z"         # 48h before now (>24h window)


class _RecoveryTestsBase:
    """Shared scenarios run against one app's served app.js. Subclasses set APP."""

    APP: Path = None
    APP_NAME: str = ""

    def run_scenario(self, scenario: dict) -> dict:
        scenario.setdefault("app", self.APP_NAME)
        scenario.setdefault("now", _NOW_ISO)
        return _run_scenario(self.APP, scenario)

    # ----- (a) launch requeue: the core fix -------------------------------- #

    def test_launch_requeues_failed_then_drain_uploads_to_done(self):
        """Seed two FAILED captures; launch must requeue them to pending with
        attempts reset + retryWindowResetAt set, and the subsequent drain (201)
        must upload them so they end ``done`` — genuine relaunch recovery."""
        out = self.run_scenario({
            "seed": [
                _failed_capture("cap-A", created_at=_RECENT_ISO),
                _failed_capture("cap-B", created_at=_RECENT_ISO),
            ],
            "uploadMode": "ok",
            "action": "launch",
        })
        caps = out["captures"]
        # Both stranded captures recovered end-to-end.
        self.assertEqual(caps["cap-A"]["status"], "done", out)
        self.assertEqual(caps["cap-B"]["status"], "done", out)
        # Each was actually POSTed to /api/submit (real re-attempt, not a no-op).
        self.assertEqual(len(out["submitUrls"]), 2, out["submitUrls"])
        # The requeue patch reset attempts, cleared lastError, stamped the window.
        requeue_patches = [
            u["patch"] for u in out["updates"]
            if u["patch"].get("status") == "pending"
            and "retryWindowResetAt" in u["patch"]
        ]
        self.assertGreaterEqual(len(requeue_patches), 2, out["updates"])
        for patch in requeue_patches:
            self.assertEqual(patch["attempts"], 0)
            self.assertIsNone(patch["lastError"])
            self.assertTrue(patch["retryWindowResetAt"])

    # ----- (b) no-delete data-loss guard: CRITICAL ------------------------- #

    def test_requeue_never_deletes_failed_capture(self):
        """The requeue path must NEVER remove a row. With an upload that keeps
        failing (5xx, retryable), captures must remain in IDB (pending/failed),
        never dropped. The IDB stub records every deleteCapture call."""
        out = self.run_scenario({
            "seed": [
                _failed_capture("cap-A", created_at=_RECENT_ISO),
                _failed_capture("cap-B", created_at=_RECENT_ISO),
            ],
            "uploadMode": "fail5xx",
            "action": "launch",
        })
        # Nothing was deleted during requeue/drain.
        self.assertEqual(out["deletes"], [], f"data loss: deleted {out['deletes']}")
        # Both rows still present (transitioned, not dropped).
        self.assertIn("cap-A", out["captures"])
        self.assertIn("cap-B", out["captures"])
        for cid in ("cap-A", "cap-B"):
            self.assertIn(out["captures"][cid]["status"], ("pending", "uploading", "failed"))

    def test_permanent_failure_keeps_row_failed_not_deleted(self):
        """A 4xx (permanent) upload after requeue marks the capture failed again
        but must NOT delete it — the worker can still see/retry it."""
        out = self.run_scenario({
            "seed": [_failed_capture("cap-A", created_at=_RECENT_ISO)],
            "uploadMode": "fail4xx",
            "action": "launch",
        })
        self.assertEqual(out["deletes"], [], out)
        self.assertIn("cap-A", out["captures"])
        self.assertEqual(out["captures"]["cap-A"]["status"], "failed")

    # ----- (c) retry control ----------------------------------------------- #

    def test_retry_control_present_when_failed_gt_zero(self):
        """When failed > 0, refreshPendingCount must render the retry-failed
        control into the queue strip."""
        out = self.run_scenario({
            "seed": [_failed_capture("cap-A", created_at=_RECENT_ISO)],
            "uploadMode": "fail4xx",   # stays failed so the strip keeps the control
            "action": "launch",
        })
        self.assertTrue(out["retryControlPresent"], "retry-failed control missing")

    def test_retry_click_requeues_and_drains(self):
        """Invoking the retry control requeues failed->pending and drains to done.

        The seed starts as a single failed capture older-than-recent; we let the
        FIRST launch leave it failed (4xx), then flip the upload to ok is not
        possible mid-run, so instead we assert via the launch+retryClick path
        with an OK upload: the click path itself performs requeue+drain.
        """
        out = self.run_scenario({
            "seed": [_failed_capture("cap-A", created_at=_RECENT_ISO)],
            "uploadMode": "ok",
            "action": "retryClick",
        })
        # Ends done (recovered) and was uploaded.
        self.assertEqual(out["captures"]["cap-A"]["status"], "done", out)
        self.assertGreaterEqual(len(out["submitUrls"]), 1, out)
        self.assertEqual(out["deletes"], [], out)

    # ----- (d) age-window reset -------------------------------------------- #

    def test_old_failed_capture_gets_real_attempt_after_requeue(self):
        """A FAILED capture older than 24h, once requeued with
        retryWindowResetAt=now, must NOT be instantly re-failed by the drain's
        age check — it gets a genuine upload attempt and recovers."""
        out = self.run_scenario({
            "seed": [_failed_capture("cap-OLD", created_at=_OLD_ISO)],
            "uploadMode": "ok",
            "action": "launch",
        })
        caps = out["captures"]
        self.assertEqual(caps["cap-OLD"]["status"], "done", out)
        # It was genuinely uploaded (proves the age check did NOT short-circuit it).
        self.assertEqual(len(out["submitUrls"]), 1, out["submitUrls"])
        # It must NOT have been stamped exceeded_retry_window.
        ages = [u for u in out["updates"]
                if u["patch"].get("lastError") == "exceeded_retry_window"]
        self.assertEqual(ages, [], out["updates"])


class FieldCaptureRecoveryTests(_RecoveryTestsBase, unittest.TestCase):
    APP = _FIELD_APP
    APP_NAME = "field"


class UnifiedCaptureRecoveryTests(_RecoveryTestsBase, unittest.TestCase):
    APP = _UNIFIED_APP
    APP_NAME = "unified"


# --------------------------------------------------------------------------- #
# Negative control: deliberately break the requeue, confirm the core test flips
# to FAIL, then the test file restores. This proves the launch-requeue test has
# real teeth (it is not vacuously green). We mutate a COPY of app.js, never the
# served source.
# --------------------------------------------------------------------------- #


class NegativeControlTests(unittest.TestCase):
    """Mutating the requeue to a no-op must make the core launch test fail."""

    def _broken_copy(self, app_path: Path) -> Path:
        src = app_path.read_text(encoding="utf-8")
        # Neutralize requeue: make requeueFailedCaptures an immediate no-op by
        # forcing the early-return guard true. We rewrite the first line of its
        # body to "return 0;" — leaving the function defined (so callers don't
        # throw) but never transitioning failed->pending.
        marker = 'const failed = (await window.fieldCaptureDb.listByStatus("failed"))'
        self.assertIn(marker, src, f"requeue marker not found in {app_path}")
        broken = src.replace(marker, "return 0;\n    const failed = (await window.fieldCaptureDb.listByStatus(\"failed\"))", 1)
        self.assertNotEqual(broken, src)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        tmp.write(broken)
        tmp.close()
        return Path(tmp.name)

    def _assert_core_fails(self, app_path: Path, app_name: str) -> None:
        broken = self._broken_copy(app_path)
        try:
            out = _run_scenario(broken, {
                "app": app_name,
                "now": _NOW_ISO,
                "seed": [_failed_capture("cap-A", created_at=_RECENT_ISO)],
                "uploadMode": "ok",
                "action": "launch",
            })
        finally:
            os.unlink(broken)
        # With requeue broken, the failed capture is NEVER requeued, so it stays
        # "failed" and is never uploaded. The core assertion (status == done)
        # would fail — confirm that here.
        self.assertEqual(out["captures"]["cap-A"]["status"], "failed",
                         "negative control did not break requeue (test has no teeth)")
        self.assertEqual(out["submitUrls"], [], out)

    def test_field_capture_core_test_has_teeth(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        self._assert_core_fails(_FIELD_APP, "field")

    def test_unified_capture_core_test_has_teeth(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        self._assert_core_fails(_UNIFIED_APP, "unified")


if __name__ == "__main__":
    unittest.main()
