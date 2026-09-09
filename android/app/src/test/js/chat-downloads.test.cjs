const {test} = require('node:test');
test('ordinary Notes attachments and relative markdown file links use authenticated native save', async () => {
  const location = {origin: 'https://appassets.androidplatform.net', href: 'https://appassets.androidplatform.net/assets/mobile-ui/'};
  const runtime = {apiOrigin: 'https://worker.example', token: 'never-in-the-url'};
  for (const [href, expected] of [
    ['https://worker.example/api/v1/notes/n/attachments/a/download', 'https://worker.example/api/v1/notes/n/attachments/a/download'],
    ['https://appassets.androidplatform.net/api/files/by-path?path=%2Fwork%2Freport.pdf', 'https://worker.example/api/files/by-path?path=%2Fwork%2Freport.pdf'],
    ['https://worker.example/api/generated-images/by-path?path=%2Fwork%2Fimage.png', 'https://worker.example/api/generated-images/by-path?path=%2Fwork%2Fimage.png']
  ]) {
    const f = fixture(undefined, {location, runtime});
    const anchor = new f.Anchor(href, null);
    let prevented = false;
    f.handlers.click({target: anchor, defaultPrevented: false, preventDefault() { prevented = true; }});
    await settled();
    assert.equal(prevented, true, href);
    assert.equal(f.messages[0].type, 'url');
    assert.equal(f.messages[0].url, expected);
    assert.ok(!JSON.stringify(f.messages).includes(runtime.token));
  }
});
test('credential-bearing URLs are never sent to native save and external ordinary links stay untouched', async () => {
  const runtime = {apiOrigin: 'https://worker.example'};
  const f = fixture(undefined, {runtime});
  new f.Anchor('https://name:password@worker.example/api/files/by-path?path=x').click();
  await settled();
  assert.equal(f.messages[0].type, 'error');
  for (const href of ['https://outside.example/file', 'http://worker.example/file', 'https://worker.example:8443/file', 'https://worker.example.evil/file']) {
    const g = fixture(undefined, {runtime});
    g.handlers.click({target: new g.Anchor(href, null), defaultPrevented: false, preventDefault() { assert.fail('External navigation was captured'); }});
    await settled();
    assert.equal(g.messages.length, 0);
  }
});

const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../../main/assets/chat-downloads.js'), 'utf8');

function fixture(rejectType, options = {}) {
  const messages = [], handlers = {}, urls = new Map();
  let sequence = 0, receiver;
  class Anchor {
    constructor(href, download = 'fixture.txt') { this.href = href; this.download = download; }
    hasAttribute(name) { return name === 'download' && this.download !== null; }
    closest() { return this; }
    click() { throw Error('Browser download should have been intercepted'); }
  }
  class FixtureURL extends URL {}
  FixtureURL.createObjectURL = blob => { const id = `blob:${options.blobOrigin || 'https://worker.example'}/${++sequence}`; urls.set(id, blob); return id; };
  FixtureURL.revokeObjectURL = url => urls.delete(url);
  const window = {};
  window.top = window;
  if (options.runtime) window.__SHUFFLE_MOBILE__ = options.runtime;
  window.shuffleFileSave = {
    addEventListener(type, listener) { receiver = listener; },
    postMessage(raw) {
      const message = JSON.parse(raw); messages.push(message);
      if (message.type === 'abort') return;
      queueMicrotask(() => receiver({data: JSON.stringify({id: message.id, ok: message.type !== rejectType})}));
    }
  };
  const context = vm.createContext({window, URL: FixtureURL, Blob, HTMLAnchorElement: Anchor, Element: Anchor,
    document: {addEventListener(type, handler) { handlers[type] = handler; }}, location: options.location || {origin: 'https://worker.example', href: 'https://worker.example/'},
    crypto: require('node:crypto').webcrypto, setTimeout, clearTimeout, Uint8Array, atob, btoa, decodeURIComponent});
  vm.runInContext(script, context);
  return {messages, handlers, Anchor, URL: FixtureURL, window};
}
async function settled() { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); }

test('existing fetch/blob/unattached click/immediate revoke preserves bytes in bounded chunks', async () => {
  const f = fixture(); const payload = 'small benign fixture\n'.repeat(5000);
  const url = f.URL.createObjectURL(new Blob([payload], {type: 'text/plain'}));
  new f.Anchor(url, 'notes.txt').click(); f.URL.revokeObjectURL(url);
  await settled();
  assert.equal(f.messages[0].type, 'begin'); assert.equal(f.messages[0].name, 'notes.txt');
  const chunks = f.messages.filter(m => m.type === 'chunk');
  assert.ok(chunks.length > 1);
  chunks.forEach((chunk, i) => { assert.equal(chunk.sequence, i); assert.ok(Buffer.from(chunk.data, 'base64').length <= 32768); });
  assert.equal(Buffer.concat(chunks.map(c => Buffer.from(c.data, 'base64'))).toString(), payload);
  assert.equal(f.messages.at(-1).type, 'finish');
});
test('inline data image and a normal connected anchor use the save protocol', async () => {
  const f = fixture();
  const image = Buffer.from([137, 80, 78, 71]);
  const anchor = new f.Anchor(`data:image/png;base64,${image.toString('base64')}`, 'image.png');
  let prevented = false;
  f.handlers.click({target: anchor, defaultPrevented: false, preventDefault() { prevented = true; }});
  await settled();
  assert.equal(prevented, true); assert.equal(f.messages[0].mime, 'image/png');
  assert.deepEqual(Buffer.from(f.messages.find(m => m.type === 'chunk').data, 'base64'), image);
});
test('same-origin URL stays native and external or file URLs never become native requests', async () => {
  const f = fixture(); new f.Anchor('https://worker.example/download/report', 'report.pdf').click();
  await settled(); assert.equal(f.messages[0].type, 'url'); assert.equal(f.messages[0].name, 'report.pdf');
  for (const url of ['https://outside.example/file', 'http://worker.example/file', 'file:///secret', 'blob:https://outside.example/blob']) {
    const isolated = fixture(); new isolated.Anchor(url).click(); await settled();
    assert.equal(isolated.messages[0].type, 'error');
    assert.ok(isolated.messages.every(m => m.type !== 'url' && m.type !== 'begin'));
  }
});
test('denied or cancelled save stops blob transfer before sending any bytes', async () => {
  const f = fixture('begin'); new f.Anchor(f.URL.createObjectURL(new Blob(['fixture']))).click(); await settled();
  assert.deepEqual(f.messages.map(m => m.type), ['begin', 'abort']);
});
test('empty blob finishes and declared oversize is reported without copying data', async () => {
  const f = fixture(); new f.Anchor(f.URL.createObjectURL(new Blob([]))).click(); await settled();
  assert.deepEqual(f.messages.map(m => m.type), ['begin', 'finish']);
  const large = fixture(); const blob = new Blob([]); Object.defineProperty(blob, 'size', {value: 128 * 1024 * 1024 + 1});
  new large.Anchor(large.URL.createObjectURL(blob)).click(); await settled();
  assert.equal(large.messages[0].type, 'error'); assert.equal(large.messages[0].reason, 'size');
});

test('on the bundled UI, worker URLs from the injected runtime stay native; the asset origin and others do not', async () => {
  const location = {origin: 'https://appassets.androidplatform.net', href: 'https://appassets.androidplatform.net/assets/mobile-ui/'};
  const runtime = {apiOrigin: 'https://worker.example/', token: 'shc_fixture'};
  const f = fixture(undefined, {location, runtime});
  new f.Anchor('https://worker.example/api/v1/workspace/files/download?path=report.pdf', 'report.pdf').click();
  await settled();
  assert.equal(f.messages[0].type, 'url'); assert.equal(f.messages[0].url, 'https://worker.example/api/v1/workspace/files/download?path=report.pdf');
  for (const url of ['https://appassets.androidplatform.net/assets/mobile-ui/index.html', 'https://outside.example/file', 'http://worker.example/file']) {
    const g = fixture(undefined, {location, runtime}); new g.Anchor(url, 'x').click(); await settled();
    assert.equal(g.messages[0].type, 'error'); assert.equal(g.messages[0].reason, 'unsupported');
  }
  // The bundle's own blob: URLs (same origin as the page) still stream through the bridge.
  const h = fixture(undefined, {location, runtime, blobOrigin: location.origin});
  const payload = 'bundle blob';
  const url = h.URL.createObjectURL(new Blob([payload], {type: 'text/plain'}));
  new h.Anchor(url, 'notes.txt').click();
  await settled();
  assert.equal(h.messages[0].type, 'begin'); assert.equal(h.messages[0].size, payload.length);
  assert.equal(h.messages.at(-1).type, 'finish');
});
