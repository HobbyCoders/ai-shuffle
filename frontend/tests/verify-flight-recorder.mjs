// Proof that the UI flight recorder tells a clean transition from the bugs it
// exists for, in a real Chromium, unthrottled and under 4x CPU throttling:
//   clean   -> zero anomalies
//   flicker -> reversal on surface (one frame of the new-chat surface)
//   remount -> remount of ChatInput
//   jump    -> layout_shift
// Serves tests/flight-recorder/fixture.html itself; needs no Vite and no
// frontend dependencies.
//
//   node tests/verify-flight-recorder.mjs            (from frontend/)
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp,
// or PLAYWRIGHT_MODULE) and Chrome/Chromium (CHROMIUM_PATH). SCREENSHOT_DIR
// saves a contact sheet per case; FLIGHT_RECORDER_VERBOSE=1 prints every timeline.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { withRecording, contactSheet } from './flight-recorder/record.mjs';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const shots = process.env.SCREENSHOT_DIR;
const verbose = Boolean(process.env.FLIGHT_RECORDER_VERBOSE);

const html = await readFile(new URL('./flight-recorder/fixture.html', import.meta.url), 'utf8');
const server = createServer((req, res) => {
	res.writeHead(req.url?.split('?')[0] === '/' ? 200 : 404, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
	res.end(req.url?.split('?')[0] === '/' ? html : 'not found');
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}/`;

const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});

// Each case: the anomaly kinds that must appear, and the only kinds allowed.
const CASES = {
	clean: { must: [], allowed: [] },
	flicker: { must: ['reversal(surface)'], allowed: ['reversal', 'remount'] },
	remount: { must: ['remount(ChatInput)'], allowed: ['remount'] },
	jump: { must: ['layout_shift'], allowed: ['layout_shift'] },
};
const label = (a) => (a.field ? `${a.kind}(${a.field})` : a.name ? `${a.kind}(${a.name})` : a.kind);
const timelines = {};
let passed = 0;

try {
	for (const cpu of [1, 4]) {
		for (const [name, expected] of Object.entries(CASES)) {
			const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
			const page = await context.newPage();
			const pageErrors = [];
			page.on('pageerror', (error) => pageErrors.push(error.message));
			await page.goto(url);
			const result = await withRecording(page, { cpu }, async ({ mark }) => {
				await mark(`click ${name}`);
				await page.click(`#${name}`);
				await page.waitForSelector(`body[data-done="${name}"]`, { timeout: 10_000 });
			});
			const kinds = result.anomalies.map(label);
			const id = `${name} @ ${cpu}x CPU`;
			timelines[id] = result.text;
			if (verbose) console.log(`\n--- ${id}\n${result.text}`);
			if (shots && result.keyframes.length) {
				await contactSheet(result.keyframes, join(shots, `flight-recorder-${name}-${cpu}x.jpg`), { title: id });
			}
			await context.close();
			assert.deepEqual(pageErrors, [], `[${id}] page errors`);
			for (const kind of expected.must) assert.ok(kinds.includes(kind), `[${id}] expected ${kind}, got ${JSON.stringify(kinds)}\n${result.text}`);
			for (const anomaly of result.anomalies) {
				assert.ok(expected.allowed.includes(anomaly.kind), `[${id}] unexpected ${label(anomaly)}: ${anomaly.message}\n${result.text}`);
			}
			if (name === 'clean') assert.equal(result.anomalies.length, 0, `[${id}] clean transition must have no anomalies\n${result.text}`);
			assert.ok(result.recording.stats.frames > 3, `[${id}] recorded ${result.recording.stats.frames} frames`);
			assert.equal(result.recording.stats.internalErrors, 0, `[${id}] recorder internal errors`);
			assert.ok(result.keyframes.length > 0, `[${id}] no keyframes`);
			console.log(`ok  ${id.padEnd(18)} ${kinds.length ? kinds.join(', ') : 'no anomalies'}  (${result.recording.stats.frames} frames, ${result.keyframes.length} keyframes, ${result.recording.stats.sampleMsAvg}ms/frame)`);
			passed += 1;
		}
	}
	if (!verbose) {
		for (const id of ['clean @ 1x CPU', 'flicker @ 1x CPU']) console.log(`\n--- ${id}\n${timelines[id]}`);
	}
	console.log(`\nflight recorder proof page: ${passed}/${Object.keys(CASES).length * 2} cases passed`);
} finally {
	await browser.close();
	server.close();
}
