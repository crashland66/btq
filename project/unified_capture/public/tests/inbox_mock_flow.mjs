import fs from 'fs';

const jsdomPath = process.argv[2] || '/tmp/node_modules/jsdom/lib/api.js';
const { JSDOM } = await import(jsdomPath);

const html = fs.readFileSync('../index.html', 'utf8');
let inboxJs = fs.readFileSync('../inbox.js', 'utf8');
inboxJs = inboxJs.replace('var INBOX_USE_MOCK = false;', 'var INBOX_USE_MOCK = true;');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://fc.example.com/?token=demo' });
const { window } = dom;
global.window = window; global.document = window.document;
window.fetch = () => Promise.reject(new Error('fetch should not run in mock mode'));
window.setInterval = () => 0;

function assert(cond, msg){ if(!cond){ console.error('FAIL:', msg); process.exitCode = 1; } else { console.log('ok -', msg); } }

window.eval(inboxJs);

const $ = (s) => window.document.querySelector(s);
const $$ = (s) => Array.from(window.document.querySelectorAll(s));

await new Promise(r => setTimeout(r, 20));

assert(!$('#inboxBadge').hidden, 'badge visible on load');
assert($('#inboxBadgeCount').textContent === '5', 'badge count = 5 (got ' + $('#inboxBadgeCount').textContent + ')');

$('#inboxBtn').click();
await new Promise(r => setTimeout(r, 20));
assert(!$('#inboxSection').hidden, 'inbox panel opens');
let card = $('.inbox-card');
assert(card && card.dataset.draftId === 'jd_mock_1', 'first card is jd_mock_1');
assert($('#inboxProgress').textContent.includes('4'), 'progress shows 4 groups to review');
assert(/log_personnel_event/.test($('.inbox-title').textContent), 'shows job_type');
assert($('.inbox-route select'), 'single card route select renders');
assert($('.inbox-route select').value === 'open_issue', 'single card default route falls back to open_issue');

$('.inbox-route select').value = 'supply_need';
$('.inbox-card .approve').click();
await new Promise(r => setTimeout(r, 20));
card = $('.inbox-card');
assert(card && card.dataset.draftId === 'jd_mock_2', 'after approve, card 2 shows');
assert(/Approved -> Supply need\./.test($('#inboxMessage').textContent), 'approve flashed chosen bucket');
assert($('#inboxBadgeCount').textContent === '4', 'badge now 4 after approve');

$('.inbox-card .reject').click();
await new Promise(r => setTimeout(r, 20));
card = $('.inbox-card');
assert(card && card.dataset.draftId === 'jd_mock_3', 'after reject, card 3 shows');
assert(/Rejected/.test($('#inboxMessage').textContent), 'reject flashed confirmation');

$('.inbox-card .approve').click();
await new Promise(r => setTimeout(r, 20));
assert(/[Aa]lready handled/.test($('#inboxMessage').textContent), 'already_decided shows already-handled, not an error');
assert($('#inboxMessage').dataset.tone !== 'error', 'already_decided is not treated as error');

card = $('.inbox-card');
assert(card && card.dataset.groupId === 'grp-4', 'after conflict, batch group shows');
assert($$('.inbox-checkitem').length === 2, 'batch renders two checklist rows');
assert($$('.inbox-checkitem select').length === 2, 'batch renders per-row route selects');
assert($$('.inbox-checkitem select')[0].value === 'open_issue', 'batch first row default route open_issue');
assert($$('.inbox-checkitem select')[1].value === 'supply_need', 'batch second row default route supply_need');

const firstCheck = $$('.inbox-checkitem input[type="checkbox"]')[0];
firstCheck.click();
await new Promise(r => setTimeout(r, 20));
assert($$('.inbox-checkitem select')[0].disabled, 'unchecked row keeps route select visible but disabled');
assert(!$$('.inbox-checkitem select')[1].disabled, 'checked row route select stays enabled');

$('.inbox-card .approve').click();
await new Promise(r => setTimeout(r, 20));
assert(!$('#inboxEmpty').hidden, 'empty state shows after batch review');
assert($('#inboxBadge').hidden, 'badge hidden when inbox empty');

console.log('All inbox mock flow assertions evaluated.');
