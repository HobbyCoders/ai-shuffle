// Shared runner for UI journeys against a running dev instance
// (`python -m scripts.devloop up`). Guide: tests/journeys/README.md.
//
// A journey file exports nothing; it calls runJourney() with a name, the cases
// to run and, optionally, the anomalies it tolerates (each with a reason). The
// runner crosses every case with every variant (viewport x throttle), records
// each case's acting part with the flight recorder and fails on any anomaly
// the journey did not explicitly allow.
//
//   node tests/journeys/<name>.journey.mjs [--only=<substring>] [--list]
//
// Env:
//   DEVLOOP_STATUS     status JSON of the dev instance (default: the one
//                      `python -m scripts.devloop status` reads for this checkout)
//   CHROMIUM_PATH      Chromium binary (default /usr/bin/chromium)
//   PLAYWRIGHT_MODULE  playwright-core (default browser-mcp's)
//   JOURNEY_OUT        where timelines (and contact sheets with
//                      JOURNEY_SHEETS=1) are written (default $TMPDIR/journeys)
//   JOURNEY_ONLY       same as --only
//   JOURNEY_HEADED=1   watch it run
//   JOURNEY_CONSOLE    regex: page console lines that match are added to the
//                      report (diagnosis, e.g. JOURNEY_CONSOLE='feedController')
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { contactSheet, withRecording } from '../flight-recorder/record.mjs';

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const CHECKOUT = resolve(HERE, '../../..');

/** Viewports every journey runs at unless it narrows them. */
export const VIEWPORTS = {
	desktop: { viewport: { width: 1440, height: 900 } },
	phone: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }
};

/** Throttle profiles: CPU slowdown plus extra fake-AI latency a case may add to its prompt. */
export const THROTTLES = {
	fast: { cpu: 1, aiLatencyMs: 0 },
	slow: { cpu: 4, aiLatencyMs: 1500 }
};

/** Path of the dev loop's status JSON for this checkout (mirrors scripts/devloop default_status_path). */
export function devloopStatusPath() {
	if (process.env.DEVLOOP_STATUS) return process.env.DEVLOOP_STATUS;
	const digest = createHash('sha256').update(CHECKOUT).digest('hex').slice(0, 12);
	// status_directory(): $XDG_RUNTIME_DIR when set, else a per-uid temp dir.
	const directory = process.env.XDG_RUNTIME_DIR
		? join(process.env.XDG_RUNTIME_DIR, 'ai-shuffle-devloop')
		: join(tmpdir(), `ai-shuffle-devloop-${process.getuid()}`);
	return join(directory, `${digest}.json`);
}

export function readDevloopStatus() {
	const path = devloopStatusPath();
	let status;
	try {
		status = JSON.parse(readFileSync(path, 'utf8'));
	} catch (error) {
		throw new Error(`no dev instance status at ${path} (${error.code || error.message}). Start one: python -m scripts.devloop up`);
	}
	if (status.state !== 'ready') throw new Error(`dev instance is ${status.state}, not ready (${path})`);
	if (!status.access_link) throw new Error(`dev instance status has no access_link (${path})`);
	return status;
}

/**
 * The fake-AI tag a case puts in its prompt so the reply honours the variant's
 * latency: `[script:default latency_ms=1500]`, or '' when there is none.
 */
export function fakeAiTag(variant, scenario = 'default') {
	const ms = variant.throttle.aiLatencyMs;
	return ms ? `[script:${scenario} latency_ms=${ms}]` : scenario === 'default' ? '' : `[script:${scenario}]`;
}

function matches(rule, anomaly) {
	if (rule.kind && rule.kind !== anomaly.kind) return false;
	if (rule.status !== undefined && rule.status !== anomaly.status) return false;
	if (rule.url && !rule.url.test(anomaly.url || '')) return false;
	if (rule.message && !rule.message.test(anomaly.message || '')) return false;
	return true;
}

function parseArgs(argv) {
	const args = { only: process.env.JOURNEY_ONLY || '', list: false };
	for (const arg of argv) {
		if (arg.startsWith('--only=')) args.only = arg.slice(7);
		else if (arg === '--list') args.list = true;
	}
	return args;
}

/**
 * Run a journey.
 *
 * spec:
 *   name       journey name (output folder)
 *   cases      [{ name, setup?(ctx), act(ctx), viewports?, throttles? }]
 *              ctx = { page, variant, status, mark?, log }
 *              `setup` runs unrecorded (navigate, stage the composer);
 *              `act` runs inside withRecording and must end in a settled UI.
 *   allow      [{ kind, url?, status?, message?, reason }]: anomalies that are
 *              known environment noise (never UI behaviour). Each needs a reason.
 *   viewports  default Object.keys(VIEWPORTS)
 *   throttles  default Object.keys(THROTTLES)
 *   recorder   extra withRecording options
 */
export async function runJourney(spec) {
	const args = parseArgs(process.argv.slice(2));
	for (const rule of spec.allow || []) {
		if (!rule.reason) throw new Error(`allow rule ${JSON.stringify(rule)} needs a reason`);
	}
	const plan = [];
	for (const testCase of spec.cases) {
		for (const viewport of testCase.viewports || spec.viewports || Object.keys(VIEWPORTS)) {
			for (const throttle of testCase.throttles || spec.throttles || Object.keys(THROTTLES)) {
				const id = `${testCase.name}@${viewport}-${throttle}`;
				if (args.only && !id.includes(args.only)) continue;
				plan.push({ id, testCase, viewport, throttle });
			}
		}
	}
	if (args.list) {
		for (const item of plan) console.log(item.id);
		return;
	}
	if (!plan.length) throw new Error(`no case matches --only=${args.only}`);

	const status = readDevloopStatus();
	const out = join(process.env.JOURNEY_OUT || join(tmpdir(), 'journeys'), spec.name);
	mkdirSync(out, { recursive: true });
	const { chromium } = require(process.env.PLAYWRIGHT_MODULE || resolve(CHECKOUT, 'browser-mcp/node_modules/playwright-core'));
	const browser = await chromium.launch({
		headless: process.env.JOURNEY_HEADED !== '1',
		executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
		args: ['--no-sandbox']
	});

	// Open the one-time access link once; every case starts from its cookie.
	const bootstrap = await browser.newContext();
	const bootPage = await bootstrap.newPage();
	await bootPage.goto(status.access_link);
	await bootPage.waitForURL((url) => !url.pathname.includes('/preview-access'), { timeout: 60_000 });
	const cookies = await bootstrap.cookies();
	await bootstrap.close();

	const results = [];
	try {
		for (const item of plan) {
			const variant = { viewport: item.viewport, throttle: THROTTLES[item.throttle], throttleName: item.throttle };
			const context = await browser.newContext({ ...VIEWPORTS[item.viewport] });
			await context.addCookies(cookies);
			const page = await context.newPage();
			const lines = [];
			const log = (line) => lines.push(line);
			const started = Date.now();
			if (process.env.JOURNEY_CONSOLE) {
				const pattern = new RegExp(process.env.JOURNEY_CONSOLE);
				page.on('console', (message) => {
					const text = message.text();
					if (pattern.test(text)) log(`console.${message.type()} +${Date.now() - started}ms: ${text.slice(0, 300)}`);
				});
			}
			const result = { id: item.id, ok: false, anomalies: [], allowed: [], text: '', error: null };
			try {
				const ctx = { page, variant, status, log };
				if (item.testCase.setup) await item.testCase.setup(ctx);
				const recording = await withRecording(
					page,
					{ cpu: variant.throttle.cpu, keyframes: process.env.JOURNEY_SHEETS === '1', ...(spec.recorder || {}) },
					({ mark }) => item.testCase.act({ ...ctx, mark })
				);
				result.text = recording.text;
				for (const anomaly of recording.anomalies) {
					const rule = (spec.allow || []).find((r) => matches(r, anomaly));
					if (rule) result.allowed.push({ message: anomaly.message, reason: rule.reason });
					else result.anomalies.push(anomaly);
				}
				result.ok = result.anomalies.length === 0;
				if (process.env.JOURNEY_SHEETS === '1') {
					await contactSheet(recording.keyframes, join(out, `${item.id}.jpg`), { title: item.id }).catch(() => {});
				}
			} catch (error) {
				result.error = error.stack || String(error);
				result.text = error.flightRecording || '';
				await page.screenshot({ path: join(out, `${item.id}.error.png`) }).catch(() => {});
			} finally {
				await context.close();
			}
			result.ms = Date.now() - started;
			const report = [
				`# ${item.id}  ${result.ok ? 'PASS' : 'FAIL'}  (${result.ms}ms)`,
				...lines,
				result.error ? `error: ${result.error}` : '',
				result.text,
				result.allowed.length
					? `allowed (environment noise):\n${result.allowed.map((a) => `  - ${a.message}  [${a.reason}]`).join('\n')}`
					: ''
			]
				.filter(Boolean)
				.join('\n');
			writeFileSync(join(out, `${item.id}.txt`), report + '\n');
			console.log(report + '\n');
			results.push(result);
		}
	} finally {
		await browser.close();
	}

	const failed = results.filter((r) => !r.ok);
	console.log(`${spec.name}: ${results.length - failed.length}/${results.length} passed; timelines in ${out}`);
	for (const r of failed) {
		console.log(`  FAIL ${r.id}: ${r.error ? r.error.split('\n')[0] : r.anomalies.map((a) => a.message).join(' | ')}`);
	}
	process.exitCode = failed.length ? 1 : 0;
	return results;
}
