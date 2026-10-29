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
// Every journey writes <JOURNEY_OUT>/<name>/summary.json (one entry per case)
// for run-all.mjs.
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

/**
 * Viewports a case can run at. `desktop` and `phone` are the default; the Fold
 * sizes (the same as tests/visual-regression.mjs) are opt-in per case
 * (`viewports: [...DEFAULT_VIEWPORTS, ...FOLD_VIEWPORTS]`).
 */
export const VIEWPORTS = {
	desktop: { viewport: { width: 1440, height: 900 } },
	phone: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
	'fold-cover': { viewport: { width: 360, height: 882 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
	'fold-inner': { viewport: { width: 884, height: 1104 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }
};
export const DEFAULT_VIEWPORTS = ['desktop', 'phone'];
export const FOLD_VIEWPORTS = ['fold-cover', 'fold-inner'];

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
	// A launcher killed before its teardown leaves a "ready" file behind.
	if (status.pid && !processAlive(status.pid)) {
		throw new Error(`dev instance status is stale: pid ${status.pid} is gone (${path}). Start one: python -m scripts.devloop up`);
	}
	return status;
}

function processAlive(pid) {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return error.code === 'EPERM';
	}
}

/**
 * The fake-AI tag a case puts in its prompt so the reply honours the variant's
 * latency: `[script:default latency_ms=1500]`, or '' when there is none.
 */
export function fakeAiTag(variant, scenario = 'default') {
	const ms = variant.throttle.aiLatencyMs;
	return ms ? `[script:${scenario} latency_ms=${ms}]` : scenario === 'default' ? '' : `[script:${scenario}]`;
}

/**
 * Delete the chats a case created (any chat newer than the case's start that
 * is not a seeded one), pass or fail. The sidebar lists only the newest chats,
 * so chats left behind push the seeded ones out of it and break later journeys.
 * Returns how many were deleted; never throws (a report line says why).
 */
async function deleteCaseChats(context, status, startedAt) {
	const seeded = Object.values(status.seed?.chats || {});
	const page = await context.newPage();
	try {
		await page.goto(new URL('/api/v1/auth/status', status.urls.ui).toString());
		return await page.evaluate(
			async ({ seeded, since }) => {
				let deleted = 0;
				for (let pass = 0; pass < 5; pass++) {
					const response = await fetch('/api/v1/sessions?limit=100');
					if (!response.ok) throw new Error(`list sessions: HTTP ${response.status}`);
					const doomed = (await response.json()).filter((r) => !seeded.includes(r.id) && Date.parse(r.created_at) >= since);
					if (!doomed.length) break;
					for (const row of doomed) {
						const del = await fetch(`/api/v1/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
						if (del.ok || del.status === 404) deleted += 1;
						else throw new Error(`delete ${row.id}: HTTP ${del.status}`);
					}
				}
				return deleted;
			},
			{ seeded, since: startedAt - 2000 }
		);
	} finally {
		await page.close().catch(() => {});
	}
}

/** Anomaly kinds that are the UI behaviour journeys judge: never allowed. */
const NEVER_ALLOWED = new Set(['reversal', 'remount']);

function checkAllowRule(rule) {
	if (!rule.reason) throw new Error(`allow rule ${JSON.stringify(rule)} needs a reason`);
	if (!rule.kind) throw new Error(`allow rule ${JSON.stringify(rule)} needs a kind (a rule for every kind would hide UI bugs)`);
	if (NEVER_ALLOWED.has(rule.kind)) throw new Error(`allow rule for ${rule.kind} refused: ${rule.kind} is what journeys exist to catch (${rule.reason})`);
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
 *   cases      [{ name, setup?(ctx), act(ctx), verify?(ctx), viewports?, throttles?,
 *                 allow?, recorder? }]
 *              ctx = { page, variant, status, mark?, log, recording? }
 *              `setup` runs unrecorded (navigate, stage the composer);
 *              `act` runs inside withRecording and must end in a settled UI;
 *              `verify` runs after it with `recording` (the recorder's stop()
 *              result: timeline, anomalies) for checks over every painted
 *              frame (lib/timeline.mjs). Throwing in any of them fails the case.
 *   allow      [{ kind, url?, status?, message?, reason }]: anomalies that are
 *              environment noise or a known app bug outside the journey's
 *              subject (`known bug #NNN`). Each needs a kind and a reason;
 *              `reversal` and `remount` are refused. A case's own `allow`
 *              adds to the journey's.
 *              A case may carry `knownBug: { issue, error, reason }`: it fails
 *              today because of that filed app bug, in the way `error` (a
 *              RegExp over the failure's first line) describes. That failure is
 *              reported as KNOWN BUG (not a gate failure) only when the
 *              recording up to the failure caught no un-allowed anomaly: a
 *              flicker before the known failure still fails the case. Any
 *              other failure still fails, and a pass fails too ("remove knownBug"), so the
 *              marker goes once the bug is fixed. A race that only sometimes
 *              shows sets `intermittent: true`: a pass is then reported as
 *              "did not reproduce" instead of failing; remove the marker when
 *              the issue is closed.
 *   viewports  default DEFAULT_VIEWPORTS
 *   throttles  default Object.keys(THROTTLES)
 *   recorder   extra withRecording options (a case's `recorder` adds to it;
 *              `selectors` are concatenated)
 */
export async function runJourney(spec) {
	const args = parseArgs(process.argv.slice(2));
	for (const rule of spec.allow || []) checkAllowRule(rule);
	for (const testCase of spec.cases) {
		for (const rule of testCase.allow || []) checkAllowRule(rule);
		const known = testCase.knownBug;
		if (known && (!/^\d+$/.test(String(known.issue)) || !(known.error instanceof RegExp) || !known.reason)) {
			throw new Error(`case ${testCase.name}: knownBug needs { issue: <number>, error: <RegExp>, reason }`);
		}
	}
	const plan = [];
	for (const testCase of spec.cases) {
		for (const viewport of testCase.viewports || spec.viewports || DEFAULT_VIEWPORTS) {
			if (!VIEWPORTS[viewport]) throw new Error(`unknown viewport ${viewport} in case ${testCase.name}`);
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
			const allow = [...(spec.allow || []), ...(item.testCase.allow || [])];
			const sortAnomalies = (anomalies) => {
				for (const anomaly of anomalies) {
					const rule = allow.find((r) => matches(r, anomaly));
					if (rule) result.allowed.push({ message: anomaly.message, reason: rule.reason });
					else result.anomalies.push(anomaly);
				}
			};
			try {
				const ctx = { page, variant, status, log };
				if (item.testCase.setup) await item.testCase.setup(ctx);
				const recorderOpts = { ...(spec.recorder || {}), ...(item.testCase.recorder || {}) };
				const selectors = [...(spec.recorder?.selectors || []), ...(item.testCase.recorder?.selectors || [])];
				if (selectors.length) recorderOpts.selectors = selectors;
				const recording = await withRecording(
					page,
					{ cpu: variant.throttle.cpu, keyframes: process.env.JOURNEY_SHEETS === '1', ...recorderOpts },
					({ mark }) => item.testCase.act({ ...ctx, mark })
				);
				result.text = recording.text;
				sortAnomalies(recording.anomalies);
				if (item.testCase.verify) await item.testCase.verify({ ...ctx, recording: recording.recording });
				result.ok = result.anomalies.length === 0;
				if (process.env.JOURNEY_SHEETS === '1') {
					await contactSheet(recording.keyframes, join(out, `${item.id}.jpg`), { title: item.id }).catch(() => {});
				}
			} catch (error) {
				result.error = error.stack || String(error);
				result.text = error.flightRecording || '';
				// A failed step still owes what it recorded before failing: a flicker
				// there is a gate failure even in a case marked as a known bug.
				sortAnomalies(error.flightAnomalies || []);
				await page.screenshot({ path: join(out, `${item.id}.error.png`) }).catch(() => {});
			} finally {
				const cleaned = await deleteCaseChats(context, status, started).catch((error) => `failed: ${error.message}`);
				if (typeof cleaned !== 'number') lines.push(`cleanup of this case's chats ${cleaned}`);
				await context.close();
			}
			result.ms = Date.now() - started;
			result.status = result.ok ? 'pass' : 'fail';
			const known = item.testCase.knownBug;
			if (known) {
				const firstLine = result.error
					? result.error.split('\n')[0]
					: result.anomalies.map((a) => a.message).join(' | ');
				if (!result.ok && result.anomalies.length === 0 && known.error.test(firstLine)) {
					result.status = 'known-bug';
					result.ok = true;
					result.knownBug = `#${known.issue}: ${known.reason}`;
				} else if (!result.ok && result.error && known.error.test(firstLine)) {
					// The known failure, but the recording caught more before it.
					result.error = `known bug #${known.issue} failed as expected, but the recording also caught: ${result.anomalies.map((a) => a.message).join(' | ')}\n${result.error}`;
				} else if (result.ok && known.intermittent) {
					result.knownBug = `#${known.issue} did not reproduce this time (intermittent): ${known.reason}`;
				} else if (result.ok) {
					result.status = 'fail';
					result.ok = false;
					result.error = `known bug #${known.issue} no longer reproduces here: remove this case's knownBug marker (and close the issue once fixed everywhere)`;
				}
			}
			const label = { pass: 'PASS', fail: 'FAIL', 'known-bug': `KNOWN BUG #${known?.issue}` }[result.status];
			const report = [
				`# ${item.id}  ${label}  (${result.ms}ms)`,
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
	writeFileSync(
		join(out, 'summary.json'),
		JSON.stringify(
			{
				journey: spec.name,
				finishedAt: new Date().toISOString(),
				cases: results.map((r) => ({
					id: r.id,
					ok: r.ok,
					status: r.status,
					knownBug: r.knownBug || null,
					ms: r.ms,
					error: r.error ? r.error.split('\n')[0] : null,
					anomalies: r.anomalies.map((a) => a.message),
					allowed: r.allowed
				}))
			},
			null,
			2
		) + '\n'
	);
	const knownBugs = results.filter((r) => r.status === 'known-bug');
	console.log(
		`${spec.name}: ${results.length - failed.length - knownBugs.length}/${results.length} passed` +
			(knownBugs.length ? `, ${knownBugs.length} known bug` : '') +
			`; timelines in ${out}`
	);
	for (const r of knownBugs) console.log(`  KNOWN BUG ${r.id}: ${r.knownBug}`);
	for (const r of failed) {
		console.log(`  FAIL ${r.id}: ${r.error ? r.error.split('\n')[0] : r.anomalies.map((a) => a.message).join(' | ')}`);
	}
	process.exitCode = failed.length ? 1 : 0;
	return results;
}
