"""Swipe-review surface: one proposed job at a time.

This is a thin *front-end* over the existing review pipeline. It introduces no
new write path and no new approval semantics. A swipe-approve posts to the same
``/field-capture/review/approve`` endpoint the table-based review page uses, so
the invariant holds unchanged:

    AI proposes job_draft -> operator approves -> deterministic writer
    materializes the approved job_draft into the runtime queue -> queue
    processor writes canonical CouchDB state.

The card shows only what the operator needs to decide "is this proposed change
true enough to commit?": what happened, who/what it affects, the proposed
mutation, confidence, and the source evidence. Pipeline internals stay hidden.

Approve and Reject are live. Edit and Defer are intentionally rendered as
disabled for this prompt; draft editing is handled by the next review increment.
Showing them as stubs (rather than fake buttons) keeps the surface honest about
what actually commits.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from field_capture import job_draft_review
from ops_dashboard.common import (
    UNKNOWN_SUBMITTER,
    capture_thumbnails,
    render_relative_time,
    resolve_site_label,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page
from queue_spec import validate_job


QUEUE_NEEDS_APPROVAL = "pending_approval"
QUEUE_APPROVED = "approved"
QUEUE_REJECTED = "rejected"


def _age_seconds_from_timestamp(value: object) -> int:
    timestamp = str(value or "").strip()
    if not timestamp:
        return 0
    try:
        created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0, int(datetime.now(timezone.utc).timestamp() - created_at.timestamp()))


def _confidence_label(value: object) -> str:
    text = str(value if value is not None else "").strip().lower()
    if not text or text == "unknown":
        return "unknown"
    return text


def _plain_site_label(html_label: str) -> str:
    """resolve_site_label returns HTML (<span> markup); the swipe card renders the
    site through JS esc(), so it needs the plain text ("Liberty Wire (1337)")."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_label or "")).strip()


def swipe_card(
    path: Path,
    payload: dict[str, object],
    *,
    runtime_root: Path,
    submitters: dict[str, dict[str, str]],
) -> dict[str, object]:
    """Reduce a job_draft artifact to the minimal card the operator decides on."""
    capture_id = str(payload.get("source_capture_id") or "")
    submitter = submitters.get(capture_id, {})

    proposed_job_type = str(payload.get("job_type") or "")
    proposed_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    proposed_error = str(payload.get("validation_error") or "")
    if not proposed_error and not validate_job({"job_type": proposed_job_type, "payload": proposed_payload}):
        proposed_error = "job_draft fails queue_spec validation"
    approvable = (
        str(payload.get("review_status") or "") == QUEUE_NEEDS_APPROVAL
        and not proposed_error
        and bool(proposed_job_type)
        and isinstance(proposed_payload, dict)
    )

    return {
        "draft_id": str(payload.get("draft_id") or ""),
        "review_status": str(payload.get("review_status") or ""),
        "site_id": str(payload.get("site_id") or ""),
        "area": "",
        "submitter_name": str(
            payload.get("submitter_name") or submitter.get("submitter_name") or UNKNOWN_SUBMITTER
        ),
        "captured_at": str(payload.get("created_at") or ""),
        "summary": str(payload.get("message") or ""),
        "rationale": "",
        "confidence": _confidence_label(payload.get("confidence")),
        "evidence": str(payload.get("message") or ""),
        # The worker's actual message + the photos they captured: the human
        # context the operator decides on (vs the technical proposed mutation).
        "message": str(payload.get("message") or ""),
        "photos": capture_thumbnails(runtime_root, capture_id),
        "proposed_job_type": str(proposed_job_type or ""),
        "proposed_payload": proposed_payload if isinstance(proposed_payload, dict) else {},
        "proposed_error": str(proposed_error or ""),
        "approvable": approvable,
        "age_seconds": _age_seconds_from_timestamp(payload.get("created_at")),
        "artifact_path": str(path),
        "_rev": str(payload.get("_rev") or ""),
    }


def collect_cards(runtime_root: Path, *, status: str = QUEUE_NEEDS_APPROVAL) -> list[dict[str, object]]:
    submitters = submitters_by_capture(runtime_root)
    cards: list[dict[str, object]] = []
    for path, payload in job_draft_review.couchdb_job_draft_payloads(review_status=status):
        if payload.get("type") != "job_draft_review":
            continue
        cards.append(swipe_card(path, payload, runtime_root=runtime_root, submitters=submitters))
    # Oldest first: the operator clears the backlog tail, and ordering is stable
    # for the keyboard-driven single-card flow.
    cards.sort(key=lambda card: (-int(card["age_seconds"]), str(card["draft_id"])))
    return cards


def queue_counts(runtime_root: Path) -> dict[str, int]:
    counts = {status: 0 for status in (QUEUE_NEEDS_APPROVAL, QUEUE_APPROVED, QUEUE_REJECTED)}
    for _path, payload in job_draft_review.couchdb_job_draft_payloads():
        status = str(payload.get("review_status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def swipe_payload(runtime_root: Path, *, counts: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts if counts is not None else queue_counts(runtime_root),
        "cards": collect_cards(runtime_root),
    }


def render_body(request_ctx: object, *, payload: dict[str, object] | None = None) -> str:
    runtime_root = getattr(request_ctx, "runtime_root", Path("."))
    vault_root = getattr(getattr(request_ctx, "config", None), "vault_dir", None)
    if payload is None:
        payload = swipe_payload(runtime_root)
    cards = payload["cards"] if isinstance(payload["cards"], list) else []
    counts = payload["counts"] if isinstance(payload["counts"], dict) else {}

    # Resolve site labels server-side so the bootstrap data already carries a
    # human-readable site name; the client never has to call back for it.
    if vault_root is not None:
        for card in cards:
            card["site_label"] = _plain_site_label(resolve_site_label(card.get("site_id"), vault_root))
    else:
        for card in cards:
            card["site_label"] = str(card.get("site_id") or "")

    bootstrap = json.dumps({"cards": cards, "counts": counts}, sort_keys=True)
    # Embed the JSON in <script type="application/json"> WITHOUT html.escape: the
    # browser does not decode character references inside a <script>, so escaping
    # the JSON's quotes to &quot; makes JSON.parse throw and the card stack render
    # empty (the "31 needs approval / Nothing waiting" bug). Escape only the chars
    # that could break out of the script context; the result stays valid JSON.
    bootstrap_safe = (
        bootstrap.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    needs_approval = int(counts.get("pending_approval", 0))
    rejected = int(counts.get("rejected", 0))

    return f"""
    <header class="swipe-header">
      <h1>Review</h1>
      <p class="muted">One proposed job at a time. Decide whether it is true enough to commit.</p>
      <div class="swipe-queues" role="status">
        <a class="swipe-queue" data-queue="approval" href="/field-capture/review?status=pending_approval" aria-label="{needs_approval} drafts need approval"><strong>{needs_approval}</strong> needs approval</a>
        <a class="swipe-queue" data-queue="rejected" href="/field-capture/review?status=rejected" aria-label="{rejected} rejected drafts"><strong>{rejected}</strong> rejected</a>
      </div>
    </header>

    <div class="swipe-reviewer">
      <label for="swipe-reviewer-input">Reviewer</label>
      <input id="swipe-reviewer-input" type="text" autocomplete="name" spellcheck="false"
             placeholder="Your name — recorded on every approval">
    </div>

    <section class="swipe-stage" aria-live="polite">
      <div id="swipe-card-mount"></div>
      <div id="swipe-empty" class="swipe-empty" hidden>
        <p class="swipe-empty-mark">✓</p>
        <p>Nothing waiting for approval.</p>
      </div>
    </section>

    <p class="swipe-help muted">
      Keyboard: <kbd>A</kbd> or <kbd>→</kbd> approve · <kbd>R</kbd> or <kbd>←</kbd> reject · <kbd>S</kbd> or <kbd>↓</kbd> skip · <kbd>U</kbd> mark unknown
    </p>

    <form id="swipe-action-form" method="post" hidden>
      <input type="hidden" name="draft_id" value="">
      <input type="hidden" name="_rev" value="">
      <input type="hidden" name="reviewer" value="">
      <input type="hidden" name="rationale" value="">
    </form>

    <script>{_SWIPE_SCRIPT}</script>
    <script id="swipe-bootstrap" type="application/json">{bootstrap_safe}</script>
    <script>window.__btqSwipeInit && window.__btqSwipeInit();</script>
    """


def render(request_ctx: object) -> str:
    body = render_body(request_ctx)
    return html_page("BTQ Review", body, active_section="swipe", refresh=False)


# The client is deliberately dependency-free and small. It renders one card,
# captures the operator's reviewer name once (persisted in localStorage so the
# audit trail attributes every decision), and submits to the existing
# approve/reject endpoints. On a 2xx/3xx it advances to the next card without a
# full page reload so the flow stays fast.
_SWIPE_SCRIPT = r"""
window.__btqSwipeInit = function () {
  var mount = document.getElementById('swipe-card-mount');
  var empty = document.getElementById('swipe-empty');
  var bootstrapEl = document.getElementById('swipe-bootstrap');
  if (!mount || !bootstrapEl) return;
  var data = {};
  try { data = JSON.parse(bootstrapEl.textContent || '{}'); } catch (e) { data = {}; }
  var cards = (data && data.cards) || [];
  var index = 0;

  // Inline reviewer field: prefill from localStorage and persist on every edit,
  // so the audit trail attributes each decision without a (suppressible) prompt.
  (function () {
    var input = document.getElementById('swipe-reviewer-input');
    if (!input) return;
    try { input.value = (window.localStorage && localStorage.getItem('btq-reviewer')) || ''; } catch (e) {}
    input.addEventListener('input', function () {
      try { if (window.localStorage) localStorage.setItem('btq-reviewer', input.value.trim()); } catch (e) {}
    });
  })();

  function reviewerName() {
    // Read from the inline reviewer field / localStorage only. NEVER use a browser
    // prompt in a loop: if the user dismisses or suppresses it ("Don't ask again")
    // the call returns null forever and the tab hard-freezes.
    var input = document.getElementById('swipe-reviewer-input');
    if (input && input.value.trim()) {
      var typed = input.value.trim();
      try { if (window.localStorage) localStorage.setItem('btq-reviewer', typed); } catch (e) {}
      return typed;
    }
    var name = '';
    try { name = window.localStorage ? localStorage.getItem('btq-reviewer') || '' : ''; } catch (e) {}
    return name.trim();
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function payloadRows(p) {
    var keys = Object.keys(p || {});
    if (!keys.length) return '';
    var rows = keys.map(function (k) {
      var v = p[k];
      if (v && typeof v === 'object') v = JSON.stringify(v);
      return '<tr><th>' + esc(k) + '</th><td>' + esc(v) + '</td></tr>';
    }).join('');
    return '<table class="swipe-payload">' + rows + '</table>';
  }

  function render() {
    if (index >= cards.length) {
      mount.innerHTML = '';
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    var c = cards[index];
    var remaining = cards.length - index;
    var site = c.site_label || c.site_id || 'Unknown site';
    var confClass = 'conf-' + esc(c.confidence || 'unknown');
    var approveDisabled = c.approvable ? '' : ' disabled';
    var approveHint = c.approvable ? 'Approve and stage this job'
      : (c.proposed_error || 'Cannot approve: the proposed job is invalid');

    var warn = c.approvable ? '' :
      '<p class="swipe-warn">⚠ ' + esc(c.proposed_error || 'Proposed job is invalid') + '</p>';

    var thumbs = (c.photos && c.photos.length)
      ? '<div class="swipe-thumbs">' + c.photos.map(function (u) {
          return '<img class="candidate-thumb" src="' + esc(u) + '" alt="capture photo" loading="lazy">';
        }).join('') + '</div>'
      : '';

    // The card leads with the human context -- the worker's message, who sent
    // it, for which site, and the photos -- not the technical proposed mutation.
    mount.innerHTML =
      '<article class="swipe-card" data-draft-id="' + esc(c.draft_id) + '">' +
        '<div class="swipe-card-top">' +
          '<span class="swipe-progress">' + remaining + ' left</span>' +
          (c.captured_at ? '<span>' + esc(c.captured_at) + '</span>' : '') +
        '</div>' +
        '<p class="swipe-message">' + esc(c.message || c.summary || '(no message)') + '</p>' +
        '<p class="swipe-meta muted">' + esc(c.submitter_name || 'Unknown submitter') +
          ' · ' + esc(site) + (c.area ? ' · ' + esc(c.area) : '') + '</p>' +
        thumbs +
        warn +
        '<div class="swipe-actions">' +
          '<button type="button" class="swipe-btn reject" data-act="reject" title="Reject (R / Left)">Reject</button>' +
          '<button type="button" class="swipe-btn skip" data-act="skip" title="Skip without acting — leaves it pending (S / Down)">Skip</button>' +
          '<button type="button" class="swipe-btn edit" disabled title="Draft editing is enabled in the next review increment">Edit</button>' +
          '<button type="button" class="swipe-btn defer" disabled title="Defer is not a draft review action">Defer</button>' +
          '<button type="button" class="swipe-btn approve" data-act="approve"' + approveDisabled +
            ' title="' + esc(approveHint) + '">Approve</button>' +
        '</div>' +
      '</article>';

    var card = mount.querySelector('.swipe-card');
    card.querySelectorAll('button[data-act]').forEach(function (btn) {
      btn.addEventListener('click', function () { decide(c, btn.getAttribute('data-act'), btn); });
    });
  }

  function decide(card, action, btn) {
    if (btn && btn.disabled) return;
    // Skip just advances to the next card without writing anything; the
    // Skip just advances client-side; the draft stays pending_approval and reappears on reload.
    if (action === 'skip') { index += 1; render(); return; }
    if (action === 'approve' && !card.approvable) return;
    var reviewer = reviewerName();
    if (!reviewer) {
      var ri = document.getElementById('swipe-reviewer-input');
      if (ri) { ri.focus(); }
      window.alert('Enter your name in the "Reviewer" field above first — it is recorded on every approval.');
      return;
    }
    var route = action === 'approve' ? '/field-capture/review/approve' : '/field-capture/review/reject';
    var body = new URLSearchParams();
    body.set('draft_id', card.draft_id);
    body.set('_rev', card._rev || '');
    body.set('reviewer', reviewer);
    body.set('rationale', action === 'unknown' ? 'mark unknown' : '');
    var stage = mount.parentElement;
    stage.classList.add(action === 'approve' ? 'swiping-right' : 'swiping-left');
    fetch(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
      redirect: 'manual'
    }).then(function (resp) {
      // 303 redirect (opaqueredirect under manual) or 200 both mean the
      // backend accepted and staged. Advance locally.
      if (resp.status === 0 || (resp.status >= 200 && resp.status < 400)) {
        index += 1;
        stage.classList.remove('swiping-right', 'swiping-left');
        render();
      } else {
        stage.classList.remove('swiping-right', 'swiping-left');
        resp.text().then(function (t) { window.alert('Action failed: ' + (t || resp.status)); });
      }
    }).catch(function (err) {
      stage.classList.remove('swiping-right', 'swiping-left');
      window.alert('Action failed: ' + err);
    });
  }

  document.addEventListener('keydown', function (e) {
    if (index >= cards.length) return;
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    var c = cards[index];
    if (e.key === 'a' || e.key === 'A' || e.key === 'ArrowRight') {
      if (c.approvable) decide(c, 'approve', null);
    } else if (e.key === 'r' || e.key === 'R' || e.key === 'ArrowLeft') {
      decide(c, 'reject', null);
    } else if (e.key === 'u' || e.key === 'U') {
      decide(c, 'unknown', null);
    } else if (e.key === 's' || e.key === 'S' || e.key === 'ArrowDown') {
      decide(c, 'skip', null);
    }
  });

  render();
};
"""
