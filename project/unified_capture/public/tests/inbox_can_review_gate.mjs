// prompt-310 hide-logic test for inbox.js: the inbox button is hidden unless
// /api/session returns can_review:true; non-review tokens never fetch /api/inbox.
// Driven by pytest (test_unified_capture_prompt310.py) when node+jsdom exist.
//
// argv[2] = absolute path to jsdom api.js
// argv[3] = absolute path to the public/ dir (containing index.html + inbox.js)
// Prints "ALL_OK" on success; exits non-zero with FAIL lines otherwise.

import fs from 'fs';

const jsdomPath = process.argv[2];
const publicDir = process.argv[3];
const { JSDOM } = await import(jsdomPath);

const html = fs.readFileSync(publicDir + '/index.html', 'utf8');
const inboxJs = fs.readFileSync(publicDir + '/inbox.js', 'utf8');

let failures = 0;
function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); failures++; }
  else { console.log('ok -', msg); }
}

async function run(sessionBody, label) {
  const dom = new JSDOM(html, {
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    url: 'https://fc.example.com/?token=demo',
  });
  const { window } = dom;
  global.window = window;
  global.document = window.document;

  const fetched = [];
  window.fetch = (url, opts) => {
    fetched.push(String(url));
    if (String(url).indexOf('/api/session') !== -1) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(sessionBody),
      });
    }
    // Any /api/inbox fetch here would be a contract violation for non-review tokens.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ count: 0, items: [] }),
    });
  };
  window.setInterval = () => 0; // don't poll

  window.eval(inboxJs);
  // let the load-time refreshBadge() promise settle
  await new Promise((r) => setTimeout(r, 30));

  const $ = (s) => window.document.querySelector(s);
  return { $, fetched, window };
}

// --- Case A: non-review token (can_review:false) -> button hidden, no inbox fetch
{
  const { $, fetched } = await run({ can_review: false, inbox_count: 0 }, 'non-review');
  assert($('#inboxBtn').hidden === true, 'A: inbox button HIDDEN for can_review:false');
  assert(
    !fetched.some((u) => u.indexOf('/api/inbox') !== -1),
    'A: /api/inbox never fetched for non-review token (got ' + JSON.stringify(fetched) + ')',
  );
  // Opening must be a no-op: click should not reveal the inbox section.
  $('#inboxBtn').click();
  await new Promise((r) => setTimeout(r, 20));
  assert($('#inboxSection').hidden !== false, 'A: clicking hidden button does not open inbox');
}

// --- Case B: review token (can_review:true) -> button shown + badge from inbox_count
{
  const { $ } = await run({ can_review: true, inbox_count: 4 }, 'review');
  assert($('#inboxBtn').hidden === false, 'B: inbox button SHOWN for can_review:true');
  assert(!$('#inboxBadge').hidden, 'B: badge visible when count > 0');
  assert($('#inboxBadgeCount').textContent === '4', 'B: badge count = 4 (got ' + $('#inboxBadgeCount').textContent + ')');
}

if (failures > 0) {
  console.error(failures + ' failure(s)');
  process.exit(1);
}
console.log('ALL_OK');
