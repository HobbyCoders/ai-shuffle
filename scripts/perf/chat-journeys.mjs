#!/usr/bin/env node
/** Read-only Phase-1 chat measurements. See docs/internal/perf.md. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { initScript, collect, loadPlaywright, windowCounters, PLATFORMS, authenticate } from './browser-helpers.mjs';
import { summarize } from './stats.mjs';
import { switchJourney } from './switch-journey.mjs';
import { scrollJourney } from './long-transcript-scroll-journey.mjs';

export const CHAT_SEL = {
	composer: '[role="textbox"][aria-label="Message"][contenteditable="true"]',
	sidebarRow: '[data-session-card]',
	log: '[role="log"][aria-label="Conversation messages"]',
	skeleton: '.sk-turn', rows: '.msg-virtualized'
};

// Serialized into Chromium, so this deliberately has no module dependencies.
export function chatInstrumentation(SEL) {
	const P = window.__perf;
	P.arm = (kind) => {
		const log = document.querySelector(SEL.log);
		P.j = { kind, t0: performance.now(), old: new WeakSet(log?.querySelectorAll(SEL.rows) || []),
			first: null, composer: null, bottom: null, usable: null, lastChange: 0, geometry: '',
			feedBytes: P.feeds.map((f) => f.bytes), surfaces: [],
			originalComposer: document.querySelector(SEL.composer) };
	};
	P.arm('cold');
	P.j.t0 = 0;
	const tick = () => {
		const j = P.j, now = performance.now();
		const log = document.querySelector(SEL.log), composer = document.querySelector(SEL.composer);
		const surface = log?.querySelector(SEL.skeleton) ? 'loading' : log?.querySelector(SEL.rows) ? 'messages' : 'empty';
		if (j.surfaces.at(-1)?.[1] !== surface) j.surfaces.push([Math.round(now - j.t0), surface]);
		const box = composer?.getBoundingClientRect();
		if (j.composer == null && box?.width > 0 && box?.height > 0 &&
			!composer.closest('[inert],[aria-hidden="true"]') && composer.getAttribute('aria-disabled') !== 'true') j.composer = now;
		if (log && surface === 'messages' && [...log.querySelectorAll(SEL.rows)].some((row) => !j.old.has(row))) {
			j.first ??= now;
			const geometry = [log.scrollHeight, log.scrollTop, log.clientHeight].join(':');
			if (geometry !== j.geometry) { j.geometry = geometry; j.lastChange = now; }
			if (log.scrollHeight - log.scrollTop - log.clientHeight <= 5) j.bottom ??= now;
			// Same Phase-1 milestone: first new rows + enabled composer + geometry
			// quiet for 120ms. The quiet observation is not added to the milestone.
			if (j.composer != null && now - j.lastChange >= 120) j.usable ??= Math.max(j.first, j.composer, j.lastChange);
		}
		requestAnimationFrame(tick);
	};
	requestAnimationFrame(tick);
}

export function parseArgs(argv) {
	const args = { journeys: 'cold,warm,switch,scroll', runs: 10, platform: 'both',
		small: '', large: '', other: '', remote: '', out: '', scrolls: 3, scrollMode: 'older', switches: 2, field: false };
	for (let i = 0; i < argv.length; i++) {
		const key = argv[i].replace(/^--/, '');
		if (!(key in args)) throw new Error(`Unknown flag --${key}`);
		if (key === 'field') { args.field = true; continue; }
		const value = argv[++i];
		if (value == null || value.startsWith('--')) throw new Error(`Missing value for --${key}`);
		args[key] = typeof args[key] === 'number' ? Number(value) : value;
	}
	for (const key of ['runs', 'scrolls', 'switches']) {
		if (!Number.isInteger(args[key]) || args[key] < 1) throw new Error(`--${key} must be a positive integer`);
	}
	if (!['both', ...Object.keys(PLATFORMS)].includes(args.platform)) throw new Error('platform must be desktop, mobile or both');
	if (!['older', 'alternate'].includes(args.scrollMode)) throw new Error('scrollMode must be older or alternate');
	const journeys = args.journeys.split(',');
	if (journeys.some((j) => !['cold', 'warm', 'switch', 'scroll', 'remote'].includes(j))) throw new Error('Unknown journey');
	if (!args.out) throw new Error('--out is required');
	if (journeys.some((j) => j !== 'remote') && !args.large) throw new Error('--large is required');
	if (journeys.some((j) => ['warm', 'switch'].includes(j)) && !args.small) throw new Error('--small is required');
	if (journeys.includes('remote') && (!args.remote || !args.small)) throw new Error('remote requires --remote and --small');
	// Session ids are selectors/URL components, never arbitrary expressions.
	for (const key of ['small', 'large', 'other', 'remote']) {
		for (const id of args[key].split(',').filter(Boolean)) {
			if (!/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error(`Invalid session id for --${key}`);
		}
	}
	return args;
}

export async function newChatPage(browser, storageState, platform) {
	const { route, cpu, ...options } = PLATFORMS[platform];
	const context = await browser.newContext({ ...options, storageState, serviceWorkers: 'block' });
	await context.addInitScript(initScript, CHAT_SEL);
	await context.addInitScript(chatInstrumentation, CHAT_SEL);
	const page = await context.newPage();
	const cdp = await context.newCDPSession(page);
	await cdp.send('Emulation.setCPUThrottlingRate', { rate: cpu });
	const p = { context, page, platform, route, apiRequests: 0, requestBaseline: 0, errors: 0 };
	page.on('request', (request) => { if (new URL(request.url()).pathname.startsWith('/api/')) p.apiRequests++; });
	page.on('pageerror', () => { p.errors++; });
	return p;
}

export async function arm(p, kind) {
	p.requestBaseline = p.apiRequests;
	await p.page.evaluate((kind) => window.__perf.arm(kind), kind);
}

export async function waitUsable(p) {
	await p.page.waitForFunction(() => window.__perf?.j?.usable != null, null, { timeout: 120000 });
}

export async function snapshot(p, override = {}) {
	// Counts intentionally include the fixed 200ms post-milestone observation.
	await p.page.waitForTimeout(200);
	const sample = await collect(p.page);
	const state = await p.page.evaluate((SEL) => {
		const j = window.__perf.j, log = document.querySelector(SEL.log);
		return { t0: j.t0, first: j.first, usable: j.usable, composer: j.composer, bottom: j.bottom,
			feedBytes: j.feedBytes, surfaces: j.surfaces, end: performance.now(),
			composerSame: !!j.originalComposer && j.originalComposer === document.querySelector(SEL.composer),
			mountedRows: log?.querySelectorAll(SEL.rows).length || 0,
			firstIndex: Number(log?.querySelector(SEL.rows)?.getAttribute('data-transcript-index')),
			bottomGap: log ? log.scrollHeight - log.scrollTop - log.clientHeight : null };
	}, CHAT_SEL);
	const t0 = override.t0 ?? state.t0;
	const result = { firstMs: Math.round(state.first - t0), usableMs: Math.round(state.usable - t0),
		composerMs: state.composer == null ? null : Math.round(state.composer - t0),
		bottomMs: state.bottom == null ? null : Math.round(state.bottom - t0),
		...windowCounters(sample, state.feedBytes, t0, state.end, p.apiRequests - p.requestBaseline),
		mountedRows: state.mountedRows, firstIndex: state.firstIndex, bottomGap: state.bottomGap,
		surfaces: state.surfaces, composerSame: state.composerSame, pageErrors: p.errors,
		observationMs: Math.round(state.end - t0), ...override };
	delete result.t0;
	return result;
}

export async function coldOpen(p, origin, id) {
	await p.page.goto(`${origin}${p.route}?session=${encodeURIComponent(id)}`, { waitUntil: 'commit' });
	await waitUsable(p);
	return snapshot(p);
}

export function summarizeCells(cells) {
	return Object.fromEntries(Object.entries(cells).map(([name, runs]) => [name, {
		failed: runs.filter((run) => run.failed).length,
		...Object.fromEntries(['firstMs', 'usableMs', 'sseBytes', 'fromSeqZeroReplays', 'apiRequests',
			'longTaskMs', 'liveDomNodes'].map((key) => [key, summarize(runs.map((r) => r[key]))]))
	}]));
}

export async function main(argv = process.argv.slice(2)) {
	const args = parseArgs(argv), access = process.env.PERF_ACCESS_URL;
	if (!access) throw new Error('Set PERF_ACCESS_URL in the environment');
	const origin = new URL(access).origin;
	const { chromium } = loadPlaywright();
	const browser = await chromium.launch({ executablePath: process.env.PERF_CHROME || '/usr/bin/chromium',
		args: ['--no-sandbox', '--disable-dev-shm-usage'] });
	const output = { meta: { instrumentation: 'chat-v1', date: new Date().toISOString(),
		revision: process.env.PERF_REVISION || null, kind: args.field ? 'field-read-only' : 'lab',
		browser: browser.version(), network: 'unshaped; report topology separately',
		cpu: os.cpus()[0]?.model, loadStart: os.loadavg(), platforms: PLATFORMS, runs: args.runs }, cells: {} };
	let failed = false;
	const save = () => { output.summary = summarizeCells(output.cells); fs.writeFileSync(args.out, JSON.stringify(output, null, 2)); };
	async function record(name, fn) {
		output.cells[name] ??= [];
		try { const row = await fn(); output.cells[name].push(row); console.log(JSON.stringify({ cell: name, ...row })); }
		catch { failed = true; output.cells[name].push({ failed: true }); console.error(`${name} FAILED (no URLs logged)`); }
		save();
	}
	try {
		const storage = await authenticate(browser, access);
		for (const platform of args.platform === 'both' ? ['desktop', 'mobile'] : [args.platform]) {
			for (let run = 0; run < args.runs; run++) {
				for (const journey of args.journeys.split(',')) {
					const p = await newChatPage(browser, storage, platform);
					try {
						if (journey === 'cold') {
							await record(`${platform}.cold.large`, () => coldOpen(p, origin, args.large));
						} else if (journey === 'warm') {
							await coldOpen(p, origin, args.large);
							await switchJourney(p, args.small, { sidebar: false });
							await record(`${platform}.warm.large`, () => switchJourney(p, args.large, { sidebar: false }));
						} else if (journey === 'switch') {
							await coldOpen(p, origin, args.small);
							await record(`${platform}.switch.unvisited`, () => switchJourney(p, args.large));
							if (args.other) await record(`${platform}.switch.unvisited.other`, () => switchJourney(p, args.other));
							for (let shift = 0; shift < args.switches; shift++) {
								const large = shift % 2 === 1;
								await record(`${platform}.switch.visited.${large ? 'large' : 'small'}`,
									() => switchJourney(p, large ? args.large : args.small));
							}
						} else if (journey === 'scroll') {
							await coldOpen(p, origin, args.large);
							for (let scroll = 0; scroll < args.scrolls; scroll++) {
								const direction = args.scrollMode === 'alternate' && scroll % 2 === 1 ? 'newer' : 'older';
								await record(`${platform}.scroll.${direction}`, () => scrollJourney(p, { direction }));
							}
						} else if (journey === 'remote') {
							for (const [index, id] of args.remote.split(',').entries()) {
								await record(`${platform}.remote.${index + 1}.cold`, () => coldOpen(p, origin, id));
								await switchJourney(p, args.small, { sidebar: false });
								await record(`${platform}.remote.${index + 1}.warm`, () => switchJourney(p, id, { sidebar: false }));
							}
						}
					} catch { failed = true; console.error(`${platform}.${journey} setup FAILED (no URLs logged)`); }
					finally { await p.context.close(); }
				}
			}
		}
	} finally {
		output.meta.loadEnd = os.loadavg(); save(); await browser.close();
	}
	if (failed) process.exitCode = 1;
	return output;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch(() => { console.error('Measurement failed; check flags, access and preview health (URLs suppressed)'); process.exitCode = 1; });
}
