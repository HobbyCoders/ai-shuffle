const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../../main/assets/chat-downloads.js'), 'utf8');

function fixture(rejectType) {
  const messages = [], handlers = {}, urls = new Map();
  let sequence = 0, receiver;
  class Anchor {
    constructor(href, download = 'fixture.txt') { this.href = href; this.download = download; }
    hasAttribute(name) { return name === 'download'; }
    closest() { return this; }
    click() { throw Error('Browser download should have been intercepted'); }
  }
  class FixtureURL extends URL {}
  FixtureURL.createObjectURL = blob => { const id = `blob:https://worker.example/${++sequence}`; urls.set(id, blob); return id; };
  FixtureURL.revokeObjectURL = url => urls.delete(url);
  const window = {};
  window.top = window;
  window.shuffleFileSave = {
    addEventListener(type, listener) { receiver = listener; },
    postMessage(raw) {
      const message = JSON.parse(raw); messages.push(message);
      if (message.type === 'abort') return;
      queueMicrotask(() => receiver({data: JSON.stringify({id: message.id, ok: message.type !== rejectType})}));
    }
  };
  const context = vm.createContext({window, URL: FixtureURL, Blob, HTMLAnchorElement: Anchor, Element: Anchor,
    document: {addEventListener(type, handler) { handlers[type] = handler; }}, location: {origin: 'https://worker.example', href: 'https://worker.example/'},
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
