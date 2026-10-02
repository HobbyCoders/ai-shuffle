/* Injected at document start on the paired worker's phone UI page (/mobile/). No credential access. */
(() => {
  if (window !== window.top || !window.shuffleFileSave) return;
  const bridge = window.shuffleFileSave;
  // Files come from the paired worker, whose origin the app hands the page as
  // window.__SHUFFLE_MOBILE__.apiOrigin (frontend/src/lib/api/runtime.ts). Read
  // it per click: the bootstrap script and this one are both document-start
  // scripts, and the page may not have run either yet when this evaluates.
  const workerOrigin = () => {
    const runtime = window.__SHUFFLE_MOBILE__;
    return runtime && typeof runtime.apiOrigin === 'string' ? runtime.apiOrigin.replace(/\/+$/, '') : location.origin;
  };
  const limit = 128 * 1024 * 1024;
  const chunkSize = 32 * 1024;
  const blobs = new Map();
  const replies = new Map();
  const createObjectURL = URL.createObjectURL.bind(URL);
  const revokeObjectURL = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = value => {
    const url = createObjectURL(value);
    if (value instanceof Blob) blobs.set(url, value);
    return url;
  };
  URL.revokeObjectURL = url => { blobs.delete(String(url)); revokeObjectURL(url); };
  bridge.addEventListener('message', event => {
    let reply;
    try { reply = JSON.parse(event.data); } catch (_) { return; }
    const pending = replies.get(reply.id);
    if (!pending) return;
    replies.delete(reply.id);
    clearTimeout(pending.timer);
    reply.ok ? pending.resolve(reply) : pending.reject(new Error('File save cancelled'));
  });
  function request(message) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        replies.delete(message.id);
        reject(new Error('File save expired'));
      }, 310000);
      replies.set(message.id, {resolve, reject, timer});
      bridge.postMessage(JSON.stringify(message));
    });
  }
  function dataImage(url) {
    const match = /^data:(image\/[a-z0-9.+-]+)(;base64)?,([\s\S]*)$/i.exec(url);
    if (url.length > limit * 1.4 + 1024) throw new Error('size');
    if (!match) throw new Error('Unsupported image');
    if (!match[2]) return new Blob([decodeURIComponent(match[3])], {type: match[1]});
    const raw = atob(match[3]);
    if (raw.length > limit) throw new Error('size');
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return new Blob([bytes], {type: match[1]});
  }
  // The phone UI's own routes: in-app navigation, never a file to save.
  const uiRoute = url => url.origin === location.origin && (url.pathname === '/mobile' || url.pathname.startsWith('/mobile/'));
  function workerUrl(href) {
    try {
      const url = new URL(href, location.href);
      if (url.username || url.password || url.protocol !== 'https:') return null;
      if (uiRoute(url)) return null;
      if (url.origin === workerOrigin()) return url.href;
      // Pages from an app-origin build (before the worker served /mobile/) resolved root-relative /api URLs there.
      if (url.origin === location.origin && url.pathname.startsWith('/api/')) {
        const worker = new URL(workerOrigin());
        if (worker.protocol === 'https:' && !worker.username && !worker.password) {
          return worker.origin + url.pathname + url.search;
        }
      }
    } catch (_) { /* Not a worker file. */ }
    return null;
  }
  async function save(anchor) {
    const id = crypto.randomUUID();
    let started = false;
    try {
      const href = anchor.href;
      const url = new URL(href, location.href);
      const filename = url.searchParams.get('path')?.split(/[/\\]/).pop();
      const name = anchor.download || filename || anchor.textContent?.trim() || 'download';
      const worker = workerUrl(href);
      if (worker) {
        started = true;
        await request({type: 'url', id, url: worker, name});
        return;
      }
      // Capture the Blob synchronously: existing frontend code revokes its URL after click().
      // Fetching blob/data URLs would be blocked by the contained page's connect-src policy.
      const blob = url.protocol === 'blob:' && url.origin === location.origin ? blobs.get(href) :
        url.protocol === 'data:' ? dataImage(href) : null;
      if (!blob) { started = true; await request({type: 'error', id, reason: 'unsupported'}); return; }
      if (blob.size > limit) {
        started = true;
        await request({type: 'error', id, reason: 'size'});
        return;
      }
      started = true;
      await request({type: 'begin', id, name, mime: blob.type, size: blob.size});
      let sequence = 0;
      for (let offset = 0; offset < blob.size; offset += chunkSize) {
        const bytes = new Uint8Array(await blob.slice(offset, offset + chunkSize).arrayBuffer());
        let binary = '';
        for (const byte of bytes) binary += String.fromCharCode(byte);
        await request({type: 'chunk', id, sequence: sequence++, data: btoa(binary)});
      }
      await request({type: 'finish', id, sequence});
    } catch (error) {
      bridge.postMessage(JSON.stringify(started ? {type: 'abort', id} : {type: 'error', id, reason: error.message === 'size' ? 'size' : 'unsupported'}));
    }
  }
  function handles(anchor) {
    return anchor instanceof HTMLAnchorElement && (anchor.hasAttribute('download') ||
      anchor.href.startsWith('blob:') || anchor.href.startsWith('data:') || workerUrl(anchor.href) !== null);
  }
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (handles(this)) { void save(this); return; }
    return click.call(this);
  };
  document.addEventListener('click', event => {
    const anchor = event.target instanceof Element ? event.target.closest('a') : null;
    if (!handles(anchor) || event.defaultPrevented) return;
    event.preventDefault();
    void save(anchor);
  }, true);
})();
