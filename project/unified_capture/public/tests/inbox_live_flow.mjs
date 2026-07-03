import fs from 'fs';

const jsdomPath = process.argv[2] || '/tmp/node_modules/jsdom/lib/api.js';
const { JSDOM } = await import(jsdomPath);

const html = fs.readFileSync('../index.html', 'utf8');
const inboxJs = fs.readFileSync('../inbox.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://fc.example.com/?token=demo' });
const { window } = dom;
global.window = window; global.document = window.document;
window.setInterval = () => 0;

function assert(c,m){ if(!c){console.error('FAIL:',m); process.exitCode=1;} else console.log('ok -',m); }

const routeOptions = [
  { value: 'open_issue', label: 'Open issue' },
  { value: 'access_constraint', label: 'Access constraint' },
  { value: 'supply_need', label: 'Supply need' },
  { value: 'equipment_request', label: 'Equipment request' },
  { value: 'no_action', label: 'No action' },
];
const calls = [];
window.fetch = (url, opts) => {
  calls.push({ url, opts });
  const u = String(url);
  if (u.endsWith('/api/session')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ can_review: true, inbox_count: 4 }) });
  if (u.endsWith('/api/inbox')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ count:4, route_options: routeOptions, items:[
    { draft_id:'d1', _rev:'7-xyz', source:'voice', site:'site-a', message:'s1', evidence:'e1', created_at:'now', group_id:'g1', job_type:'log_supply_need', default_route:'supply_need', payload:{site_id:'site-a', item_name:'liners', requested_by:'op'} },
    { draft_id:'d2', _rev:'3-pqr', source:'note', site:'site-b', message:'s2', evidence:'e2', created_at:'now', group_id:'g2', job_type:'append_to_note', default_route:'open_issue', payload:{path:'x', content:'y', destination:'site_note'} },
    { draft_id:'d3', _rev:'1-aaa', source:'voice', site:'site-c', message:'s3', evidence:'e3', created_at:'now', group_id:'g3', job_type:'flag_access_constraint', default_route:'access_constraint', payload:{site:'site-c', details:'gate'} },
    { draft_id:'d4', _rev:'1-bbb', source:'voice', site:'site-c', message:'s4', evidence:'e4', created_at:'now', group_id:'g3', job_type:'log_equipment_request', default_route:'equipment_request', payload:{site_id:'site-c', equipment_name:'vacuum', requested_by:'op'} },
  ]}) });
  if (u.endsWith('/api/inbox/approve')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ status:'approved' }) });
  if (u.endsWith('/api/inbox/reject')) return Promise.resolve({ ok:false, status:409, json:()=>Promise.resolve({ status:'already_decided', draft_id:'d2' }) });
  if (u.endsWith('/api/inbox/approve-set')) return Promise.resolve({ ok:true, status:200, json:()=>Promise.resolve({ ok:true, approved:1, rejected:1, results:[] }) });
  return Promise.reject(new Error('unexpected url '+u));
};

window.eval(inboxJs);
const $ = s => window.document.querySelector(s);
const $$ = s => Array.from(window.document.querySelectorAll(s));
await new Promise(r=>setTimeout(r,20));

assert($('#inboxBadgeCount').textContent==='4','live badge from session.inbox_count=4');
const sessionCall = calls.find(c=>String(c.url).endsWith('/api/session'));
assert(sessionCall && sessionCall.opts.headers.Authorization==='Bearer demo','session sent Bearer token');

$('#inboxBtn').click();
await new Promise(r=>setTimeout(r,20));
assert($('.inbox-card').dataset.draftId==='d1','live first card d1');
assert($('.inbox-route select').value === 'supply_need', 'single select uses API default route');

$('.inbox-route select').value = 'open_issue';
$('.inbox-card .approve').click();
await new Promise(r=>setTimeout(r,20));
const approveCall = calls.find(c=>String(c.url).endsWith('/api/inbox/approve'));
assert(!!approveCall,'approve POST made');
const ab = JSON.parse(approveCall.opts.body);
assert(ab.draft_id==='d1' && ab._rev==='7-xyz','approve body carries draft_id + _rev');
assert(ab.route === 'open_issue','approve body carries selected route');
assert(approveCall.opts.headers['Content-Type']==='application/json','approve is JSON POST');

await new Promise(r=>setTimeout(r,10));
assert($('.inbox-card').dataset.draftId==='d2','advanced to d2');
$('.inbox-card .reject').click();
await new Promise(r=>setTimeout(r,20));
const rejectCall = calls.find(c=>String(c.url).endsWith('/api/inbox/reject'));
const rb = JSON.parse(rejectCall.opts.body);
assert(rb.draft_id==='d2' && rb._rev==='3-pqr','reject body carries draft_id + _rev');
assert(!Object.prototype.hasOwnProperty.call(rb, 'route'),'reject body does not carry route');
assert(/[Aa]lready handled/.test($('#inboxMessage').textContent),'409 mapped to already-handled message');
assert($('#inboxMessage').dataset.tone!=='error','409 not shown as error');

await new Promise(r=>setTimeout(r,10));
assert($('.inbox-card').dataset.groupId==='g3','advanced to batch group');
const checks = $$('.inbox-checkitem input[type="checkbox"]');
const selects = $$('.inbox-checkitem select');
assert(selects[0].value === 'access_constraint', 'batch first select uses row default');
assert(selects[1].value === 'equipment_request', 'batch second select uses row default');
selects[0].value = 'open_issue';
checks[1].click();
$('.inbox-card .approve').click();
await new Promise(r=>setTimeout(r,20));
const setCall = calls.find(c=>String(c.url).endsWith('/api/inbox/approve-set'));
assert(!!setCall,'approve-set POST made');
const sb = JSON.parse(setCall.opts.body);
assert(sb.drafts[0].draft_id === 'd3' && sb.drafts[0].checked === true && sb.drafts[0].route === 'open_issue', 'checked batch row carries selected route');
assert(sb.drafts[1].draft_id === 'd4' && sb.drafts[1].checked === false, 'unchecked batch row is sent unchecked');
assert(!Object.prototype.hasOwnProperty.call(sb.drafts[1], 'route'), 'unchecked batch row does not carry route');

console.log('Live-mode route assertions evaluated.');
