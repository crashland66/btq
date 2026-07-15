import fs from 'node:fs';
import {createRequire} from 'node:module';

const [fixturePath, jsdomApi, scenario] = process.argv.slice(2);
const require = createRequire(import.meta.url);
const {JSDOM} = require(jsdomApi);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function scriptBody() {
  return fixture.script.replace(/^\s*<script>\s*/, '').replace(/\s*<\/script>\s*$/, '');
}

function installFileDragSupport(window) {
  function DataTransferItemList() {}
  DataTransferItemList.prototype.add = function () {};
  window.DataTransferItemList = DataTransferItemList;
}

function makeDom({supported = true, fetchImpl} = {}) {
  const dom = new JSDOM(fixture.html, {
    runScripts: 'outside-only',
    url: 'https://dashboard.test/field-photos?site_id=7050',
  });
  if (supported) installFileDragSupport(dom.window);
  else delete dom.window.DataTransferItemList;
  dom.window.fetch = fetchImpl || (() => {
    throw new Error('unexpected fetch');
  });
  dom.window.eval(scriptBody());
  dom.window.BTQPhotoFileDrag.init(dom.window.document);
  return dom;
}

function jpegResponse(window) {
  const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
  return {
    ok: true,
    blob: async () => new window.Blob([bytes], {type: 'image/jpeg'}),
  };
}

function dragTransfer(window, {rejectFile = false} = {}) {
  const fileItems = [];
  const compatibility = new Map();
  const transfer = {
    items: {
      add(file) {
        if (rejectFile) return null;
        if (!(file instanceof window.File)) return null;
        const item = {kind: 'file', type: file.type, getAsFile: () => file};
        fileItems.push(item);
        return item;
      },
    },
    clearData() {
      compatibility.clear();
    },
    setData(type, value) {
      compatibility.set(type, String(value));
    },
    setDragImage() {},
    effectAllowed: 'none',
  };
  return {transfer, fileItems, compatibility};
}

function controls(dom) {
  return Array.from(dom.window.document.querySelectorAll('[data-photo-file-drag]'));
}

async function preparedDrag() {
  let resolveFetch;
  let fetchCount = 0;
  const dom = makeDom({
    fetchImpl: () => {
      fetchCount += 1;
      return new Promise((resolve) => { resolveFetch = resolve; });
    },
  });
  const [control] = controls(dom);
  assert(control, 'real rendered drag control is missing');
  assert(control.dataset.photoDragState === 'idle', 'supported control must begin idle');
  assert(fetchCount === 0, 'initialization must not eagerly fetch media');
  const expectedName = control.getAttribute('data-photo-drag-filename');
  assert(control.textContent.includes('Drag or download'), 'idle visible label must describe both available actions');
  assert(control.getAttribute('aria-label') === 'Drag or download ' + expectedName, 'idle accessible label must be truthful');
  assert(control.getAttribute('title').includes('Hover or focus to prepare ' + expectedName), 'idle title must explain preparation timing');

  control.dispatchEvent(new dom.window.Event('pointerenter'));
  assert(fetchCount === 1, 'pointer entry must start exactly one preparation fetch');
  assert(control.dataset.photoDragState === 'preparing', 'pending fetch must say preparing');
  assert(control.textContent.includes('Preparing photo'), 'pending visible label must be truthful');
  assert(control.getAttribute('aria-label').startsWith('Preparing '), 'pending accessible label must be truthful');
  assert(control.getAttribute('title').startsWith('Preparing ' + expectedName), 'pending title must be truthful');

  resolveFetch(jpegResponse(dom.window));
  await flush();
  await flush();
  assert(control.dataset.photoDragState === 'ready', 'resolved image fetch must say ready');
  assert(control.textContent.includes('Ready — drag photo'), 'ready visible label must explicitly say ready');
  assert(control.getAttribute('aria-label') === 'Drag ' + expectedName + ' as a file, or click to download', 'ready accessible label must describe the File drag');
  assert(control.getAttribute('title').startsWith('Drag ' + expectedName + ' to a file drop target'), 'ready title must describe the File drag target');

  assert(expectedName && expectedName.endsWith('.jpg'), 'rendered filename helper must provide a JPEG name');
  const {transfer, fileItems, compatibility} = dragTransfer(dom.window);
  const event = new dom.window.Event('dragstart', {bubbles: true, cancelable: true});
  Object.defineProperty(event, 'dataTransfer', {value: transfer});
  control.dispatchEvent(event);

  assert(!event.defaultPrevented, 'a prepared supported drag must proceed');
  assert(fileItems.length === 1, `prepared drag must add exactly one File item; got ${fileItems.length}`);
  const file = fileItems[0].getAsFile();
  assert(file instanceof dom.window.File, 'drag item must contain a File, not a URL-only string');
  assert(file.name === expectedName, `drag File name must match rendered helper name ${expectedName}; got ${file.name}`);
  assert(file.type === 'image/jpeg', `drag File must retain image type; got ${file.type}`);
  assert(file.size === 4, `drag File must contain the fetched JPEG bytes; got ${file.size}`);
  assert(transfer.effectAllowed === 'copy', 'prepared file drag must advertise copy semantics');

  assert(compatibility.get('text/plain') === expectedName, 'plain compatibility text must be only the safe filename');
  assert(compatibility.get('text/uri-list') === 'https://dashboard.test/field-photos', 'URI compatibility text must name the dashboard page, not raw media');
  for (const value of compatibility.values()) {
    assert(!value.includes('/media/'), 'compatibility drag text must not expose the raw media URL');
    assert(!value.includes('raw-media-key'), 'compatibility drag text must not expose a raw media key');
    assert(!value.includes('/srv/'), 'compatibility drag text must not expose a filesystem path');
    assert(!value.includes('user:password'), 'compatibility drag text must not expose credentials');
  }
  assert(control.getAttribute('href').startsWith('/media/'), 'click fallback must retain the same-origin media href');
  assert(control.getAttribute('download') === expectedName, 'click fallback must retain the rendered download name');
}

async function failureStates() {
  let fetchCount = 0;
  const unsupported = makeDom({supported: false});
  const unsupportedControl = controls(unsupported)[0];
  assert(unsupportedControl.dataset.photoDragState === 'fallback', 'unsupported browser must say fallback');
  assert(unsupportedControl.draggable === false, 'unsupported browser must disable native dragging');
  assert(unsupportedControl.textContent.includes('Download photo'), 'unsupported browser must visibly offer download');
  assert(unsupportedControl.getAttribute('aria-label').startsWith('File dragging is unavailable; download '), 'unsupported accessible label must explain fallback');
  assert(unsupportedControl.getAttribute('title').startsWith('File dragging is unavailable; click to download '), 'unsupported title must explain fallback');
  assert(unsupportedControl.getAttribute('href').startsWith('/media/'), 'unsupported fallback must remain downloadable');

  const failed = makeDom({
    fetchImpl: async () => {
      fetchCount += 1;
      return {ok: false, blob: async () => { throw new Error('must not read failed body'); }};
    },
  });
  const failedControl = controls(failed)[0];
  failedControl.dispatchEvent(new failed.window.Event('focus'));
  assert(failedControl.dataset.photoDragState === 'preparing', 'in-flight failed request must first say preparing');
  await flush();
  await flush();
  assert(fetchCount === 1, 'failed preparation must make one request');
  assert(failedControl.dataset.photoDragState === 'fallback', 'failed fetch must degrade to fallback');
  assert(failedControl.textContent.includes('Download photo'), 'failed fetch must visibly offer download');
  assert(failedControl.getAttribute('title').startsWith('File dragging is unavailable; click to download '), 'failed fetch title must explain fallback');
  assert(failedControl.getAttribute('href').startsWith('/media/'), 'failed fetch fallback must remain downloadable');

  let resolveSlow;
  const timing = makeDom({fetchImpl: () => new Promise((resolve) => { resolveSlow = resolve; })});
  const timingControl = controls(timing)[0];
  timingControl.dispatchEvent(new timing.window.Event('pointerenter'));
  const premature = dragTransfer(timing.window);
  const earlyDrag = new timing.window.Event('dragstart', {bubbles: true, cancelable: true});
  Object.defineProperty(earlyDrag, 'dataTransfer', {value: premature.transfer});
  timingControl.dispatchEvent(earlyDrag);
  assert(earlyDrag.defaultPrevented, 'drag before preparation completes must be cancelled');
  assert(premature.fileItems.length === 0, 'drag before preparation completes must not claim a File');
  assert(timingControl.dataset.photoDragState === 'fallback', 'premature drag must truthfully show fallback');
  resolveSlow(jpegResponse(timing.window));
  await flush();
  await flush();
  assert(timingControl.dataset.photoDragState === 'ready', 'completed preparation may truthfully recover to ready');

  let invalidFetches = 0;
  const invalid = makeDom({fetchImpl: async () => { invalidFetches += 1; return jpegResponse(invalid.window); }});
  const invalidControl = controls(invalid)[0];
  invalidControl.setAttribute('data-photo-drag-url', 'https://user:password@evil.test/media/raw-media-key');
  await invalid.window.BTQPhotoFileDrag.prepare(invalidControl);
  assert(invalidFetches === 0, 'off-origin credentialed media must never be fetched');
  assert(invalidControl.dataset.photoDragState === 'fallback', 'off-origin media must degrade to download fallback');
}

async function lazyBoundedAndDeduplicated() {
  let fetchCount = 0;
  const dom = makeDom({
    fetchImpl: async () => {
      fetchCount += 1;
      return jpegResponse(dom.window);
    },
  });
  const all = controls(dom);
  assert(all.length === 50, `large-page fixture must render 50 controls; got ${all.length}`);
  assert(fetchCount === 0, '50-card initialization must retain/fetch zero image bodies');
  assert(all.every((control) => control.dataset.photoDragState === 'idle'), 'all supported controls must initialize idle');

  for (let index = 0; index < 7; index += 1) {
    await dom.window.BTQPhotoFileDrag.prepare(all[index]);
  }
  assert(fetchCount === 7, 'only explicitly prepared cards may fetch');
  assert(all.filter((control) => control.dataset.photoDragState === 'ready').length === 6, 'prepared cache must expose no more than six ready files');
  assert(all[0].dataset.photoDragState === 'idle', 'seventh preparation must evict the least-recently-used file');
  await dom.window.BTQPhotoFileDrag.prepare(all[0]);
  assert(fetchCount === 8, 'preparing an evicted card must fetch it again');
  assert(all[1].dataset.photoDragState === 'idle', 'LRU order must advance on re-preparation');
  assert(all.filter((control) => control.dataset.photoDragState === 'ready').length === 6, 're-preparation must keep cache bounded at six');

  let resolveShared;
  let sharedFetches = 0;
  const shared = makeDom({fetchImpl: () => {
    sharedFetches += 1;
    return new Promise((resolve) => { resolveShared = resolve; });
  }});
  const sharedControls = controls(shared);
  const commonUrl = sharedControls[0].getAttribute('data-photo-drag-url');
  const commonName = sharedControls[0].getAttribute('data-photo-drag-filename');
  sharedControls[1].setAttribute('data-photo-drag-url', commonUrl);
  sharedControls[1].setAttribute('data-photo-drag-filename', commonName);
  const first = shared.window.BTQPhotoFileDrag.prepare(sharedControls[0]);
  const second = shared.window.BTQPhotoFileDrag.prepare(sharedControls[1]);
  assert(sharedFetches === 1, 'duplicate controls for one media URL must share a pending request');
  assert(sharedControls[0].dataset.photoDragState === 'preparing' && sharedControls[1].dataset.photoDragState === 'preparing', 'duplicate controls must share truthful pending state');
  resolveShared(jpegResponse(shared.window));
  await Promise.all([first, second]);
  assert(sharedControls[0].dataset.photoDragState === 'ready' && sharedControls[1].dataset.photoDragState === 'ready', 'duplicate controls must share ready state');
}

const scenarios = {
  prepared_drag: preparedDrag,
  failure_states: failureStates,
  lazy_bounded: lazyBoundedAndDeduplicated,
};

if (!scenarios[scenario]) throw new Error(`unknown scenario: ${scenario}`);
await scenarios[scenario]();
console.log(`ALL_OK ${scenario}`);
