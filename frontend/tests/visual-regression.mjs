// Visual regression screenshots for the chat UI at the owner's real viewports.
//
// Many past fixes were small layout regressions found by hand (timeline
// spacing, autoscroll, composer collapse, Git panel missing on the Galaxy Z
// Fold 6 inner screen). This renders the existing standalone fixtures at
// desktop, phone and both Z Fold 6 screens, and compares each with a committed
// baseline. Not a CI job (CI minutes cost money); run it before and after UI
// work:
//
//   cd frontend
//   node tests/visual-regression.mjs            # compare against baselines
//   node tests/visual-regression.mjs --update   # accept the current renders
//   node tests/visual-regression.mjs git-panel  # only fixtures whose name matches
//
// Each fixture is served by its own tests/vite.<name>.config.js (they carry
// their own API stubs) on a private port, one at a time. Uses playwright-core
// and Chromium from the browser runtime (override with PLAYWRIGHT_MODULE and
// CHROMIUM_PATH). Diffs and current renders go to VISUAL_OUT
// (default /tmp/ai-shuffle-visual).
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/opt/browser-mcp/node_modules/playwright-core');

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '..');
const baselines = path.join(here, 'visual-baselines');
const out = process.env.VISUAL_OUT || '/tmp/ai-shuffle-visual';
const args = process.argv.slice(2);
const update = args.includes('--update');
const filters = args.filter((a) => !a.startsWith('--'));
// Share of pixels allowed to differ (antialiasing, caret blink) before failing.
const TOLERANCE = Number(process.env.VISUAL_TOLERANCE || 0.002);

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

/** Share of differing pixels, computed in the page with a canvas (no image deps). */
async function diffRatio(page, a, b) {
	return page.evaluate(async ([left, right]) => {
		const load = (b64) => new Promise((resolve, reject) => {
			const img = new Image();
			img.onload = () => resolve(img);
			img.onerror = reject;
			img.src = `data:image/png;base64,${b64}`;
		});
		const [x, y] = await Promise.all([load(left), load(right)]);
		if (x.width !== y.width || x.height !== y.height) return 1;
		const pixels = (img) => {
			const canvas = document.createElement('canvas');
			canvas.width = img.width;
			canvas.height = img.height;
			const ctx = canvas.getContext('2d');
			ctx.drawImage(img, 0, 0);
			return ctx.getImageData(0, 0, img.width, img.height).data;
		};
		const p = pixels(x), q = pixels(y);
		let differing = 0;
		for (let i = 0; i < p.length; i += 4) {
			if (Math.abs(p[i] - q[i]) > 8 || Math.abs(p[i + 1] - q[i + 1]) > 8 || Math.abs(p[i + 2] - q[i + 2]) > 8) differing++;
		}
		return differing / (p.length / 4);
	}, [a.toString('base64'), b.toString('base64')]);
}

async function main() {
	await mkdir(baselines, { recursive: true });
	await mkdir(out, { recursive: true });
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
					await sleep(300);
					const shot = await page.screenshot({ fullPage: true });
					const key = `${fixture.name}-${vp.name}.png`;
					const baseline = path.join(baselines, key);
					await writeFile(path.join(out, key), shot);
					let status;
					if (update) {
						await writeFile(baseline, shot);
						status = 'updated';
					} else {
						const exists = await access(baseline).then(() => true, () => false);
						if (!exists) status = 'no baseline (run --update)';
						else {
							const ratio = await diffRatio(page, await readFile(baseline), shot);
							status = ratio <= TOLERANCE ? 'ok' : `CHANGED ${(ratio * 100).toFixed(2)}% (see ${path.join(out, key)})`;
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
	for (const r of results) console.log(`${r.status.startsWith('CHANGED') || r.status.startsWith('no') ? '✗' : '✓'} ${r.fixture} @ ${r.viewport}: ${r.status}`);
	if (results.some((r) => r.status.startsWith('CHANGED'))) process.exitCode = 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	await main();
	process.exit(process.exitCode ?? 0);
}
