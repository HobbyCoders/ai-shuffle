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
import { SEL, initScript, collect, normalizePath, loadPlaywright } from './browser-helpers.mjs';
import { fileURLToPath } from 'node:url';
import { summarize, median } from './stats.mjs';

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

export { normalizePath } from './browser-helpers.mjs';

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
