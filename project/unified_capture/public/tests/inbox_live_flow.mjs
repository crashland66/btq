import { JSDOM } from '/tmp/node_modules/jsdom/lib/api.js';
import fs from 'fs';

const html = fs.readFileSync('../index.html', 'utf8');
let inboxJs = fs.readFileSync('../inbox.js', 'utf8');
// Flip to live mode to exercise real fetch + _rev + 409 mapping.
inboxJs = inboxJs.replace('var INBOX_USE_MOCK = true;', 'var INBOX_USE_MOCK = false;');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://fc.example.com/?token=demo' });
const { window } = dom;
global.window = window; global.document = window.document;
window.setInterval = () => 0;

function assert(c,m){ if(!c){console.error('FAIL:',m); process.exitCode=1;} else console.log('ok -',m); }

const calls = [];
window.fetch = (url, opts) => {
  calls.push({ url, opts });
  const u = String(url);
  if (u.endsWith('/api/session')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ inbox_count: 2 }) });
  if (u.endsWith('/api/inbox')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ count:2, items:[
    { candidate_id:'c1', _rev:'7-xyz', source:'voice', site:'site-a', summary:'s1', evidence:'e1', created_at:'now', proposed_action:{ title:'T1', job_type:'append_to_note', payload:{a:1} } },
    { candidate_id:'c2', _rev:'3-pqr', source:'note', site:'site-b', summary:'s2', evidence:'e2', created_at:'now', proposed_action:{ title:'T2', job_type:'log_supply_need', payload:{b:2} } },
  ]}) });
  if (u.endsWith('/api/inbox/approve')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ status:'approved' }) });
  if (u.endsWith('/api/inbox/reject')) return Promise.resolve({ ok:false, status:409, json:()=>Promise.resolve({ status:'already_decided', candidate_id:'c2' }) });
  return Promise.reject(new Error('unexpected url '+u));
};

window.eval(inboxJs);
const $ = s => window.document.querySelector(s);
await new Promise(r=>setTimeout(r,20));

// badge from /api/session.inbox_count
assert($('#inboxBadgeCount').textContent==='2','live badge from session.inbox_count=2');
const sessionCall = calls.find(c=>String(c.url).endsWith('/api/session'));
assert(sessionCall && sessionCall.opts.headers.Authorization==='Bearer demo','session sent Bearer token');

$('#inboxBtn').click();
await new Promise(r=>setTimeout(r,20));
assert($('.inbox-card').dataset.candidateId==='c1','live first card c1');

// approve c1 -> POST carries _rev 7-xyz
$('.inbox-card .approve').click();
await new Promise(r=>setTimeout(r,20));
const approveCall = calls.find(c=>String(c.url).endsWith('/api/inbox/approve'));
assert(!!approveCall,'approve POST made');
const ab = JSON.parse(approveCall.opts.body);
assert(ab.candidate_id==='c1' && ab._rev==='7-xyz','approve body carries candidate_id + _rev');
assert(approveCall.opts.headers['Content-Type']==='application/json','approve is JSON POST');

// now on c2 -> reject returns 409 -> already_decided handled gracefully
await new Promise(r=>setTimeout(r,10));
assert($('.inbox-card').dataset.candidateId==='c2','advanced to c2');
$('.inbox-card .reject').click();
await new Promise(r=>setTimeout(r,20));
const rejectCall = calls.find(c=>String(c.url).endsWith('/api/inbox/reject'));
const rb = JSON.parse(rejectCall.opts.body);
assert(rb._rev==='3-pqr','reject body carries _rev');
assert(/[Aa]lready handled/.test($('#inboxMessage').textContent),'409 mapped to already-handled message');
assert($('#inboxMessage').dataset.tone!=='error','409 not shown as error');
assert(!$('#inboxEmpty').hidden,'empty after both decided');
console.log('Live-mode (_rev + 409) assertions evaluated.');
