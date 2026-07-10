# Inbox PWA flow tests

Headless jsdom tests for the approval inbox UI (`../inbox.js`).

From the repository root, install the pinned test-only dependency from the
lockfile and run all three suites in deterministic order:

```bash
npm --prefix project/unified_capture/public/tests ci
npm --prefix project/unified_capture/public/tests test
```

The repository-wide `./scripts/btq-test-all` command runs the same package test
after the Python and supported-host Swift suites. It never installs dependencies;
run the locked `npm ci` command above first on a clean checkout.

`inbox_mock_flow` runs against the in-file MOCK (INBOX_USE_MOCK = true).
`inbox_live_flow` flips the flag and stubs `window.fetch` to assert the Phase-3
contract: Bearer auth, JSON POST bodies carrying `draft_id` + `_rev`, approve-only
routes, checked-row-only batch routes, and the 409 optimistic-concurrency mapping
to a non-error "already handled" outcome.

`inbox_can_review_gate` verifies that non-review tokens cannot open or fetch the
inbox while review tokens see the expected button and badge.
