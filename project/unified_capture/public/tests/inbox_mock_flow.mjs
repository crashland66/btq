import { JSDOM } from '/tmp/node_modules/jsdom/lib/api.js';
import fs from 'fs';

const html = fs.readFileSync('../index.html', 'utf8');
const inboxJs = fs.readFileSync('../inbox.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://fc.example.com/?token=demo' });
const { window } = dom;
global.window = window; global.document = window.document;
// minimal stubs
window.fetch = () => Promise.reject(new Error('fetch should not run in mock mode'));
window.setInterval = () => 0; // don't actually poll during test

function assert(cond, msg){ if(!cond){ console.error('FAIL:', msg); process.exitCode = 1; } else { console.log('ok -', msg); } }

// Execute inbox.js in the window context
const scriptEl = window.document.createElement('script');
scriptEl.textContent = inboxJs;
window.eval(inboxJs);

const $ = (s) => window.document.querySelector(s);

// allow the load-time refreshBadge() promise to resolve
await new Promise(r => setTimeout(r, 20));

// 1. Badge reflects 3 pending from the mock
assert(!$('#inboxBadge').hidden, 'badge visible on load');
assert($('#inboxBadgeCount').textContent === '3', 'badge count = 3 (got ' + $('#inboxBadgeCount').textContent + ')');

// 2. Open the inbox
$('#inboxBtn').click();
await new Promise(r => setTimeout(r, 20));
assert(!$('#inboxSection').hidden, 'inbox panel opens');
let card = $('.inbox-card');
assert(card && card.dataset.candidateId === 'ac_mock_1', 'first card is ac_mock_1');
assert($('#inboxProgress').textContent.includes('3'), 'progress shows 3 to review');
assert(/log_personnel_event/.test($('.inbox-jobtype').textContent), 'shows job_type');

// 3. Approve card 1 -> advance to card 2
$('.inbox-card .approve').click();
await new Promise(r => setTimeout(r, 20));
card = $('.inbox-card');
assert(card && card.dataset.candidateId === 'ac_mock_2', 'after approve, card 2 shows');
assert(/Approved/.test($('#inboxMessage').textContent), 'approve flashed confirmation');
assert($('#inboxBadgeCount').textContent === '2', 'badge now 2 after approve');

// 4. Reject card 2 -> advance to card 3 (the conflict one)
$('.inbox-card .reject').click();
await new Promise(r => setTimeout(r, 20));
card = $('.inbox-card');
assert(card && card.dataset.candidateId === 'ac_mock_3', 'after reject, card 3 shows');
assert(/Rejected/.test($('#inboxMessage').textContent), 'reject flashed confirmation');

// 5. Approve card 3 -> mock returns already_decided -> "already handled" + advance to empty
$('.inbox-card .approve').click();
await new Promise(r => setTimeout(r, 20));
assert(/[Aa]lready handled/.test($('#inboxMessage').textContent), 'already_decided shows already-handled, not an error');
assert($('#inboxMessage').dataset.tone !== 'error', 'already_decided is not treated as error');
assert(!$('#inboxEmpty').hidden, 'empty state shows after last card');
assert($('#inboxBadge').hidden, 'badge hidden when inbox empty');

// 6. Keyboard: reopen and approve via "a"
console.log('All inbox flow assertions evaluated.');
