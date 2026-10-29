// Shared instrumentation for production-build and read-only field journeys.

/** Counts in one observation window; ongoing streams use a byte snapshot at arm. */
export function windowCounters(sample, baseline, t0, t1, apiRequests) {
	const feeds = sample.feeds || [];
	return {
		sseBytes: feeds.reduce((sum, feed, i) => sum + Math.max(0, feed.bytes - (baseline[i] || 0)), 0),
		fromSeqZeroReplays: feeds.filter((feed) => feed.start >= t0 && /[?&]from_seq=0(?:&|$)/.test(feed.url)).length,
		apiRequests,
		longTaskMs: (sample.longtasks || []).reduce((sum, [start, duration]) =>
			sum + Math.max(0, Math.min(start + duration, t1) - Math.max(start, t0)), 0),
		liveDomNodes: sample.dom
	};
}

export const PLATFORMS = {
	desktop: { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, isMobile: false, hasTouch: false, route: '/', cpu: 1 },
	mobile: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true, route: '/mobile/', cpu: 4 }
};

/** Only the environment may supply a bearer access URL. Never persist it. */
export async function authenticate(browser, access, options = {}) {
	if (!access) throw new Error('Set PERF_ACCESS_URL in the environment (never an access file)');
	const context = await browser.newContext({ ...options, serviceWorkers: 'block' });
	try {
		const response = await context.request.get(access, { maxRedirects: 0 });
		if (response.status() >= 400) throw new Error(`Access refused (${response.status()})`);
		return await context.storageState();
	} finally {
		await context.close();
	}
}

import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

export function loadPlaywright() {
	const candidates = [
		process.env.PERF_PLAYWRIGHT_CORE,
		path.join(REPO, 'browser-mcp', 'package.json'),
		path.join(REPO, 'frontend', 'package.json'),
		path.join(REPO, 'package.json')
	].filter(Boolean);
	for (const c of candidates) {
		try {
			return createRequire(c)('playwright-core');
		} catch {
			/* try the next one */
		}
	}
	throw new Error('playwright-core not found: run `npm ci` in browser-mcp/ (or set PERF_PLAYWRIGHT_CORE to a package.json that can resolve it)');
}

// ── selectors (live DOM of revision 04d9a0cfb, 2026-10-06) ────────────────────
export const SEL = {
	composer: '[role="textbox"][aria-label="Message"][contenteditable="true"]',
	sidebarRow: 'aside.sidebar [data-session-card]',
	log: '[role="log"][aria-label="Conversation messages"]',
	skeleton: '.sk-turn',
	rows: '.message-container, [data-user-turn-id], .userrow'
};

// Installed before any app script runs, in every document.
export function initScript(SEL) {
	const P = (window.__perf = { marks: {}, cards: [], longtasks: [], paints: {}, lcp: 0, ws: [], feeds: [], open: null, send: null });
	try { performance.setResourceTimingBufferSize(5000); } catch { /* old engines */ }
	try {
		new PerformanceObserver((l) => {
			for (const e of l.getEntries()) P.longtasks.push([Math.round(e.startTime), Math.round(e.duration)]);
		}).observe({ type: 'longtask', buffered: true });
	} catch { /* unsupported */ }
	try {
		new PerformanceObserver((l) => {
			for (const e of l.getEntries()) P.paints[e.name] = Math.round(e.startTime);
		}).observe({ type: 'paint', buffered: true });
		new PerformanceObserver((l) => {
			for (const e of l.getEntries()) P.lcp = Math.round(e.startTime);
		}).observe({ type: 'largest-contentful-paint', buffered: true });
	} catch { /* unsupported */ }
	// WebSocket frames, on the page clock (session events travel over WS).
	const W = window.WebSocket;
	if (W) {
		const peek = (d) => {
			if (typeof d !== 'string') return ['bin', 0];
			const m = /"type"\s*:\s*"([^"]+)"/.exec(d.slice(0, 400));
			return [m ? m[1] : '?', d.length];
		};
		const Wrapped = function (...args) {
			const ws = new W(...args);
			const url = String(args[0]).replace(/^wss?:\/\/[^/]+/, '').replace(/\?.*$/, '');
			ws.addEventListener('message', (e) => {
				if (P.ws.length < 20000) P.ws.push([Math.round(performance.now()), 'in', url, ...peek(e.data)]);
			});
			const send = ws.send.bind(ws);
			ws.send = (d) => {
				if (P.ws.length < 20000) P.ws.push([Math.round(performance.now()), 'out', url, ...peek(d)]);
				return send(d);
			};
			return ws;
		};
		Wrapped.prototype = W.prototype;
		Object.assign(Wrapped, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
		window.WebSocket = Wrapped;
	}
	// The transcript arrives over a long-lived fetch stream (GET
	// /sessions/:id/events?from_seq=N, stores/session/v2/sse-feed.ts) that
	// resource timing only reports once it closes. Tap it: headers, first byte,
	// arrival of the `caught_up` sentinel, bytes and chunks, on the page clock.
	const F = window.fetch;
	window.fetch = async function (input, init) {
		const url = typeof input === 'string' ? input : (input && input.url) || String(input);
		if (!/\/events\?/.test(url) || !/[?&]from_seq=/.test(url)) return F.call(this, input, init);
		const rec = {
			url: url.replace(/^https?:\/\/[^/]+/, '').replace(/[0-9a-f]{8}-[0-9a-f-]{27}/, ':id'),
			start: Math.round(performance.now()), headers: null, first: null, caughtUp: null, bytes: 0, chunks: 0, status: null
		};
		if (P.feeds.length < 200) P.feeds.push(rec);
		const res = await F.call(this, input, init);
		rec.headers = Math.round(performance.now());
		rec.status = res.status;
		if (!res.body) return res;
		const dec = new TextDecoder();
		let tail = '';
		const tap = new TransformStream({
			transform(chunk, c) {
				const now = Math.round(performance.now());
				if (rec.first == null) rec.first = now;
				rec.bytes += chunk.byteLength;
				rec.chunks++;
				if (rec.caughtUp == null) {
					const text = tail + dec.decode(chunk, { stream: true });
					// Match the SSE event line, not the word inside a frame's text.
					if (/(^|\n)event: caught_up\r?\n/.test(text)) rec.caughtUp = now;
					tail = text.slice(-24);
				}
				c.enqueue(chunk);
			}
		});
		return new Response(res.body.pipeThrough(tap), { status: res.status, statusText: res.statusText, headers: res.headers });
	};
	const visible = (e) => {
		if (!e) return false;
		const r = e.getBoundingClientRect();
		return r.width > 0 && r.height > 0 && !e.closest('[inert],[aria-hidden="true"]');
	};
	const checks = {
		sidebar: () => visible(document.querySelector(SEL.sidebarRow)),
		sidebarAttached: () => !!document.querySelector(SEL.sidebarRow),
		composer: () => visible(document.querySelector(SEL.composer)),
		transcript: () => {
			const log = document.querySelector(SEL.log);
			return !!log && !log.querySelector(SEL.skeleton) && !!log.querySelector(SEL.rows);
		},
		skeleton: () => !!document.querySelector(SEL.log + ' ' + SEL.skeleton),
		// The assistant landing's "Open conversation" button; present on desktop
		// and mobile (the desktop-only rail button is not).
		assistant: () => [...document.querySelectorAll('main button, button')].some((b) => /Open conversation/.test(b.textContent || '') && visible(b))
	};
	const tick = () => {
		const now = performance.now();
		// Sidebar card-count timeline: the first card can be the deep-linked chat
		// itself, so "list rendered" is computed afterwards as the first frame at
		// which the count reached its final value (sidebarFull).
		const n = document.querySelectorAll(SEL.sidebarRow).length;
		if (!P.cards.length || P.cards[P.cards.length - 1][1] !== n) P.cards.push([Math.round(now), n]);
		for (const k in checks) {
			if (!(k in P.marks)) {
				try { if (checks[k]()) P.marks[k] = Math.round(now); } catch { /* DOM mid-update */ }
			}
		}
		if (P.open && !P.open.marks.transcript) {
			const o = P.open;
			const log = document.querySelector(SEL.log);
			const sk = log && log.querySelector(SEL.skeleton);
			if (sk && !o.marks.skeleton) o.marks.skeleton = Math.round(now);
			if (log && !sk) {
				const fresh = [...log.querySelectorAll(SEL.rows)].filter((r) => !o.old.has(r));
				if (fresh.length) {
					o.marks.transcript = Math.round(now);
					o.rows = fresh.length;
				}
			}
		}
		if (P.open && P.open.marks.transcript && !P.open.settled) {
			const log = document.querySelector(SEL.log);
			const h = log ? log.scrollHeight : 0;
			if (h !== P.open.h) { P.open.h = h; P.open.lastChange = Math.round(now); }
			if (now - P.open.marks.transcript > 3000) P.open.settled = true;
		}
		if (P.send && !P.send.done) {
			const s = P.send;
			const log = document.querySelector(SEL.log);
			if (log) {
				const fresh = [...log.querySelectorAll(SEL.rows)].filter((r) => !s.old.has(r));
				const user = fresh.find((r) => r.matches('.userrow, [data-user-turn-id]') || r.querySelector('.bubble'));
				if (user && !s.marks.userEcho) s.marks.userEcho = Math.round(now);
				const agent = fresh.find((r) => !(r.matches('.userrow, [data-user-turn-id]') || r.querySelector('.bubble')));
				if (agent && !s.marks.agentVisible) s.marks.agentVisible = Math.round(now);
				const activity = document.querySelector('[data-testid="turn-activity"], .streaming-indicator');
				if (activity && !s.marks.activityVisible) s.marks.activityVisible = Math.round(now);
				const text = fresh
					.filter((r) => !(r.matches('.userrow, [data-user-turn-id]') || r.querySelector('.bubble')))
					.map((r) => (r.querySelector('.abody, .prose') || {}).textContent || '')
					.join('');
				if (/\S/.test(text) && !s.marks.firstToken) { s.marks.firstToken = Math.round(now); s.text = text.slice(0, 80); }
				if (log.getAttribute('aria-busy') === 'true' && !s.marks.busy) s.marks.busy = Math.round(now);
				if (s.marks.firstToken && log.getAttribute('aria-busy') !== 'true' && !s.marks.done) {
					s.marks.done = Math.round(now);
					s.text = text.slice(0, 80);
					s.done = true;
				}
			}
		}
		requestAnimationFrame(tick);
	};
	requestAnimationFrame(tick);
	P.armOpen = (sessionId) => {
		P.open = { old: new WeakSet(document.querySelectorAll(SEL.rows)), marks: {}, h: 0, lastChange: 0, settled: false };
		P.open.t0 = performance.now();
		window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId } }));
		P.open.t0 = Math.round(P.open.t0);
	};
	P.armSend = () => {
		P.send = { old: new WeakSet(document.querySelectorAll(SEL.rows)), marks: {}, done: false, t0: null };
		// Not `once`: typing the prompt fires keydown first; only Enter counts.
		const onKey = (e) => {
			if (e.key !== 'Enter' || e.shiftKey) return;
			if (P.send && P.send.t0 == null) P.send.t0 = Math.round(performance.now());
			document.removeEventListener('keydown', onKey, { capture: true });
		};
		document.addEventListener('keydown', onKey, { capture: true });
	};
}

// ── resource helpers ─────────────────────────────────────────────────────────
export function normalizePath(u) {
	let p = u.replace(/^https?:\/\/[^/]+/, '');
	if (p.startsWith('/_app/immutable/')) {
		const ext = (/\.(\w+)$/.exec(p.split('?')[0]) || [])[1] || 'other';
		return `static:${ext}`;
	}
	const [base, q] = p.split('?');
	const keys = q ? '?' + q.split('&').map((kv) => kv.split('=')[0]).filter((k) => k !== 'token').join('&') : '';
	return base
		.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, ':id')
		.replace(/pv2-[0-9a-f]+/g, ':pv2')
		.replace(/\/projects\/[^/]+\/git\//, '/projects/:p/git/') + keys;
}

export async function collect(page) {
	return page.evaluate(() => {
		const P = window.__perf;
		const nav = performance.getEntriesByType('navigation')[0];
		const res = performance.getEntriesByType('resource').map((e) => ({
			u: e.name,
			s: Math.round(e.startTime),
			e: Math.round(e.responseEnd),
			ttfb: Math.round(e.responseStart - e.startTime),
			tb: e.transferSize,
			eb: e.encodedBodySize,
			db: e.decodedBodySize,
			it: e.initiatorType
		}));
		return {
			marks: { ...P.marks },
			cards: P.cards.slice(),
			paints: { ...P.paints },
			lcp: P.lcp,
			longtasks: P.longtasks.slice(),
			nav: nav ? {
				ttfb: Math.round(nav.responseStart),
				dcl: Math.round(nav.domContentLoadedEventEnd),
				load: Math.round(nav.loadEventEnd),
				tb: nav.transferSize
			} : null,
			res,
			ws: P.ws.slice(),
			feeds: P.feeds.map((f) => ({ ...f })),
			open: P.open ? { t0: P.open.t0, marks: { ...P.open.marks }, rows: P.open.rows, lastChange: P.open.lastChange } : null,
			send: P.send ? { t0: P.send.t0, marks: { ...P.send.marks }, text: P.send.text } : null,
			measures: performance.getEntriesByType('measure').filter((m) => m.name.startsWith('shuffle:'))
				.map((m) => ({ name: m.name, s: Math.round(m.startTime), d: Math.round(m.duration) })),
			dom: document.getElementsByTagName('*').length
		};
	});
}

