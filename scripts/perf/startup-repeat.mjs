#!/usr/bin/env node
/** Cold app processes over one warm schema; owned preview only, never field. */
import fs from 'node:fs';
import os from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { authenticate, loadPlaywright, PLATFORMS, windowCounters, collect, validateOutput } from './browser-helpers.mjs';
import { newChatPage, coldOpen, CHAT_SEL } from './chat-journeys.mjs';
import { summarize } from './stats.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function freePort() {
	return new Promise((resolve, reject) => {
		const server = net.createServer(); server.once('error', reject);
		server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
	});
}

export async function main(argv = process.argv.slice(2)) {
	const args = { runs: 10, warmup: 1, out: '', platform: 'both', seed: 'long', session: '' };
	for (let i = 0; i < argv.length; i++) {
		const key = argv[i].replace(/^--/, '');
		if (!(key in args) || argv[i + 1] == null) throw new Error('Invalid startup flags');
		args[key] = typeof args[key] === 'number' ? Number(argv[++i]) : argv[++i];
	}
	if (!Number.isInteger(args.runs) || args.runs < 1 || !Number.isInteger(args.warmup) || args.warmup < 1 ||
		!args.out || !['long', 'none'].includes(args.seed) || !['both', 'desktop', 'mobile'].includes(args.platform)) throw new Error('Invalid startup flags');
	if (args.seed === 'long' && !/^[\w-]+$/.test(args.session)) throw new Error('--session must name the long seed chat');
	validateOutput(args.out);
	const port = await freePort(), { chromium } = loadPlaywright();
	const browser = await chromium.launch({ executablePath: process.env.PERF_CHROME || '/usr/bin/chromium',
		args: ['--no-sandbox', '--disable-dev-shm-usage'] });
	const result = { meta: { date: new Date().toISOString(), kind: 'lab', loadStart: os.loadavg(),
		cpu: os.cpus()[0]?.model, platforms: PLATFORMS, seed: args.seed, browser: browser.version(),
		setup: 'one disposable PostgreSQL; excluded schema/seed warmup; cold app processes, warm OS caches; unshaped loopback' }, cells: {} };
	let child, ended = false, exitCode = null, exitSignal = null, current = null, start = 0, access = null, crash = false, error = false;
	const save = () => {
		result.summary = Object.fromEntries(Object.entries(result.cells).map(([name, rows]) => [name,
			Object.fromEntries(['processSpawnMs', 'healthReadyMs', 'usableMs', 'sseBytes', 'fromSeqZeroReplays',
				'apiRequests', 'longTaskMs', 'liveDomNodes'].map((key) => [key, summarize(rows.filter((r) => !r.warmup).map((r) => r[key]))]))]));
		fs.writeFileSync(args.out, JSON.stringify(result, null, 2));
	};
	try {
		child = spawn(process.env.PERF_PYTHON || 'python', ['-m', 'scripts.perf.startup_preview', '--port', String(port), '--seed', args.seed],
			{ cwd: ROOT, stdio: ['pipe', 'pipe', 'pipe'] });
		child.once('error', () => { ended = true; crash = true; });
		child.once('exit', (code, signal) => { ended = true; exitCode = code; exitSignal = signal; });
		for (const stream of [child.stdout, child.stderr]) {
			let pending = ''; stream.setEncoding('utf8');
			stream.on('data', (text) => {
				pending += text;
				let end;
				while ((end = pending.indexOf('\n')) >= 0) {
					const line = pending.slice(0, end); pending = pending.slice(end + 1);
					if (line.startsWith('@PERF_APP_SPAWN@') && current) current.processSpawnMs = Math.round(performance.now() - start);
					if (line.startsWith('@PERF_APP_EXIT@')) crash = true;
					if (line.includes('Branch preview access (open once')) access = line.match(/https?:\/\/\S+/)?.[0] || null;
					// Do not print/store app logs: they contain the one-use access URL.
				}
			});
		}
		const platforms = args.platform === 'both' ? ['desktop', 'mobile'] : [args.platform];
		for (const [platformIndex, platform] of platforms.entries()) {
			result.cells[platform] = [];
			for (let run = 0; run < args.runs + (platformIndex === 0 ? args.warmup : 0); run++) {
				const first = platformIndex === 0 && run === 0;
				current = { warmup: platformIndex === 0 && run < args.warmup };
				start = performance.now(); access = null; crash = false;
				if (!first) child.stdin.write('restart\n');
				const deadline = start + 240000;
				while (!access && !ended && !crash && performance.now() < deadline) await delay(100);
				if (!access || ended || crash) throw new Error('Startup process failed before access/listener');
				// Access prints during lifespan BEFORE seeding/listening. Retry only
				// transport-not-ready, not an HTTP access denial, until the same deadline.
				let storage;
				while (!storage && !ended && !crash && performance.now() < deadline) {
					try { storage = await authenticate(browser, access); }
					catch (e) { if (String(e.message).includes('Access refused')) throw e; await delay(200); }
				}
				if (!storage || ended || crash) throw new Error('Listener did not become ready');
				const p = await newChatPage(browser, storage, platform);
				try {
					const health = await p.context.request.get(`http://127.0.0.1:${port}/health/ready`);
					if (health.status() !== 200) throw new Error('Readiness refused');
					current.healthReadyMs = Math.round(performance.now() - start);
					let counts;
					if (args.seed === 'long') {
						counts = await coldOpen(p, `http://127.0.0.1:${port}`, args.session);
					} else {
						await p.page.goto(`http://127.0.0.1:${port}${p.route}`, { waitUntil: 'domcontentloaded' });
						await p.page.locator(CHAT_SEL.composer).waitFor({ state: 'visible', timeout: 30000 });
						const sample = await collect(p.page);
						counts = windowCounters(sample, [], 0, await p.page.evaluate(() => performance.now()), p.apiRequests);
					}
					await p.page.locator(CHAT_SEL.composer).fill('startup input probe');
					if (await p.page.locator(CHAT_SEL.composer).textContent() !== 'startup input probe') throw new Error('Composer not typeable');
					Object.assign(current, counts, { usableMs: Math.round(performance.now() - start) });
					result.cells[platform].push(current);
					console.log(JSON.stringify({ platform, ...current })); save();
				} finally { await p.context.close(); }
			}
		}
	} catch { error = true; result.failed = true; console.error('Startup measurement failed (app logs suppressed)'); }
	finally {
		try {
			if (child && !ended) child.stdin.end('stop\n');
			const deadline = performance.now() + 45000;
			while (!ended && performance.now() < deadline) await delay(100);
			if (!ended) child.kill('SIGTERM');
			result.cleanedUp = ended && exitCode === 0 && exitSignal == null;
			result.previewExitCode = exitCode;
			result.previewExitSignal = exitSignal;
			result.meta.loadEnd = os.loadavg();
			if (!result.cleanedUp) { error = true; result.failed = true; }
			save();
		} finally { await browser.close(); }
	}
	if (error) process.exitCode = 1;
	return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch(() => { console.error('Invalid startup measurement configuration'); process.exitCode = 1; });
}
