#!/usr/bin/env node
/**
 * AI Shuffle journey timing rig (perf sprint 2026-10, docs/perf/sprint-2026-10.md).
 *
 * Drives a real Chromium against a running worker and times three journeys:
 *   launch  app launch -> usable (composer typeable AND a sidebar session row
 *           rendered; the Assistant landing variant waits for its hero instead)
 *   open    open an existing chat -> transcript rendered (small and large chat,
 *           cold = first open in a fresh page load, warm = re-open in the same page)
 *   send    send a message -> first agent event visible, first token visible
 *           (creates ONE throwaway chat; archive it afterwards)
 * Every run records in-page milestones (rAF-sampled, <=16.7 ms resolution),
 * navigation/paint timings, long tasks (>50 ms) and every resource timing, so
 * client work can be separated from server work. Prints nearest-rank p75 and
 * min/max per cell and writes the raw per-run data as JSON.
 *
 * Needs: Node >= 20; playwright-core (the repo's browser-mcp package depends on
 * it: `cd browser-mcp && npm ci`); a Chromium (PERF_CHROME, default
 * /usr/bin/chromium). Access: PERF_ACCESS_URL = a preview-access URL for the
 * worker's preview listener (see .fluid/skills/preview-branch), or any URL that
 * signs the browser in and redirects into the app. The URL is a credential:
 * keep it in the environment; the rig prints only its origin and never writes
 * cookies or storage state to disk.
 *
 * Usage:
 *   PERF_ACCESS_URL=... node scripts/perf/journeys.mjs \
 *     --journeys launch,open --runs 20 --viewport 1280x800 \
 *     --small <session id> --large <session id> --other <session id> \
 *     --out /tmp/perf-run.json
 *   ... --journeys send --sends 5 --model 'prov:<provider id>:<model id>'
 *   ... --journeys send --sends 4 --sendSession <id>   (follow-ups in an existing chat)
 *   ... --probe launch.deeplink.cold   (delay each API path by 1.5 s and report
 *                                       which ones move the milestone)
 * Read-only for launch/open. `send` sends real prompts and creates a chat.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { summarize, median } from './stats.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');

function loadPlaywright() {
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

// ── arguments ───────────────────────────────────────────────────────────────
function parseArgs(argv) {
	const a = {
		journeys: 'launch,open', runs: 20, warmup: 1, viewport: '1280x800', small: null, large: null, other: null,
		out: null, probe: null, probeRuns: 3, probeDelay: 1500, sends: 0, model: null, prompt: 'Reply with exactly OK',
		settleMs: 2000, label: '', probePaths: '', sendSession: null
	};
	for (let i = 0; i < argv.length; i++) {
		const k = argv[i].replace(/^--/, '');
		const v = argv[i + 1];
		if (!(k in a)) throw new Error(`unknown flag --${k}`);
		a[k] = typeof a[k] === 'number' ? Number(v) : v;
		i++;
	}
	return a;
}

// ── selectors (live DOM of revision 04d9a0cfb, 2026-10-06) ────────────────────
const SEL = {
	composer: '[role="textbox"][aria-label="Message"][contenteditable="true"]',
	sidebarRow: 'aside.sidebar [data-session-card]',
	log: '[role="log"][aria-label="Conversation messages"]',
	skeleton: '.sk-turn',
	rows: '.message-container, [data-user-turn-id], .userrow'
};

// Installed before any app script runs, in every document.
function initScript(SEL) {
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
		if (!/\/events\?from_seq=/.test(url)) return F.call(this, input, init);
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

async function collect(page) {
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(page, fn, arg, timeout = 45000) {
	const t = Date.now();
	for (;;) {
		const v = await page.evaluate(fn, arg).catch(() => null);
		if (v) return v;
		if (Date.now() - t > timeout) throw new Error('timeout waiting for milestone');
		await sleep(50);
	}
}

/** Resolves when no fetch/XHR has been in flight for quietMs (cap capMs). */
function trackNetwork(page) {
	let inflight = 0;
	let last = Date.now();
	const isApi = (r) => ['fetch', 'xhr'].includes(r.resourceType());
	page.on('request', (r) => { if (isApi(r)) { inflight++; last = Date.now(); } });
	const done = (r) => { if (isApi(r)) { inflight = Math.max(0, inflight - 1); last = Date.now(); } };
	page.on('requestfinished', done);
	page.on('requestfailed', done);
	return async (quietMs = 800, capMs = 10000) => {
		const t = Date.now();
		while (Date.now() - t < capMs) {
			if (inflight === 0 && Date.now() - last >= quietMs) return true;
			await sleep(50);
		}
		return false;
	};
}

// ── cells ────────────────────────────────────────────────────────────────────
function contextOptions(vp, storageState) {
	const [w, h] = vp.split('x').map(Number);
	const mobile = w < 600;
	return {
		storageState,
		viewport: { width: w, height: h },
		deviceScaleFactor: mobile ? 3 : 1,
		isMobile: mobile,
		hasTouch: mobile
	};
}

async function newPage(browser, ctxOpts, { route } = {}) {
	const ctx = await browser.newContext(ctxOpts);
	await ctx.addInitScript(initScript, SEL);
	if (route) await ctx.route(() => true, route);
	const page = await ctx.newPage();
	const quiet = trackNetwork(page);
	const cdp = await ctx.newCDPSession(page);
	await cdp.send('Performance.enable');
	return { ctx, page, quiet, cdp };
}

/** Renderer main-thread time (ms) from CDP Performance.getMetrics: total task
 * time and its script / layout / style split. Cumulative; callers take deltas. */
async function cpuNow(cdp) {
	const { metrics } = await cdp.send('Performance.getMetrics');
	const m = Object.fromEntries(metrics.map((x) => [x.name, x.value]));
	const ms = (k) => Math.round((m[k] || 0) * 1000);
	return { task: ms('TaskDuration'), script: ms('ScriptDuration'), layout: ms('LayoutDuration'), style: ms('RecalcStyleDuration') };
}

function cpuDelta(a, b) {
	// A reload can start a fresh renderer; then the counters restart from zero.
	if (b.task < a.task) return b;
	return { task: b.task - a.task, script: b.script - a.script, layout: b.layout - a.layout, style: b.style - a.style };
}

/** First frame at which the sidebar card count reached its final (settled) value. */
export function sidebarFull(cards) {
	if (!cards || !cards.length) return null;
	const final = cards[cards.length - 1][1];
	if (final === 0) return null;
	const hit = cards.find(([, n]) => n >= final);
	return hit ? hit[0] : null;
}

function usableOf(r, keys) {
	const marks = { ...r.marks, sidebarFull: sidebarFull(r.cards) };
	r.marks = marks;
	return keys.every((k) => marks[k] != null) ? Math.max(...keys.map((k) => marks[k])) : null;
}

async function launchRun(browser, env, cell, page0) {
	// cold: fresh context (empty HTTP cache, empty sessionStorage) + deep link or landing
	// warm: reload of the same tab (immutable assets cached, tab's chat restored)
	let ctx, page, quiet, cdp, c0;
	if (page0) {
		({ page, quiet, cdp } = page0);
		c0 = await cpuNow(cdp);
		await page.reload({ waitUntil: 'commit' });
	} else {
		({ ctx, page, quiet, cdp } = await newPage(browser, env.ctxOpts, { route: env.route }));
		c0 = await cpuNow(cdp);
		await page.goto(env.base + cell.path, { waitUntil: 'commit' });
	}
	const live = cell.keys.map((k) => (k === 'sidebarFull' ? 'sidebar' : k));
	await waitFor(page, (keys) => keys.every((k) => window.__perf && window.__perf.marks[k] != null), live);
	const cpu = cpuDelta(c0, await cpuNow(cdp));
	await quiet(800, env.settleMs + 6000);
	const r = await collect(page);
	r.usable = usableOf(r, cell.keys);
	r.sidebarCards = r.cards.length ? r.cards[r.cards.length - 1][1] : 0;
	r.cpu = cpu; // main-thread time until the last live milestone was observed (+<=50 ms poll)
	const feed = r.feeds.find((f) => /from_seq=0/.test(f.url));
	r.feed = feed ? { start: feed.start, headers: feed.headers, first: feed.first, caughtUp: feed.caughtUp, bytes: feed.bytes, chunks: feed.chunks } : null;
	if (ctx) await ctx.close();
	return r;
}

async function openRun(browser, env, target, warmPage) {
	let ctx, page, quiet, cdp;
	if (warmPage) {
		({ page, quiet, cdp } = warmPage);
	} else {
		({ ctx, page, quiet, cdp } = await newPage(browser, env.ctxOpts, { route: env.route }));
		await page.goto(env.base + '/', { waitUntil: 'commit' });
		await waitFor(page, () => window.__perf && window.__perf.marks.assistant != null || (window.__perf && window.__perf.marks.composer != null));
		await quiet(1000, 12000);
	}
	const c0 = await cpuNow(cdp);
	await page.evaluate((id) => window.__perf.armOpen(id), target);
	await waitFor(page, () => window.__perf.open && window.__perf.open.marks.transcript != null);
	const cpu = cpuDelta(c0, await cpuNow(cdp));
	await quiet(800, 8000);
	await sleep(400);
	const r = await collect(page);
	r.usable = r.open.marks.transcript - r.open.t0;
	r.skeleton = r.open.marks.skeleton != null ? r.open.marks.skeleton - r.open.t0 : null;
	r.settled = r.open.lastChange ? r.open.lastChange - r.open.t0 : null;
	r.t0 = r.open.t0;
	r.cpu = cpu;
	const feed = r.feeds.find((f) => f.start >= r.t0 - 5 && /from_seq=0/.test(f.url));
	const rel = (x) => (x == null ? null : x - r.t0);
	r.feed = feed ? { start: rel(feed.start), headers: rel(feed.headers), first: rel(feed.first), caughtUp: rel(feed.caughtUp), bytes: feed.bytes, chunks: feed.chunks } : null;
	if (ctx) await ctx.close();
	return r;
}

// ── reporting ────────────────────────────────────────────────────────────────
function criticalPath(runs, origin) {
	// Aggregate resources that STARTED after the journey origin and ENDED before
	// its milestone in at least one run, plus everything up to milestone+2s.
	const agg = new Map();
	for (const r of runs) {
		const t0 = r.t0 ?? 0;
		const end = (r.t0 ?? 0) + r.usable;
		const seen = new Map();
		for (const e of r.res) {
			if (e.s < t0) continue;
			if (e.s > end + 2000) continue;
			const k = normalizePath(e.u);
			const x = seen.get(k) || { count: 0, s: Infinity, e: 0, dur: 0, tb: 0, eb: 0, before: 0 };
			x.count++;
			x.s = Math.min(x.s, e.s - t0);
			x.e = Math.max(x.e, e.e - t0);
			x.dur = Math.max(x.dur, e.e - e.s);
			x.tb += e.tb;
			x.eb += e.eb;
			if (e.e <= end) x.before++;
			seen.set(k, x);
		}
		for (const [k, x] of seen) {
			const a = agg.get(k) || { runs: 0, count: [], s: [], e: [], dur: [], tb: [], eb: [], before: 0 };
			a.runs++;
			a.count.push(x.count);
			a.s.push(x.s);
			a.e.push(x.e);
			a.dur.push(x.dur);
			a.tb.push(x.tb);
			a.eb.push(x.eb);
			if (x.before === x.count) a.before++;
			agg.set(k, a);
		}
	}
	return [...agg.entries()].map(([k, a]) => ({
		path: k,
		inRuns: a.runs,
		perRun: median(a.count),
		start: median(a.s),
		end: median(a.e),
		maxDur: median(a.dur),
		transferBytes: median(a.tb),
		bodyBytes: median(a.eb),
		endedBeforeMilestone: `${a.before}/${a.runs}`
	})).sort((x, y) => x.start - y.start);
}

function longTaskSummary(runs) {
	const before = runs.map((r) => r.longtasks.filter(([s]) => s >= (r.t0 ?? 0) && s < (r.t0 ?? 0) + r.usable));
	return {
		countBefore: summarize(before.map((l) => l.length)),
		msBefore: summarize(before.map((l) => l.reduce((a, [, d]) => a + d, 0))),
		longest: summarize(before.map((l) => Math.max(0, ...l.map(([, d]) => d))))
	};
}

function fmt(s) {
	return s.n ? `p75 ${s.p75} (min ${s.min}, max ${s.max}, n=${s.n})` : 'n/a';
}

// ── main ─────────────────────────────────────────────────────────────────────
async function main() {
	const args = parseArgs(process.argv.slice(2));
	const access = process.env.PERF_ACCESS_URL;
	if (!access) throw new Error('set PERF_ACCESS_URL (see the header of this file)');
	const base = new URL(access).origin;
	const { chromium } = loadPlaywright();
	const browser = await chromium.launch({
		executablePath: process.env.PERF_CHROME || (fs.existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined),
		args: ['--no-sandbox', '--disable-dev-shm-usage']
	});
	const meta = {
		date: new Date().toISOString(),
		label: args.label,
		origin: base,
		viewport: args.viewport,
		runs: args.runs,
		warmup: args.warmup,
		browser: browser.version(),
		host: { cpus: os.cpus().length, model: os.cpus()[0]?.model, loadavg: os.loadavg().map((x) => +x.toFixed(2)) }
	};

	// Sign in once; keep the storage state in memory only.
	const setupCtx = await browser.newContext(contextOptions(args.viewport));
	const sp = await setupCtx.newPage();
	await sp.goto(access, { waitUntil: 'load' });
	await sp.waitForFunction((SEL) => !!document.querySelector(`${SEL.sidebarRow}, ${SEL.composer}`) ||
		[...document.querySelectorAll('button')].some((b) => /Open conversation/.test(b.textContent || '')), SEL, { timeout: 45000 });
	await sp.waitForTimeout(2500);
	const api = async (p) => sp.evaluate(async (p) => {
		const r = await fetch(p, { credentials: 'include' });
		const t = await r.text();
		return { status: r.status, bytes: t.length, body: t.length < 200000 ? t : null };
	}, p);
	const ver = await api('/api/v1/version');
	meta.revision = ver.body ? JSON.parse(ver.body).api_version : null;
	const sizes = {};
	for (const [k, p] of [
		['sessionsActive', '/api/v1/sessions/cursor?limit=100&admin_only=true&exclude_archived=true&active_only=true'],
		['sessionsRecent', '/api/v1/sessions/cursor?limit=100&admin_only=true&exclude_archived=true'],
		['projects', '/api/v1/projects'],
		['clusterProjects', '/api/v1/cluster/projects'],
		['preferences', '/api/v1/preferences/all']
	]) {
		const r = await api(p);
		let items = null;
		try {
			const j = JSON.parse(r.body);
			items = Array.isArray(j) ? j.length : Array.isArray(j?.items) ? j.items.length : Array.isArray(j?.sessions) ? j.sessions.length : null;
		} catch { /* not JSON or too large */ }
		sizes[k] = { bytes: r.bytes, items };
	}
	for (const [k, id] of [['small', args.small], ['large', args.large], ['other', args.other]]) {
		if (!id) continue;
		const r = await sp.evaluate(async (id) => {
			const res = await fetch(`/api/v1/sessions/${id}`, { credentials: 'include' });
			const t = await res.text();
			const j = JSON.parse(t);
			const m = j.messages || [];
			const users = m.filter((x) => (x.type || x.role) === 'user').length;
			return { bytes: t.length, messages: m.length, userTurns: users, status: j.status, turnCount: j.turn_count };
		}, id);
		sizes[k] = { id: id.slice(0, 8), ...r };
	}
	meta.sizes = sizes;
	const storageState = await setupCtx.storageState();
	await setupCtx.close();
	console.error(`rig: origin ${base} revision ${meta.revision} viewport ${args.viewport} runs ${args.runs}+${args.warmup} warm-up`);

	const env = { base, ctxOpts: contextOptions(args.viewport, storageState), settleMs: args.settleMs, route: null };
	const journeys = args.journeys.split(',');
	const out = { meta, cells: {} };
	const mobile = Number(args.viewport.split('x')[0]) < 600;

	const runCell = async (name, fn) => {
		const runs = [];
		for (let i = 0; i < args.warmup + args.runs; i++) {
			try {
				const r = await fn(i);
				if (i >= args.warmup) runs.push(r);
				process.stderr.write(`${name} #${i}${i < args.warmup ? ' (warm-up)' : ''}: ${r.usable} ms\n`);
			} catch (e) {
				process.stderr.write(`${name} #${i}: FAILED ${e.message}\n`);
				if (i >= args.warmup) runs.push({ failed: e.message });
			}
		}
		const ok = runs.filter((r) => !r.failed);
		out.cells[name] = {
			usable: summarize(ok.map((r) => r.usable)),
			failed: runs.length - ok.length,
			raw: ok.map((r) => r.usable),
			runs: ok
		};
		console.error(`${name}: ${fmt(out.cells[name].usable)}`);
		return out.cells[name];
	};

	// Journey 1: launch
	if (journeys.includes('launch')) {
		const keysDeep = mobile ? ['composer'] : ['composer', 'sidebarFull'];
		if (args.small) {
			const cell = { path: `/?session=${args.small}`, keys: keysDeep };
			await runCell('launch.deeplink.cold', () => launchRun(browser, env, cell));
			const warm = await newPage(browser, env.ctxOpts);
			await warm.page.goto(base + cell.path, { waitUntil: 'commit' });
			await waitFor(warm.page, () => window.__perf && window.__perf.marks.composer != null);
			await warm.quiet(1000, 10000);
			await runCell('launch.reload.warm', () => launchRun(browser, env, cell, warm));
			await warm.ctx.close();
		}
		await runCell('launch.landing.cold', () => launchRun(browser, env, { path: '/', keys: mobile ? ['assistant'] : ['assistant', 'sidebarFull'] }));
	}

	// Journey 2: open an existing chat
	if (journeys.includes('open')) {
		for (const [label, id] of [['small', args.small], ['large', args.large]]) {
			if (!id) continue;
			await runCell(`open.${label}.cold`, () => openRun(browser, env, id));
			if (args.other) {
				const w = await newPage(browser, env.ctxOpts);
				await w.page.goto(base + '/', { waitUntil: 'commit' });
				await waitFor(w.page, () => window.__perf && (window.__perf.marks.assistant != null || window.__perf.marks.composer != null));
				await w.quiet(1000, 12000);
				await runCell(`open.${label}.warm`, async () => {
					await openRun(browser, env, args.other, w);
					return openRun(browser, env, id, w);
				});
				await w.ctx.close();
			}
		}
	}

	// Delay probe: which requests are on the critical path of a cell?
	if (args.probe) {
		const [j, variant, temp] = args.probe.split('.');
		if (temp !== 'cold') throw new Error('probe supports cold cells only');
		const run = j === 'launch'
			? (e) => launchRun(browser, e, variant === 'landing'
				? { path: '/', keys: mobile ? ['assistant'] : ['assistant', 'sidebarFull'] }
				: { path: `/?session=${args.small}`, keys: mobile ? ['composer'] : ['composer', 'sidebarFull'] })
			: (e) => openRun(browser, e, variant === 'large' ? args.large : args.small);
		const passthrough = async (route) => route.continue();
		const baseRuns = [];
		for (let i = 0; i < args.probeRuns + 1; i++) baseRuns.push(await run({ ...env, route: passthrough }));
		baseRuns.shift();
		const baseline = median(baseRuns.map((r) => r.usable));
		const candidates = new Set();
		for (const r of baseRuns) {
			const t0 = r.t0 ?? 0;
			for (const e of r.res) {
				if (e.s >= t0 && e.e <= t0 + r.usable && /\/api\//.test(e.u)) candidates.add(normalizePath(e.u));
			}
		}
		// The transcript replay stream is long-lived, so resource timing never
		// shows it as finished before the milestone; probe it explicitly.
		if (j === 'open' || variant === 'deeplink') candidates.add('/api/v1/sessions/:id/events?from_seq');
		const only = args.probePaths ? args.probePaths.split(',') : null;
		const results = [];
		for (const target of candidates) {
			if (only && !only.some((x) => target.includes(x))) continue;
			const delayed = async (route) => {
				if (normalizePath(route.request().url()) === target) await sleep(args.probeDelay);
				return route.continue();
			};
			const rs = [];
			for (let i = 0; i < args.probeRuns; i++) rs.push(await run({ ...env, route: delayed }).catch(() => null));
			const m = median(rs.filter(Boolean).map((r) => r.usable));
			const shift = m == null ? null : m - baseline;
			results.push({ path: target, median: m, shift, blocks: shift != null && shift >= args.probeDelay * 0.66 });
			console.error(`probe ${target}: shift ${shift} ms`);
		}
		out.probe = { cell: args.probe, delayMs: args.probeDelay, baseline, baselineRuns: baseRuns.map((r) => r.usable), results: results.sort((a, b) => b.shift - a.shift) };
	}

	// Journey 3: send (creates one throwaway chat in the Chat lane)
	if (journeys.includes('send') && args.sends > 0) {
		out.send = await sendJourney(browser, env, args);
	}

	await browser.close();

	// Summaries
	for (const [name, c] of Object.entries(out.cells)) {
		c.criticalPath = criticalPath(c.runs, 0);
		c.longTasks = longTaskSummary(c.runs);
		c.fcp = summarize(c.runs.map((r) => r.paints?.['first-contentful-paint']).filter((x) => x != null && !name.startsWith('open')));
		c.requestCount = summarize(c.runs.map((r) => r.res.filter((e) => e.s >= (r.t0 ?? 0) && e.e <= (r.t0 ?? 0) + r.usable).length));
		c.apiBeforeMilestone = summarize(c.runs.map((r) => r.res.filter((e) => /\/api\//.test(e.u) && e.s >= (r.t0 ?? 0) && e.e <= (r.t0 ?? 0) + r.usable).length));
		c.bytesBeforeMilestone = summarize(c.runs.map((r) => r.res.filter((e) => e.s >= (r.t0 ?? 0) && e.e <= (r.t0 ?? 0) + r.usable).reduce((a, e) => a + e.tb, 0)));
		for (const k of ['task', 'script', 'layout', 'style']) c['cpu_' + k] = summarize(c.runs.map((r) => r.cpu?.[k]));
		for (const k of ['start', 'headers', 'first', 'caughtUp', 'bytes']) c['feed_' + k] = summarize(c.runs.map((r) => r.feed?.[k]));
		if (name.startsWith('open')) {
			c.skeleton = summarize(c.runs.map((r) => r.skeleton));
			c.settled = summarize(c.runs.map((r) => r.settled));
			c.rows = summarize(c.runs.map((r) => r.open?.rows));
		} else {
			for (const k of ['composer', 'sidebar', 'sidebarFull', 'transcript', 'assistant']) c[k] = summarize(c.runs.map((r) => r.marks[k]));
			c.sidebarCards = summarize(c.runs.map((r) => r.sidebarCards));
			c.ttfb = summarize(c.runs.map((r) => r.nav?.ttfb));
			c.dcl = summarize(c.runs.map((r) => r.nav?.dcl));
		}
		// Drop bulky per-request detail from the JSON; keep per-run headline numbers.
		c.runs = c.runs.map((r) => ({
			usable: r.usable, cpu: r.cpu, feed: r.feed, feeds: r.feeds, marks: r.marks, cards: r.cards, open: r.open, nav: r.nav, paints: r.paints, lcp: r.lcp,
			longtasks: r.longtasks, measures: r.measures, dom: r.dom, skeleton: r.skeleton, settled: r.settled,
			res: r.res.map((e) => ({ ...e, u: normalizePath(e.u) }))
		}));
	}
	const json = JSON.stringify(out, null, 1);
	if (args.out) fs.writeFileSync(args.out, json);
	printTable(out);
}

function printTable(out) {
	const m = out.meta;
	console.log(`\n# ${m.date} revision ${m.revision} viewport ${m.viewport} N=${m.runs} (+${m.warmup} warm-up) ${m.browser} host load ${m.host.loadavg.join('/')}`);
	console.log(`data: ${JSON.stringify(m.sizes)}`);
	console.log('| cell | milestone p75 | min | max | n | FCP p75 | main-thread ms p75 (script/layout/style) | API reqs before | bytes before | long tasks before (ms p75) |');
	console.log('|---|---|---|---|---|---|---|---|---|---|');
	for (const [name, c] of Object.entries(out.cells)) {
		const u = c.usable;
		console.log(`| ${name} | ${u.p75} | ${u.min} | ${u.max} | ${u.n}${c.failed ? ` (${c.failed} failed)` : ''} | ${c.fcp?.p75 ?? '-'} | ${c.cpu_task.p75} (${c.cpu_script.p75}/${c.cpu_layout.p75}/${c.cpu_style.p75}) | ${c.apiBeforeMilestone.p75} | ${c.bytesBeforeMilestone.p75} | ${c.longTasks.msBefore.p75} |`);
	}
	for (const [name, c] of Object.entries(out.cells)) {
		const f = (k) => c['feed_' + k]?.p75;
		const extra = name.startsWith('open')
			? `skeleton ${c.skeleton.p75}; rows ${c.rows.p75}`
			: `composer ${c.composer?.p75}; sidebar full ${c.sidebarFull?.p75} (${c.sidebarCards?.p75} cards); transcript ${c.transcript?.p75}`;
		console.log(`${name}: transcript feed p75 start ${f('start')} headers ${f('headers')} first byte ${f('first')} caught_up ${f('caughtUp')} bytes ${f('bytes')}; ${extra}`);
	}
	for (const [name, c] of Object.entries(out.cells)) {
		console.log(`\n## ${name}: raw ${JSON.stringify(c.raw)}`);
		console.log('| path | start | end | max dur | per run | transfer B | ended before milestone |');
		console.log('|---|---|---|---|---|---|---|');
		for (const r of c.criticalPath.filter((r) => !r.path.startsWith('static:') || r.maxDur > 150).slice(0, 60)) {
			console.log(`| ${r.path} | ${r.start} | ${r.end} | ${r.maxDur} | ${r.perRun} | ${r.transferBytes} | ${r.endedBeforeMilestone} |`);
		}
	}
	if (out.probe) {
		console.log(`\n## probe ${out.probe.cell} (+${out.probe.delayMs} ms per path; baseline median ${out.probe.baseline})`);
		for (const r of out.probe.results) console.log(`| ${r.path} | shift ${r.shift} | ${r.blocks ? 'BLOCKS' : ''} |`);
	}
	if (out.send) console.log(`\n## send\n${JSON.stringify(out.send.summary, null, 1)}`);
}

// ── Journey 3 ────────────────────────────────────────────────────────────────
async function sendJourney(browser, env, args) {
	const { ctx, page } = await newPage(browser, env.ctxOpts);
	// Chat lane, cheapest model, low effort, no project (no worktree is created).
	await ctx.addInitScript((model) => {
		if (sessionStorage.getItem('__perf_seeded')) return;
		sessionStorage.setItem('__perf_seeded', '1');
		localStorage.setItem('ai-shuffle.sessionMode', 'chat');
		if (model) {
			localStorage.setItem('ai-shuffle-composer:model', JSON.stringify(model));
			localStorage.setItem('aihub_lastModel', model);
		}
		localStorage.setItem('ai-shuffle-composer:effort', JSON.stringify('low'));
		localStorage.setItem('aihub_lastEffort', 'low');
	}, args.model);
	if (args.sendSession) {
		// Follow-ups into an existing (throwaway) chat instead of a new one.
		await page.goto(`${env.base}/?session=${encodeURIComponent(args.sendSession)}`, { waitUntil: 'commit' });
		await page.waitForSelector(SEL.composer, { timeout: 30000 });
		await sleep(3000);
	} else {
		await page.goto(env.base + '/', { waitUntil: 'commit' });
		await waitFor(page, () => window.__perf && (window.__perf.marks.assistant != null || window.__perf.marks.composer != null));
		await sleep(2000);
		await page.click('nav.rail button.r-new');
		await page.waitForSelector(SEL.composer, { timeout: 15000 });
	}
	await sleep(1500);
	const sends = [];
	for (let i = 0; i < args.sends; i++) {
		await page.evaluate(() => window.__perf.armSend());
		await page.click(SEL.composer);
		await page.keyboard.type(args.prompt, { delay: 0 });
		await page.keyboard.press('Enter');
		try {
			await waitFor(page, () => window.__perf.send && window.__perf.send.done, null, 120000);
		} catch { /* record what we have */ }
		const r = await collect(page);
		const t0 = r.send.t0;
		const after = t0 == null ? [] : r.ws.filter(([t]) => t >= t0);
		const firstOut = after.find(([, dir, , type]) => dir === 'out' && type === 'query');
		const firstIn = after.find(([t, dir, url]) => dir === 'in' && url.includes('/ws/') && (!firstOut || t >= firstOut[0]));
		const firstDelta = after.find(([, dir, , type]) => dir === 'in' && /delta|chunk|text/.test(type));
		const rel = (x) => (x == null || t0 == null ? null : x - t0);
		const sessionId = await page.evaluate(() => {
			const fromUrl = new URL(location.href).searchParams.get('session');
			if (fromUrl && /^[0-9a-f-]{36}$/.test(fromUrl)) return fromUrl;
			try {
				for (let k = 0; k < sessionStorage.length; k++) {
					const v = sessionStorage.getItem(sessionStorage.key(k)) || '';
					const m = /"sessionId"\s*:\s*"([0-9a-f-]{36})"/.exec(v);
					if (m) return m[1];
				}
			} catch { /* none */ }
			return null;
		});
		const rec = {
			i,
			sessionId,
			userEcho: rel(r.send.marks.userEcho),
			busy: rel(r.send.marks.busy),
			activityVisible: rel(r.send.marks.activityVisible),
			agentVisible: rel(r.send.marks.agentVisible),
			firstToken: rel(r.send.marks.firstToken),
			done: rel(r.send.marks.done),
			wsQueryOut: rel(firstOut?.[0]),
			wsFirstIn: rel(firstIn?.[0]),
			wsFirstInType: firstIn?.[3],
			wsFirstDelta: rel(firstDelta?.[0]),
			wsFirstDeltaType: firstDelta?.[3],
			wsTypes: [...new Set(after.filter(([, d]) => d === 'in').map(([t, , u, type]) => type))].slice(0, 40),
			wsTimeline: after.slice(0, 40).map(([t, d, u, type, n]) => [t - t0, d, u, type, n]),
			text: r.send.text,
			longtasksDuring: r.longtasks.filter(([s]) => s >= t0 && s <= t0 + (rel(r.send.marks.done) ?? 60000))
		};
		sends.push(rec);
		console.error(`send #${i}: ${JSON.stringify({ ...rec, wsTimeline: undefined, wsTypes: undefined })}`);
		await sleep(2500);
	}
	await ctx.close();
	const follow = sends.slice(1);
	const summary = {};
	for (const k of ['userEcho', 'busy', 'activityVisible', 'agentVisible', 'firstToken', 'done', 'wsQueryOut', 'wsFirstIn', 'wsFirstDelta']) {
		summary[k] = { first: sends[0]?.[k] ?? null, followUps: summarize(follow.map((s) => s[k])) };
	}
	return { sessionId: sends.find((s) => s.sessionId)?.sessionId ?? null, summary, sends };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch((e) => {
		console.error(e.stack || String(e));
		process.exit(1);
	});
}
