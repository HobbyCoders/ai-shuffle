/* Injected at document start only on the connected HTTPS worker. No credential access. */
(() => {
  if (window !== window.top || !window.shuffleFileSave) return;
  const bridge = window.shuffleFileSave;
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
  async function save(anchor) {
    const id = crypto.randomUUID();
    let started = false;
    try {
      const href = anchor.href;
      const name = anchor.download || 'download';
      const url = new URL(href, location.href);
      if (url.protocol === 'https:' && url.origin === location.origin) {
        started = true;
        await request({type: 'url', id, url: href, name});
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
      anchor.href.startsWith('blob:') || anchor.href.startsWith('data:'));
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
