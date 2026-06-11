/*
 * Mobile approval inbox for the unified capture PWA.
 *
 * This is the phone-side surface of the BTQ review loop: a voice memo or photo
 * becomes one or more job drafts, the mail glyph badges, and the operator
 * approves or rejects the draft or draft set. It is the mobile PWA review
 * surface for the job-first queue.
 *
 * Contract:
 *   GET  /api/session         -> { ..., can_review, inbox_count: N }
 *                                  (drives visibility and the badge)
 *   GET  /api/inbox           -> { count, items: [ { draft_id, _rev,
 *                                  source_capture_id, source, message,
 *                                  evidence, site, group_id, job_type,
 *                                  payload, created_at } ] }
 *   POST /api/inbox/approve   <- { draft_id, _rev }
 *   POST /api/inbox/reject    <- { draft_id, _rev, reason? }
 *   POST /api/inbox/approve-set <- { drafts: [{ draft_id, _rev, checked }] }
 *
 * Two contract obligations on the UI:
 *   1. Carry _rev from the item into the approve/reject POST (optimistic
 *      concurrency). A stale _rev means someone else already decided.
 *   2. Handle `already_decided`: another surface (the desktop /swipe, or this
 *      phone in another tab) already resolved the draft. That is expected,
 *      not an error -- show "already handled" and drop the card.
 *
 * There is intentionally NO direct mutation path here. approve/reject and
 * approve-set POST to the shared draft review endpoint; the UI never mutates
 * draft state itself.
 *
 * Phase 3 swap: set INBOX_USE_MOCK = false. INBOX_API_BASE stays "" for
 * same-origin (unified_capture serves both the PWA and the API). Nothing else
 * changes -- the mock mirrors the live response shapes exactly.
 */
(function () {
  "use strict";

  // ---- Config seam --------------------------------------------------------
  // One line flips mock -> live once Phase 3 endpoints land.
  var INBOX_USE_MOCK = false;
  // Empty = same origin. Set to e.g. "https://fc.gregstoltz.com" only if the
  // inbox API is ever served from a different host than the PWA.
  var INBOX_API_BASE = "";

  var BADGE_POLL_MS = 60000; // cheap badge refresh; full list only on open.

  // ---- DOM ----------------------------------------------------------------
  var els = {
    btn: document.querySelector("#inboxBtn"),
    badge: document.querySelector("#inboxBadge"),
    badgeCount: document.querySelector("#inboxBadgeCount"),
    section: document.querySelector("#inboxSection"),
    back: document.querySelector("#inboxBack"),
    mount: document.querySelector("#inboxCardMount"),
    empty: document.querySelector("#inboxEmpty"),
    progress: document.querySelector("#inboxProgress"),
    message: document.querySelector("#inboxMessage"),
  };
  if (!els.btn || !els.section || !els.mount) return; // markup not present

  var state = { groups: [], index: 0, loading: false, canReview: false };
  els.btn.hidden = true;

  // ---- Token (read exactly as app.js does) --------------------------------
  function currentToken() {
    var t = "";
    try {
      var qs = new URLSearchParams(window.location.search);
      t = qs.get("token") || "";
      if (!t) t = window.localStorage.getItem("unifiedCaptureToken") || "";
      if (!t) {
        var row = document.cookie.split("; ").find(function (r) {
          return r.indexOf("unifiedCaptureToken=") === 0;
        });
        if (row) t = decodeURIComponent(row.slice("unifiedCaptureToken=".length));
      }
    } catch (_e) {}
    return t;
  }

  function authHeaders(extra) {
    var h = { Accept: "application/json", Authorization: "Bearer " + currentToken() };
    if (extra) Object.keys(extra).forEach(function (k) { h[k] = extra[k]; });
    return h;
  }

  // ---- API (mock-aware) ---------------------------------------------------
  function apiUrl(path) { return INBOX_API_BASE + path; }

  function getInbox() {
    if (INBOX_USE_MOCK) return Promise.resolve(MOCK.inbox());
    return fetch(apiUrl("/api/inbox"), { headers: authHeaders(), cache: "no-store" })
      .then(handleJson);
  }

  function getReviewSession() {
    // The badge rides on /api/session.inbox_count so the poll is one cheap
    // call shared with session refresh, not a full inbox fetch.
    if (INBOX_USE_MOCK) return Promise.resolve({ can_review: true, inbox_count: MOCK.count() });
    return fetch(apiUrl("/api/session"), { headers: authHeaders(), cache: "no-store" })
      .then(handleJson);
  }

  function postDecision(kind, item, reason) {
    var body = { draft_id: item.draft_id, _rev: item._rev };
    if (kind === "reject" && reason) body.reason = reason;
    if (INBOX_USE_MOCK) return Promise.resolve(MOCK.decide(kind, item));
    return fetch(apiUrl("/api/inbox/" + kind), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }).then(handleJson);
  }

  function postDecisionSet(group, cardEl) {
    var entries = Array.prototype.slice.call(cardEl.querySelectorAll("input[data-draft-id]"));
    var drafts = entries.map(function (input) {
      return {
        draft_id: input.getAttribute("data-draft-id"),
        _rev: input.getAttribute("data-rev"),
        checked: input.checked,
      };
    });
    if (INBOX_USE_MOCK) return Promise.resolve(MOCK.decideSet(group, drafts));
    return fetch(apiUrl("/api/inbox/approve-set"), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ drafts: drafts }),
    }).then(handleJson);
  }

  function handleJson(resp) {
    if (resp.status === 401 || resp.status === 403) {
      var authErr = new Error("auth_" + resp.status);
      authErr.status = resp.status;
      throw authErr;
    }
    // Older endpoints used 409 for optimistic concurrency; current draft
    // review returns HTTP 200 with {error:"already_decided"}.
    if (resp.status === 409) return resp.json().then(function (b) {
      return Object.assign({ status: "already_decided" }, b || {});
    });
    if (!resp.ok) {
      var err = new Error("http_" + resp.status);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  // ---- Badge --------------------------------------------------------------
  function setBadge(count) {
    var n = Number(count) || 0;
    if (!els.badge) return;
    els.badge.hidden = n <= 0;
    if (els.badgeCount) els.badgeCount.textContent = n > 99 ? "99+" : String(n);
  }

  function refreshBadge() {
    if (!currentToken()) return Promise.resolve();
    return getReviewSession().then(function (s) {
      state.canReview = Boolean(s && (s.can_review || !Object.prototype.hasOwnProperty.call(s, "can_review")));
      els.btn.hidden = !state.canReview;
      if (!state.canReview) {
        setBadge(0);
        return;
      }
      setBadge(Number(s && s.inbox_count) || 0);
    }).catch(function () {
      state.canReview = false;
      els.btn.hidden = true;
      setBadge(0);
    });
  }

  // ---- Rendering ----------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  var SOURCE_GLYPH = { voice: "🎙", photo: "📷", note: "✎" };

  function payloadRows(payload) {
    var keys = Object.keys(payload || {});
    if (!keys.length) return "";
    var rows = keys.map(function (k) {
      var v = payload[k];
      if (v && typeof v === "object") v = JSON.stringify(v);
      return "<tr><th>" + esc(k) + "</th><td>" + esc(v) + "</td></tr>";
    }).join("");
    return '<table class="inbox-payload">' + rows + "</table>";
  }

  function groupInboxItems(items) {
    var groups = [];
    var byId = {};
    (items || []).forEach(function (item, idx) {
      var key = item.group_id || item.draft_id || ("draft-" + idx);
      if (!byId[key]) {
        byId[key] = { group_id: key, items: [] };
        groups.push(byId[key]);
      }
      byId[key].items.push(item);
    });
    return groups;
  }

  function itemTitle(item) {
    return item.job_type || "Job draft";
  }

  function itemSummary(item) {
    var text = item.message || "";
    if (text) return text;
    var payload = item.payload || {};
    for (var i = 0, keys = ["title", "summary", "name", "path"]; i < keys.length; i += 1) {
      var value = payload[keys[i]];
      if (value) return String(value);
    }
    return "";
  }

  function renderDraftDetail(item) {
    var summary = itemSummary(item);
    return (
      (summary ? '<p class="inbox-summary">' + esc(summary) + "</p>" : "") +
      (item.evidence ? '<blockquote class="inbox-evidence">' + esc(item.evidence) + "</blockquote>" : "") +
      '<details class="inbox-detail"><summary>Job payload</summary>' + payloadRows(item.payload) + "</details>"
    );
  }

  function renderCard() {
    if (state.index >= state.groups.length) {
      els.mount.innerHTML = "";
      if (els.empty) els.empty.hidden = false;
      if (els.progress) els.progress.textContent = "";
      return;
    }
    if (els.empty) els.empty.hidden = true;
    var group = state.groups[state.index];
    var items = group.items || [];
    var item = items[0] || {};
    var remaining = state.groups.length - state.index;
    var glyph = SOURCE_GLYPH[item.source] || "•";
    if (els.progress) els.progress.textContent = remaining + " to review";

    if (items.length > 1) {
      els.mount.innerHTML =
        '<article class="inbox-card" data-group-id="' + esc(group.group_id) + '">' +
          '<div class="inbox-card-top">' +
            '<span class="inbox-source" title="' + esc(item.source || "") + '">' + glyph + " " + esc(item.source || "") + "</span>" +
            '<span class="inbox-site">' + esc(item.site || "Unknown site") + "</span>" +
          "</div>" +
          '<h3 class="inbox-title">Review job set</h3>' +
          (item.submitter_name ? '<p class="inbox-jobtype">' + esc(item.submitter_name) + "</p>" : "") +
          '<div class="inbox-checklist">' +
            items.map(function (draft) {
              return (
                '<label class="inbox-checkitem">' +
                  '<input type="checkbox" checked data-draft-id="' + esc(draft.draft_id) + '" data-rev="' + esc(draft._rev) + '">' +
                  '<span><strong>' + esc(itemTitle(draft)) + "</strong>" +
                  (itemSummary(draft) ? '<small>' + esc(itemSummary(draft)) + "</small>" : "") +
                  "</span>" +
                "</label>"
              );
            }).join("") +
          "</div>" +
          '<details class="inbox-detail"><summary>Payloads</summary>' +
            items.map(function (draft) {
              return '<h4>' + esc(itemTitle(draft)) + "</h4>" + payloadRows(draft.payload);
            }).join("") +
          "</details>" +
          '<div class="inbox-actions">' +
            '<button type="button" class="inbox-btn skip" data-act="skip">Skip</button>' +
            '<button type="button" class="inbox-btn approve" data-act="approve-set">Approve set</button>' +
          "</div>" +
        "</article>";

      var groupCard = els.mount.querySelector(".inbox-card");
      groupCard.querySelectorAll("button[data-act]").forEach(function (b) {
        b.addEventListener("click", function () { decideGroup(group, b.getAttribute("data-act"), groupCard); });
      });
      return;
    }

    els.mount.innerHTML =
      '<article class="inbox-card" data-draft-id="' + esc(item.draft_id) + '">' +
        '<div class="inbox-card-top">' +
          '<span class="inbox-source" title="' + esc(item.source || "") + '">' + glyph + " " + esc(item.source || "") + "</span>" +
          '<span class="inbox-site">' + esc(item.site || "Unknown site") + "</span>" +
        "</div>" +
        '<h3 class="inbox-title">' + esc(itemTitle(item)) + "</h3>" +
        (item.submitter_name ? '<p class="inbox-jobtype">' + esc(item.submitter_name) + "</p>" : "") +
        renderDraftDetail(item) +
        '<p class="inbox-meta">' + esc(item.created_at || "") + "</p>" +
        '<div class="inbox-actions">' +
          '<button type="button" class="inbox-btn reject" data-act="reject">Reject</button>' +
          '<button type="button" class="inbox-btn skip" data-act="skip">Skip</button>' +
          '<button type="button" class="inbox-btn approve" data-act="approve">Approve</button>' +
        "</div>" +
      "</article>";

    var card = els.mount.querySelector(".inbox-card");
    card.querySelectorAll("button[data-act]").forEach(function (b) {
      b.addEventListener("click", function () { decide(item, b.getAttribute("data-act"), card); });
    });
  }

  function flash(text, tone) {
    if (!els.message) return;
    els.message.textContent = text;
    els.message.dataset.tone = tone || "";
    els.message.hidden = !text;
  }

  function advance() {
    state.index += 1;
    renderCard();
    refreshBadge();
  }

  // ---- Decide -------------------------------------------------------------
  function decide(item, kind, cardEl) {
    if (state.loading) return;
    // Skip just moves to the next card without writing anything; the item stays
    // pending and comes back next time the inbox is opened.
    if (kind === "skip") { advance(); return; }
    state.loading = true;
    if (cardEl) cardEl.classList.add(kind === "approve" ? "swiping-right" : "swiping-left");
    var reason = kind === "reject" ? "" : undefined; // reason optional; UI keeps it simple

    postDecision(kind, item, reason).then(function (result) {
      state.loading = false;
      if (result && (result.status === "already_decided" || result.error === "already_decided")) {
        // Expected concurrency outcome -- another surface handled it. Don't
        // error; tell the operator and drop the card.
        flash("Already handled on another device.", "info");
        advance();
        return;
      }
      flash(kind === "approve" ? "Approved." : "Rejected.", "info");
      advance();
    }).catch(function (err) {
      state.loading = false;
      if (cardEl) cardEl.classList.remove("swiping-right", "swiping-left");
      if (err.status === 401 || err.status === 403) {
        flash("Your token is invalid or expired. Reopen capture and re-paste it.", "error");
      } else {
        // On a transient failure, re-fetch so the operator sees current truth
        // (the draft may or may not have been decided).
        flash("Action failed. Refreshing...", "error");
        open();
      }
    });
  }

  function decideGroup(group, kind, cardEl) {
    if (state.loading) return;
    if (kind === "skip") { advance(); return; }
    state.loading = true;
    if (cardEl) cardEl.classList.add("swiping-right");

    postDecisionSet(group, cardEl).then(function (result) {
      state.loading = false;
      var conflicts = Number(result && result.already_decided) || 0;
      if (conflicts) {
        flash("Set handled; " + conflicts + " already decided elsewhere.", "info");
      } else if (result && result.results && result.results.some(function (r) { return r.error; })) {
        flash("Set partly failed. Refreshing...", "error");
      } else {
        flash("Set reviewed.", "info");
      }
      advance();
    }).catch(function (err) {
      state.loading = false;
      if (cardEl) cardEl.classList.remove("swiping-right");
      if (err.status === 401 || err.status === 403) {
        flash("Your token is invalid or expired. Reopen capture and re-paste it.", "error");
      } else {
        flash("Action failed. Refreshing...", "error");
        open();
      }
    });
  }

  // ---- Open / close panel -------------------------------------------------
  function open() {
    if (!state.canReview) return;
    if (!currentToken()) { flash("Paste your access token first.", "error"); return; }
    // Mirror app.js: hide the capture/success takeover panels when a feed opens.
    document.querySelectorAll(".quick-fields, .photo-input-panel, .voice-panel, .note-panel, #successScreen, #feedSection")
      .forEach(function (p) { if (p) p.hidden = true; });
    els.section.hidden = false;
    flash("", "");
    if (els.empty) els.empty.hidden = true;
    els.mount.innerHTML = '<p class="inbox-loading">Loading inbox...</p>';
    state.loading = true;
    getInbox().then(function (data) {
      state.loading = false;
      state.groups = groupInboxItems((data && data.items) || []);
      state.index = 0;
      setBadge(data && typeof data.count === "number" ? data.count : state.groups.length);
      renderCard();
    }).catch(function (err) {
      state.loading = false;
      if (err.status === 401 || err.status === 403) {
        els.mount.innerHTML = '<p class="inbox-loading">Token invalid or expired. Reopen capture and re-paste it.</p>';
      } else {
        els.mount.innerHTML = '<p class="inbox-loading">Could not load the inbox. Try again in a moment.</p>';
      }
    });
  }

  function close() {
    els.section.hidden = true;
    // Hand control back to app.js's reset if available; otherwise just reload
    // the capture form by showing it.
    if (typeof window.btqResetToForm === "function") {
      window.btqResetToForm();
    } else {
      document.querySelectorAll(".quick-fields, .photo-input-panel, .voice-panel, .note-panel")
        .forEach(function (p) { if (p) p.hidden = false; });
    }
    refreshBadge();
  }

  // ---- Wiring -------------------------------------------------------------
  els.btn.addEventListener("click", open);
  if (els.back) els.back.addEventListener("click", close);

  document.addEventListener("keydown", function (e) {
    if (els.section.hidden) return;
    if (state.index >= state.groups.length) return;
    var tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    var group = state.groups[state.index];
    var item = group && group.items && group.items[0];
    var card = els.mount.querySelector(".inbox-card");
    if (!group || !item) return;
    if ((e.key === "a" || e.key === "A" || e.key === "ArrowRight") && group.items.length > 1) decideGroup(group, "approve-set", card);
    else if (e.key === "a" || e.key === "A" || e.key === "ArrowRight") decide(item, "approve", card);
    else if ((e.key === "r" || e.key === "R" || e.key === "ArrowLeft") && group.items.length === 1) decide(item, "reject", card);
    else if (e.key === "s" || e.key === "S" || e.key === "ArrowDown") decide(item, "skip", card);
  });

  // Expose a hook app.js can call after a successful capture submit, so the
  // badge updates without waiting for the next poll.
  window.btqInboxRefresh = refreshBadge;

  // ---- Mock ---------------------------------------------------------------
  // Mirrors the live response shapes 1:1 so the swap is a flag flip. Decisions
  // mutate the in-memory set, and one item is pre-set to return already_decided
  // so the conflict path is exercisable without a second device.
  var MOCK = (function () {
    var items = [
      {
        draft_id: "jd_mock_1", _rev: "1-aaa", source_capture_id: "cap-1", source: "voice",
        site: "7050 — Summit Wire", site_id: "7050", group_id: "grp-1", submitter_name: "Preview Operator", created_at: "Today 8:42 PM",
        message: "Staffing risk: guard removed self from required group and no-showed.",
        evidence: "Jordan removed themselves from the required group and no-showed again.",
        job_type: "log_personnel_event",
        payload: { employee: "Jordan", event_type: "attendance", summary: "Removed self from required group; no-show.", reported_by: "operator" },
      },
      {
        draft_id: "jd_mock_2", _rev: "1-bbb", source_capture_id: "cap-2", source: "photo",
        site: "7050 — Summit Wire", site_id: "7050", group_id: "grp-2", submitter_name: "Preview Operator", created_at: "Today 8:50 PM",
        message: "Supply need: paper towels out in the east restroom.",
        evidence: "Photo shows empty dispenser.",
        job_type: "log_supply_need",
        payload: { site_id: "7050", item_name: "Paper towels", requested_by: "operator" },
      },
      {
        draft_id: "jd_mock_3", _rev: "1-ccc", source_capture_id: "cap-3", source: "note",
        site: "7060 — Continental Metalworks", site_id: "7060", group_id: "grp-3", submitter_name: "Preview Operator", created_at: "Yesterday",
        message: "Note appended to site record.",
        evidence: "Front entrance mats need replacement.",
        job_type: "append_to_note",
        payload: { path: "Accounts/7060.md", content: "Front entrance mats need replacement.", destination: "site_note" },
      },
      {
        draft_id: "jd_mock_4", _rev: "1-ddd", source_capture_id: "cap-4", source: "voice",
        site: "7050 — Summit Wire", site_id: "7050", group_id: "grp-4", submitter_name: "Preview Operator", created_at: "Today 9:05 PM",
        message: "Checklist item: log site note.",
        evidence: "Three follow-up actions from one memo.",
        job_type: "append_to_note",
        payload: { path: "Accounts/7050.md", content: "East gate needs follow-up.", destination: "site_note" },
      },
      {
        draft_id: "jd_mock_5", _rev: "1-eee", source_capture_id: "cap-4", source: "voice",
        site: "7050 — Summit Wire", site_id: "7050", group_id: "grp-4", submitter_name: "Preview Operator", created_at: "Today 9:05 PM",
        message: "Checklist item: log supply need.",
        evidence: "Three follow-up actions from one memo.",
        job_type: "log_supply_need",
        payload: { site_id: "7050", item_name: "Traffic cones", requested_by: "operator" },
      },
    ];
    var decided = {};
    var CONFLICT_ID = "jd_mock_3"; // approving/rejecting this returns already_decided
    return {
      count: function () { return items.filter(function (i) { return !decided[i.draft_id]; }).length; },
      inbox: function () {
        var live = items.filter(function (i) { return !decided[i.draft_id]; });
        return { count: live.length, items: live.map(function (i) { return JSON.parse(JSON.stringify(i)); }) };
      },
      decide: function (kind, item) {
        if (item.draft_id === CONFLICT_ID) {
          decided[item.draft_id] = true; // it's gone now
          return { error: "already_decided", draft_id: item.draft_id };
        }
        decided[item.draft_id] = true;
        return { status: kind === "approve" ? "approved" : "rejected", draft_id: item.draft_id };
      },
      decideSet: function (_group, drafts) {
        var approved = 0;
        var rejected = 0;
        var results = drafts.map(function (draft) {
          decided[draft.draft_id] = true;
          if (draft.checked) approved += 1;
          else rejected += 1;
          return { draft_id: draft.draft_id, status: draft.checked ? "approved" : "rejected" };
        });
        return { ok: true, approved: approved, rejected: rejected, already_decided: 0, results: results };
      },
    };
  })();

  // ---- Lifecycle (runs last, after MOCK is defined) -----------------------
  // Badge: refresh on load, on a slow poll, and when the tab returns to the
  // foreground (you approve, pocket the phone, drive, come back).
  refreshBadge();
  setInterval(refreshBadge, BADGE_POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshBadge();
  });
})();
