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

Approve, Reject, and Edit are live. Multi-job groups are reviewed as a
checklist: checked drafts are approved, unchecked drafts are rejected, and each
draft remains its own decision.
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


def _draft_detail(payload: dict[str, object]) -> dict[str, object]:
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
        "job_type": proposed_job_type,
        "payload": proposed_payload if isinstance(proposed_payload, dict) else {},
        "validation_error": proposed_error,
        "_rev": str(payload.get("_rev") or ""),
        "approvable": approvable,
    }


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
    draft = _draft_detail(payload)

    return {
        "draft_id": draft["draft_id"],
        "group_id": str(payload.get("group_id") or capture_id or draft["draft_id"]),
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
        "proposed_job_type": str(draft["job_type"] or ""),
        "proposed_payload": draft["payload"],
        "proposed_error": str(draft["validation_error"] or ""),
        "approvable": bool(draft["approvable"]),
        "drafts": [draft],
        "age_seconds": _age_seconds_from_timestamp(payload.get("created_at")),
        "artifact_path": str(path),
        "_rev": str(draft["_rev"] or ""),
    }


def collect_cards(runtime_root: Path, *, status: str = QUEUE_NEEDS_APPROVAL) -> list[dict[str, object]]:
    submitters = submitters_by_capture(runtime_root)
    grouped: dict[str, dict[str, object]] = {}
    for path, payload in job_draft_review.couchdb_job_draft_payloads(review_status=status):
        if payload.get("type") != "job_draft_review":
            continue
        card = swipe_card(path, payload, runtime_root=runtime_root, submitters=submitters)
        group_id = str(card.get("group_id") or card.get("draft_id") or "")
        if group_id not in grouped:
            grouped[group_id] = card
            continue
        existing = grouped[group_id]
        drafts = existing.get("drafts") if isinstance(existing.get("drafts"), list) else []
        drafts.extend(card.get("drafts") if isinstance(card.get("drafts"), list) else [])
        existing["drafts"] = drafts
        existing["age_seconds"] = max(int(existing.get("age_seconds") or 0), int(card.get("age_seconds") or 0))
        existing["approvable"] = any(bool(draft.get("approvable")) for draft in drafts if isinstance(draft, dict))
        if not existing.get("proposed_error"):
            existing["proposed_error"] = next(
                (str(draft.get("validation_error") or "") for draft in drafts if isinstance(draft, dict) and draft.get("validation_error")),
                "",
            )
    cards = list(grouped.values())
    for card in cards:
        drafts = card.get("drafts") if isinstance(card.get("drafts"), list) else []
        drafts.sort(key=lambda draft: str(draft.get("draft_id") or "") if isinstance(draft, dict) else "")
    # Oldest first: the operator clears the backlog tail, and ordering is stable
    # for the keyboard-driven single-card flow.
    cards.sort(key=lambda card: (-int(card["age_seconds"]), str(card["group_id"]), str(card["draft_id"])))
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
      if (Array.isArray(v)) v = v.join(', ');
      else if (v && typeof v === 'object') v = JSON.stringify(v);
      return '<tr><th>' + esc(k) + '</th><td>' + esc(v) + '</td></tr>';
    }).join('');
    return '<table class="swipe-payload">' + rows + '</table>';
  }

  function prettyPayload(p) {
    try { return JSON.stringify(p || {}, null, 2); } catch (e) { return '{}'; }
  }

  function shortPayloadSummary(p) {
    var keys = Object.keys(p || {});
    if (!keys.length) return '(empty payload)';
    return keys.slice(0, 3).map(function (k) {
      var v = p[k];
      if (v && typeof v === 'object') v = JSON.stringify(v);
      return k + '=' + String(v);
    }).join(', ');
  }

  function editDomId(draftId, index) {
    var raw = String(draftId || ('draft-' + index));
    return 'swipe-edit-' + raw.replace(/[^A-Za-z0-9_-]/g, '-');
  }

  function draftEditForm(d, id) {
    return '<form class="swipe-edit-form" method="post" action="/field-capture/review/edit" hidden id="' + esc(id) + '">' +
      '<input type="hidden" name="draft_id" value="' + esc(d.draft_id || '') + '">' +
      '<input type="hidden" name="_rev" value="' + esc(d._rev || '') + '">' +
      '<input type="hidden" name="reviewer" value="" class="swipe-edit-reviewer">' +
      '<label>Payload JSON</label>' +
      '<textarea name="payload" rows="10" spellcheck="false" required>' + esc(prettyPayload(d.payload || {})) + '</textarea>' +
      '<p><button type="submit" class="swipe-btn edit">Save payload</button></p>' +
    '</form>';
  }

  function renderSingleDraft(c, draft) {
    var editId = editDomId(draft.draft_id || c.draft_id || 'draft', 0);
    var error = draft.validation_error || c.proposed_error || '';
    var warn = draft.approvable ? '' :
      '<p class="swipe-warn">⚠ ' + esc(error || 'Proposed job is invalid') + '</p>';
    return warn +
      '<section class="swipe-proposed-job">' +
        '<p class="muted">Proposed change</p>' +
        '<p><strong>' + esc(draft.job_type || c.proposed_job_type || '(unknown job)') + '</strong></p>' +
        payloadRows(draft.payload || {}) +
      '</section>' +
      draftEditForm(draft, editId);
  }

  function renderChecklist(c, drafts) {
    var items = drafts.map(function (d, i) {
      var editId = editDomId(d.draft_id, i);
      var disabled = d.approvable ? '' : ' disabled';
      var checked = d.approvable ? ' checked' : '';
      var error = d.validation_error
        ? '<p class="swipe-warn">⚠ ' + esc(d.validation_error) + '</p>'
        : '';
      return '<div class="swipe-checklist-item">' +
        '<input type="hidden" name="draft_id" value="' + esc(d.draft_id || '') + '">' +
        '<input type="hidden" name="_rev" value="' + esc(d._rev || '') + '">' +
        '<label>' +
          '<input type="checkbox" name="checked" value="' + esc(d.draft_id || '') + '"' + checked + disabled + '> ' +
          '<strong>' + esc(d.job_type || '(unknown job)') + '</strong> ' +
          '<span class="muted">' + esc(shortPayloadSummary(d.payload || {})) + '</span>' +
        '</label>' +
        error +
        '<p><button type="button" class="swipe-btn edit" data-edit="' + esc(editId) + '">Edit</button></p>' +
        draftEditForm(d, editId) +
      '</div>';
    }).join('');
    return '<form class="swipe-checklist" data-approve-set="1">' +
      '<p class="muted">' + drafts.length + ' proposed changes. Checked drafts will execute; unchecked drafts will be denied.</p>' +
      items +
    '</form>';
  }

  function bindEditForms(cardEl) {
    cardEl.querySelectorAll('button[data-edit]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var form = document.getElementById(btn.getAttribute('data-edit'));
        if (form) form.hidden = !form.hidden;
      });
    });
    cardEl.querySelectorAll('form.swipe-edit-form').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        var reviewer = reviewerName();
        if (!reviewer) {
          e.preventDefault();
          var ri = document.getElementById('swipe-reviewer-input');
          if (ri) ri.focus();
          window.alert('Enter your name in the "Reviewer" field above first — it is recorded on every edit.');
          return;
        }
        var input = form.querySelector('input.swipe-edit-reviewer');
        if (input) input.value = reviewer;
      });
    });
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
    var drafts = (c.drafts && c.drafts.length) ? c.drafts : [{
      draft_id: c.draft_id,
      job_type: c.proposed_job_type,
      payload: c.proposed_payload || {},
      validation_error: c.proposed_error || '',
      _rev: c._rev || '',
      approvable: c.approvable
    }];
    var isMulti = drafts.length > 1;
    var firstDraft = drafts[0] || {};
    var approveDisabled = (!isMulti && !firstDraft.approvable) ? ' disabled' : '';
    var approveHint = isMulti ? 'Approve checked jobs and deny unchecked jobs'
      : (firstDraft.approvable ? 'Approve and stage this job' : (firstDraft.validation_error || 'Cannot approve: the proposed job is invalid'));

    var thumbs = (c.photos && c.photos.length)
      ? '<div class="swipe-thumbs">' + c.photos.map(function (u) {
          return '<img class="candidate-thumb" src="' + esc(u) + '" alt="capture photo" loading="lazy">';
        }).join('') + '</div>'
      : '';
    var proposed = isMulti ? renderChecklist(c, drafts) : renderSingleDraft(c, firstDraft);

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
        proposed +
        '<div class="swipe-actions">' +
          '<button type="button" class="swipe-btn reject" data-act="reject" title="Reject (R / Left)">Reject</button>' +
          '<button type="button" class="swipe-btn skip" data-act="skip" title="Skip without acting — leaves it pending (S / Down)">Skip</button>' +
          (isMulti ? '' : '<button type="button" class="swipe-btn edit" data-edit="' + esc(editDomId(firstDraft.draft_id || c.draft_id || 'draft', 0)) + '">Edit</button>') +
          '<button type="button" class="swipe-btn defer" disabled title="Defer is not a draft review action">Defer</button>' +
          '<button type="button" class="swipe-btn approve" data-act="approve"' + approveDisabled +
            ' title="' + esc(approveHint) + '">Approve</button>' +
        '</div>' +
      '</article>';

    var card = mount.querySelector('.swipe-card');
    bindEditForms(card);
    card.querySelectorAll('button[data-act]').forEach(function (btn) {
      btn.addEventListener('click', function () { decide(c, btn.getAttribute('data-act'), btn); });
    });
  }

  function decide(card, action, btn) {
    if (btn && btn.disabled) return;
    // Skip just advances to the next card without writing anything; the
    // Skip just advances client-side; the draft stays pending_approval and reappears on reload.
    if (action === 'skip') { index += 1; render(); return; }
    var drafts = (card.drafts && card.drafts.length) ? card.drafts : [{
      draft_id: card.draft_id,
      _rev: card._rev || '',
      approvable: card.approvable
    }];
    var isMulti = drafts.length > 1;
    if (action === 'approve' && !isMulti && !drafts[0].approvable) return;
    var reviewer = reviewerName();
    if (!reviewer) {
      var ri = document.getElementById('swipe-reviewer-input');
      if (ri) { ri.focus(); }
      window.alert('Enter your name in the "Reviewer" field above first — it is recorded on every approval.');
      return;
    }
    var body = new URLSearchParams();
    body.set('reviewer', reviewer);
    body.set('rationale', action === 'unknown' ? 'mark unknown' : '');
    var route = action === 'approve' ? '/field-capture/review/approve' : '/field-capture/review/reject';
    if (isMulti) {
      route = '/field-capture/review/approve-set';
      drafts.forEach(function (d) {
        body.append('draft_id', d.draft_id || '');
        body.append('_rev', d._rev || '');
      });
      if (action === 'approve') {
        mount.querySelectorAll('input[name="checked"]:checked').forEach(function (input) {
          body.append('checked', input.value);
        });
      }
    } else {
      body.set('draft_id', card.draft_id);
      body.set('_rev', card._rev || '');
    }
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
