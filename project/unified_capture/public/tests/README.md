# Inbox PWA flow tests

Headless jsdom tests for the approval inbox UI (`../inbox.js`).

    npm install jsdom        # once
    node tests/inbox_mock_flow.mjs /path/to/jsdom/lib/api.js   # mock-mode: route selects, approve/reject, already_decided, batch
    node tests/inbox_live_flow.mjs /path/to/jsdom/lib/api.js   # live-mode: route POST bodies + _rev + 409 mapping

`inbox_mock_flow` runs against the in-file MOCK (INBOX_USE_MOCK = true).
`inbox_live_flow` flips the flag and stubs `window.fetch` to assert the Phase-3
contract: Bearer auth, JSON POST bodies carrying `draft_id` + `_rev`, approve-only
routes, checked-row-only batch routes, and the 409 optimistic-concurrency mapping
to a non-error "already handled" outcome.
