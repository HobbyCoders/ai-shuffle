// Visual regression screenshots for the chat UI at the owner's real viewports.
//
// Many past fixes were small layout regressions found by hand (timeline
// spacing, autoscroll, composer collapse, Git panel missing on the Galaxy Z
// Fold 6 inner screen). This renders the existing standalone fixtures at
// desktop, phone and both Z Fold 6 screens, and compares each with a committed
// baseline. CI runs it on every pull request (ci-frontend.yml, "Visual
// regression") in the Playwright container pinned to browser-mcp's
// playwright-core, so the Chromium build and the fonts are the same on every
// run; the committed baselines are that container's renders.
//
//   cd frontend
//   node tests/visual-regression.mjs            # compare against baselines
//   node tests/visual-regression.mjs --strict   # also fail on a missing baseline (CI)
//   node tests/visual-regression.mjs --update   # accept the current renders
//   node tests/visual-regression.mjs git-panel  # only fixtures whose name matches
//
// Each fixture is served by its own tests/vite.<name>.config.js (they carry
// their own API stubs) on a private port, one at a time. Uses playwright-core
// (PLAYWRIGHT_MODULE, default this checkout's browser-mcp) and Chromium
// (CHROMIUM_PATH). Writes to VISUAL_OUT (default /tmp/ai-shuffle-visual):
// renders/<fixture>-<viewport>.png (every current render: on a CI failure these
// are exactly the files to copy into tests/visual-baselines/), diffs/ (changed
// pixels in red over the dimmed baseline) and report.txt. Renders on another
// machine differ slightly from CI's (fonts, GPU); refresh the committed
// baselines from a CI run (docs/ci/pr-checks.md, "Visual regression").
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { changedAreas, MIN_AREA, TOLERANCE as DEFAULT_TOLERANCE, verdict } from './visual-diff.mjs';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const checkoutPlaywright = path.resolve(here, '../../browser-mcp/node_modules/playwright-core');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
	|| (existsSync(checkoutPlaywright) ? checkoutPlaywright : '/opt/browser-mcp/node_modules/playwright-core'));

const frontend = path.resolve(here, '..');
const baselines = path.join(here, 'visual-baselines');
const out = process.env.VISUAL_OUT || '/tmp/ai-shuffle-visual';
const renders = path.join(out, 'renders');
const diffs = path.join(out, 'diffs');
const args = process.argv.slice(2);
const update = args.includes('--update');
const strict = args.includes('--strict');
const filters = args.filter((a) => !a.startsWith('--'));
// Share of pixels allowed to differ (antialiasing, caret blink) before failing,
// and the connected area of changed pixels that fails on its own (visual-diff.mjs).
const TOLERANCE = Number(process.env.VISUAL_TOLERANCE || DEFAULT_TOLERANCE);
const AREA = Number(process.env.VISUAL_MIN_AREA || MIN_AREA);

export const VIEWPORTS = [
	{ name: 'desktop', width: 1440, height: 900, mobile: false },
	{ name: 'phone', width: 412, height: 915, mobile: true },
	{ name: 'fold-inner', width: 884, height: 1104, mobile: true },
	{ name: 'fold-cover', width: 360, height: 882, mobile: true }
];

export const FIXTURES = [
	{ name: 'git-panel', page: '/tests/git-panel.html', mobileQuery: '?shell=mobile', open: '.git-btn.trigger', ready: '.git-panel' },
	{ name: 'turn-navigator', page: '/tests/turn-navigator.html' },
	{ name: 'side-panel', page: '/tests/side-panel.html' },
	{ name: 'composer-send-guard', page: '/tests/composer-send-guard.html' },
	{ name: 'usage-rows', page: '/tests/usage-rows.html', open: '.usage-indicator', ready: '.usage-popover' }
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startServer(name, port) {
	const child = spawn('npx', ['vite', 'dev', '--config', `tests/vite.${name}.config.js`, '--host', '127.0.0.1',
		'--port', String(port), '--strictPort'], { cwd: frontend, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
	// npx starts vite as a child: kill the whole process group, or vite keeps this script alive.
	child.stop = () => { try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill(); } };
	let log = '';
	child.stdout.on('data', (d) => (log += d));
	child.stderr.on('data', (d) => (log += d));
	for (let i = 0; i < 120; i++) {
		try {
			const res = await fetch(`http://127.0.0.1:${port}/`);
			if (res.status) return child;
		} catch { /* not up yet */ }
		if (child.exitCode !== null) throw new Error(`vite for ${name} exited:\n${log}`);
		await sleep(500);
	}
	child.stop();
	throw new Error(`vite for ${name} did not start:\n${log}`);
}

/**
 * Share of differing pixels and the largest connected area of them, from a
 * canvas comparison in the page (no image deps), plus a diff image: changed
 * pixels red over the dimmed baseline (over the larger of the two sizes when
 * the sizes differ, which counts as all changed).
 */
async function diffRatio(page, a, b) {
	const result = await page.evaluate(async ([left, right]) => {
		const load = (b64) => new Promise((resolve, reject) => {
			const img = new Image();
			img.onload = () => resolve(img);
			img.onerror = reject;
			img.src = `data:image/png;base64,${b64}`;
		});
		const [x, y] = await Promise.all([load(left), load(right)]);
		const width = Math.max(x.width, y.width), height = Math.max(x.height, y.height);
		const pixels = (img) => {
			const canvas = document.createElement('canvas');
			canvas.width = width;
			canvas.height = height;
			const ctx = canvas.getContext('2d');
			ctx.drawImage(img, 0, 0);
			return ctx.getImageData(0, 0, width, height).data;
		};
		const p = pixels(x), q = pixels(y);
		const canvas = document.createElement('canvas');
		canvas.width = width;
		canvas.height = height;
		const ctx = canvas.getContext('2d');
		const image = ctx.createImageData(width, height);
		const mask = new Uint8Array(width * height);
		let differing = 0;
		for (let i = 0; i < p.length; i += 4) {
			const changed = Math.abs(p[i] - q[i]) > 8 || Math.abs(p[i + 1] - q[i + 1]) > 8 || Math.abs(p[i + 2] - q[i + 2]) > 8;
			if (changed) {
				differing++;
				mask[i / 4] = 1;
			}
			// Unchanged: the baseline as a light grey wash, enough to see where things are.
			const wash = 155 + ((p[i] + p[i + 1] + p[i + 2]) / 3) * 0.38;
			image.data[i] = changed ? 255 : wash;
			image.data[i + 1] = changed ? 0 : wash;
			image.data[i + 2] = changed ? 0 : wash;
			image.data[i + 3] = 255;
		}
		ctx.putImageData(image, 0, 0);
		const sameSize = x.width === y.width && x.height === y.height;
		let bytes = '';
		for (let i = 0; i < mask.length; i += 0x8000) bytes += String.fromCharCode(...mask.subarray(i, i + 0x8000));
		return { ratio: sameSize ? differing / (p.length / 4) : 1, width, height, mask: btoa(bytes),
			diff: canvas.toDataURL('image/png').split(',')[1] };
	}, [a.toString('base64'), b.toString('base64')]);
	const { changed, largest } = changedAreas(Buffer.from(result.mask, 'base64'), result.width, result.height);
	return { ratio: result.ratio, changed, largest, diff: Buffer.from(result.diff, 'base64') };
}

async function main() {
	await mkdir(baselines, { recursive: true });
	await mkdir(renders, { recursive: true });
	await mkdir(diffs, { recursive: true });
	const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] });
	const results = [];
	let port = 5600;
	try {
		for (const fixture of FIXTURES.filter((f) => !filters.length || filters.some((x) => f.name.includes(x)))) {
			const server = await startServer(fixture.name, ++port);
			try {
				for (const vp of VIEWPORTS) {
					const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height }, isMobile: vp.mobile,
						hasTouch: vp.mobile, deviceScaleFactor: 1, reducedMotion: 'reduce' });
					await page.route((url) => url.pathname.startsWith('/api/'), (route) => route.abort());
					// Freeze Date so "reported 42m ago" and reset countdowns render the same every run.
					await page.clock.setFixedTime(new Date('2026-10-01T12:00:00Z'));
					await page.goto(`http://127.0.0.1:${port}${fixture.page}${vp.mobile && fixture.mobileQuery ? fixture.mobileQuery : ''}`);
					await page.waitForLoadState('networkidle');
					await page.addStyleTag({ content: '*{caret-color:transparent!important;animation:none!important;transition:none!important}' });
					// Open the surface under test (a closed popover/panel proves nothing).
					if (fixture.open) {
						const trigger = page.locator(fixture.open).first();
						await (vp.mobile ? trigger.tap() : trigger.click());
						if (fixture.ready) await page.locator(fixture.ready).first().waitFor({ timeout: 10000 });
					}
					// Web fonts settle before the shot, or a late swap reads as a change.
					await page.evaluate(() => document.fonts.ready);
					await sleep(300);
					const shot = await page.screenshot({ fullPage: true });
					const key = `${fixture.name}-${vp.name}.png`;
					const baseline = path.join(baselines, key);
					await writeFile(path.join(renders, key), shot);
					let status;
					if (update) {
						await writeFile(baseline, shot);
						status = 'updated';
					} else {
						const exists = await access(baseline).then(() => true, () => false);
						if (!exists) status = 'no baseline (run --update)';
						else {
							const { ratio, changed, largest, diff } = await diffRatio(page, await readFile(baseline), shot);
							const judged = verdict({ ratio, largest }, { tolerance: TOLERANCE, minArea: AREA });
							if (judged !== 'ok') {
								await writeFile(path.join(diffs, key), diff);
								status = `${judged} (render ${path.join(renders, key)}, diff ${path.join(diffs, key)})`;
							} else if (changed) {
								// Under both limits but not identical: shown in the job summary, so a
								// baseline drifting a few pixels at a time is visible before it fails.
								await writeFile(path.join(diffs, key), diff);
								status = `ok, ${changed} px differ (largest area ${largest} px)`;
							} else status = 'ok';
						}
					}
					results.push({ fixture: fixture.name, viewport: vp.name, status });
					await page.close();
				}
			} finally {
				server.stop();
			}
		}
	} finally {
		await browser.close();
	}
	const failed = (r) => r.status.startsWith('CHANGED') || (strict && r.status.startsWith('no baseline'));
	const lines = results.map((r) => `${r.status.startsWith('CHANGED') || r.status.startsWith('no') ? '✗' : '✓'} ${r.fixture} @ ${r.viewport}: ${r.status}`);
	for (const line of lines) console.log(line);
	await writeFile(path.join(out, 'report.txt'), lines.join('\n') + '\n');
	if (!results.length) {
		console.log(`no fixture matches ${filters.join(' ')}`);
		process.exitCode = 1;
	}
	if (results.some(failed)) {
		console.log(`${results.filter(failed).length} of ${results.length} renders differ from or lack a baseline; renders in ${renders}, diffs in ${diffs}`);
		process.exitCode = 1;
	}
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	await main();
	process.exit(process.exitCode ?? 0);
}
