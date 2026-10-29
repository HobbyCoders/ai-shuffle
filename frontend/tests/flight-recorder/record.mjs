// UI flight recorder for Playwright journeys (guide: docs/internal/flight-recorder.md).
//
//   import { withRecording, contactSheet } from './flight-recorder/record.mjs';
//   const { text, anomalies, keyframes } = await withRecording(page, { cpu: 4 }, async ({ mark }) => {
//     await mark('click New chat');
//     await page.getByRole('button', { name: 'New chat' }).click();
//     await page.getByRole('textbox').waitFor();
//   });
//   console.log(text);                       // one line per signature change
//   assert.deepEqual(anomalies, []);
//   await contactSheet(keyframes, '/tmp/new-chat.jpg');
//
// The recorder itself is browser-mcp/src/recorder/flight-recorder.js; this file
// only drives it over a dedicated CDP session (Chromium only), the same way the
// browser_record tool does. No npm dependencies of its own: callers bring the
// Playwright page (playwright-core from browser-mcp/node_modules, as
// visual-regression.mjs does).
import {
	applyThrottle,
	clearThrottle,
	contactSheetHtml,
	pickKeyframes,
	recorderSource,
	resumeScript,
	sheetColumns,
	startScreencast
} from '../../../browser-mcp/src/recorder/driver.js';

export { NETWORK_PROFILES } from '../../../browser-mcp/src/recorder/driver.js';

/**
 * Record while `actions({ mark })` runs.
 *
 * opts:
 *   cpu            CPU slowdown factor (Emulation.setCPUThrottlingRate), e.g. 4
 *   network        'slow-3g' | 'fast-3g' | '4g' (Network.emulateNetworkConditions)
 *   keyframes      collect screencast frames for keyframes (default true)
 *   maxKeyframes   cap (default 12)
 *   fields         record only these data-ui-<field> attributes
 *   selectors      extra fields: ['name=css', ...] recorded as match counts
 *   cyclic         fields allowed to go A -> B -> A (default loading, streaming + selectors)
 *   scope          fields naming what is on screen (default path, session; see analyze.js)
 *   settleMs       keep recording this long after actions resolve (default 250)
 *   maxDurationMs  recorder stops sampling after this long (default 120000)
 *
 * Returns { text, anomalies, keyframes, recording }. If `actions` throws, the
 * recording still stops, throttling is cleared, and the error carries the
 * timeline as error.flightRecording.
 */
export async function withRecording(page, opts = {}, actions = async () => {}) {
	const cdp = await page.context().newCDPSession(page);
	const recorderOpts = {
		...(opts.fields ? { fields: opts.fields } : {}),
		...(opts.selectors ? { selectors: opts.selectors } : {}),
		...(opts.cyclic ? { cyclic: opts.cyclic } : {}),
		...(opts.scope ? { scope: opts.scope } : {}),
		maxDurationMs: opts.maxDurationMs ?? 120_000
	};
	let throttle = {};
	let screencast;
	let init;
	let recording = null;
	let frames = [];
	let failure;
	try {
		throttle = await applyThrottle(cdp, { cpu: opts.cpu, network: opts.network });
		if (opts.keyframes !== false) screencast = await startScreencast(cdp);
		await page.evaluate(recorderSource());
		const started = await page.evaluate((o) => window.__flightRecorder.start({ ...o, restart: true }), recorderOpts);
		if (!started.ok) throw new Error(`flight recorder did not start: ${started.error}`);
		init = await page.addInitScript({
			content: resumeScript({ id: started.id, startEpoch: started.startedAt, opts: recorderOpts })
		});
		const mark = (label) =>
			page.evaluate((l) => (window.__flightRecorder ? window.__flightRecorder.mark(l) : null), String(label));
		try {
			await actions({ mark, page });
		} catch (error) {
			failure = error;
		}
		await page.waitForTimeout(opts.settleMs ?? 250);
		recording = await page.evaluate(() => (window.__flightRecorder ? window.__flightRecorder.stop() : null));
		if (screencast) frames = await screencast.stop();
	} finally {
		if (screencast) await screencast.stop().catch(() => {});
		await clearThrottle(cdp, throttle);
		await init?.dispose().catch(() => {});
		await cdp.detach().catch(() => {});
	}
	if (failure) {
		if (recording?.text) failure.flightRecording = recording.text;
		// What the recorder caught before the failure: a known bug must not hide it.
		if (Array.isArray(recording?.anomalies)) failure.flightAnomalies = recording.anomalies;
		throw failure;
	}
	if (!recording || recording.ok === false) {
		throw new Error('flight recording lost: the page has no recorder (a full navigation it could not resume on?)');
	}
	const keyframes = screencast ? pickKeyframes(recording, frames, { max: opts.maxKeyframes ?? 12 }) : [];
	Object.defineProperty(keyframes, 'context', { value: page.context(), enumerable: false });
	return { text: recording.text, anomalies: recording.anomalies, keyframes, recording };
}

/**
 * Render keyframes into one grid image: a generated HTML page screenshotted by
 * Playwright in a fresh tab of the recording's browser context (or
 * options.context). Returns outPath, or null when there are no keyframes.
 */
export async function contactSheet(keyframes, outPath, options = {}) {
	if (!keyframes?.length) return null;
	const context = options.context ?? keyframes.context;
	if (!context) throw new Error('contactSheet needs the keyframes from withRecording or options.context');
	const page = await context.newPage();
	try {
		await page.setViewportSize({ width: 1312, height: 800 });
		await page.setContent(
			contactSheetHtml(keyframes, { columns: sheetColumns(keyframes), title: options.title ?? 'flight recorder keyframes' }),
			{ waitUntil: 'load' }
		);
		await page.screenshot({ path: outPath, fullPage: true, type: /\.png$/i.test(outPath) ? 'png' : 'jpeg' });
	} finally {
		await page.close();
	}
	return outPath;
}
