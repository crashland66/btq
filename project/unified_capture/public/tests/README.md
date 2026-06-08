# Inbox PWA flow tests

Headless jsdom tests for the approval inbox UI (`../inbox.js`).

    npm install jsdom        # once
    node tests/inbox_mock_flow.mjs   # mock-mode: badge -> open -> approve/reject -> already_decided -> empty
    node tests/inbox_live_flow.mjs   # live-mode: stubbed fetch; asserts _rev is carried + 409 -> already_decided

`inbox_mock_flow` runs against the in-file MOCK (INBOX_USE_MOCK = true).
`inbox_live_flow` flips the flag and stubs `window.fetch` to assert the Phase-3
contract: Bearer auth, JSON POST bodies carrying `candidate_id` + `_rev`, and the
409 optimistic-concurrency mapping to a non-error "already handled" outcome.
