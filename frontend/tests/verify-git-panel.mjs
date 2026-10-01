// Real-browser regression for the Git panel's inline editors (GitHeaderActions,
// GitActionOptionsDropdown, GitProgressPopover). Clicks are real input events,
// so Svelte flushes between listeners the way it does for a user; jsdom does
// not, which is how a panel that closed on every chevron/Back click shipped.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.git-panel.config.js --host 127.0.0.1 --port 5193 --strictPort
//   node tests/verify-git-panel.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and a Chrome/Chromium binary (CHROMIUM_PATH). GIT_PANEL_FIXTURE_URL overrides the URL.
import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : '/usr/bin/chromium'),
	headless: true,
	args: ['--no-sandbox', '--disable-gpu'],
});
const url = process.env.GIT_PANEL_FIXTURE_URL || 'http://127.0.0.1:5193/tests/git-panel.html';

const EDITORS = [
	{ scenario: 'dirty', chevron: 'Commit options', id: 'git-commit' },
	{ scenario: 'ahead', chevron: 'Push options', id: 'git-push' },
	{ scenario: 'behind', chevron: 'Pull options', id: 'git-pull' },
	{ scenario: 'ahead', chevron: 'Pull request options', id: 'git-pr' },
	{ scenario: 'dirty', chevron: 'Ship It options', id: 'git-shipit' },
];
const BACK = 'Back to Git actions';
let passed = 0;

async function harness(viewport, { touch, shell }) {
	const context = await browser.newContext({ viewport, isMobile: touch, hasTouch: touch, deviceScaleFactor: 1, serviceWorkers: 'block' });
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => route.abort());
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (e) => errors.push(e.message));
	page.on('response', (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });
	const name = `${viewport.width}x${viewport.height}${shell === 'mobile' ? ' mobile shell' : ''}`;
	const check = (condition, label, detail) => {
		assert.ok(condition, `[${name}] ${label}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
		passed++;
	};
	const press = async (locator) => { await (touch ? locator.tap() : locator.click()); await page.waitForTimeout(80); };
	const button = (label) => page.getByRole('button', { name: label, exact: true });
	const open = () => page.evaluate(() => window.gitPanelFixture.open);
	const fx = (fn, arg) => page.evaluate(fn, arg).then(() => page.waitForTimeout(80));
	const panelVisible = () => page.locator('.git-panel').isVisible();
	async function openPanel() {
		if (!(await panelVisible())) await press(page.locator('.git-btn.trigger'));
		check(await panelVisible(), 'trigger opens the panel');
	}
	async function openEditor({ chevron, id }) {
		await openPanel();
		await press(button(chevron));
		check((await open()) === id && (await panelVisible()), `${chevron} opens its editor and the panel stays open`, await open());
	}
	// Geometry probes run in the page against the live DOM.
	const layout = (selector) => page.evaluate((selector) => {
		const panel = document.querySelector('.git-panel');
		const el = document.querySelector(selector);
		const vw = document.documentElement.clientWidth;
		const outside = [];
		panel?.querySelectorAll('*').forEach((e) => {
			const b = e.getBoundingClientRect();
			if (b.width && (b.right > vw + 0.5 || b.left < -0.5)) outside.push(`${e.tagName}.${e.className}`.slice(0, 60));
		});
		return {
			inPanel: !!el && !!panel?.contains(el),
			position: el ? getComputedStyle(el).position : null,
			pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
			outside,
		};
	}, selector);
	const smallTargets = () => page.evaluate(() => {
		const out = [];
		if (!document.querySelector('.options-dropdown button')) out.push('no editor controls found');
		document.querySelectorAll('.options-dropdown :is(button, a, input, select, textarea, [role=switch])').forEach((e) => {
			const b = e.getBoundingClientRect();
			if (b.width && b.height < 43.5) out.push(`${(e.getAttribute('aria-label') || e.textContent || e.tagName).trim().slice(0, 30)} ${Math.round(b.height)}px`);
		});
		return out;
	});
	function checkInFlow(state, what) {
		check(state.inPanel, `${what} renders inside the panel`);
		check(state.position !== 'fixed', `${what} is in flow, not position fixed`, state.position);
		check(!state.pageOverflow && state.outside.length === 0, `${what} has no horizontal overflow`, state.outside);
	}

	async function mobileShellChecks() {
		check((await page.locator('html').getAttribute('data-platform')) === 'mobile', 'mobile shell sets data-platform');
		check((await page.locator('.git-actions.sheet-layout').count()) === 1, 'mobile shell uses the bottom sheet');
		for (const editor of EDITORS) {
			await fx((s) => window.gitPanelFixture.scenario(s), editor.scenario);
			await openEditor(editor);
			checkInFlow(await layout('.options-dropdown'), `${editor.chevron} editor`);
			await press(button(BACK));
			check((await open()) === 'git-panel', `${editor.chevron}: Back returns to the overview`, await open());
		}
		for (const scenario of ['dirty', 'openpr']) {
			await fx((s) => { window.gitPanelFixture.endOp(); window.gitPanelFixture.scenario(s); }, scenario);
			await fx(() => window.gitPanelFixture.startOp('running'));
			check((await open()) === 'git-panel', `${scenario}: starting an operation opens the panel`);
			checkInFlow(await layout('.git-progress'), `${scenario}: progress box`);
		}
	}

	async function panelChecks() {
		const sheet = (await page.locator('.git-actions.sheet-layout').count()) === 1;
		check(sheet === touch, touch ? 'touch viewport uses the bottom sheet' : 'desktop viewport uses the anchored panel');
		for (const editor of EDITORS) {
			const { chevron, id } = editor;
			await fx((s) => window.gitPanelFixture.scenario(s), editor.scenario);
			await openEditor(editor);
			checkInFlow(await layout('.options-dropdown'), `${chevron} editor`);
			if (touch) {
				const small = await smallTargets();
				check(small.length === 0, `${chevron}: touch targets are at least 44px tall`, small);
			}
			// The regression that matters most: a real click on Back swaps the view
			// out from under the window click listener and must not close the panel.
			await press(button(BACK));
			check((await open()) === 'git-panel' && (await panelVisible()), `${chevron}: Back returns to the overview without closing the panel`, await open());

			await press(button(chevron));
			check((await open()) === id, `${chevron}: reopens from the overview`, await open());
			await page.keyboard.press('Escape');
			await page.waitForTimeout(80);
			check((await open()) === 'git-panel', `${chevron}: Escape in the editor returns to the overview`, await open());
			await page.keyboard.press('Escape');
			await page.waitForTimeout(80);
			check((await open()) === null && !(await panelVisible()), `${chevron}: Escape on the overview closes the panel`, await open());

			// Busy: a streaming turn locks the fields but Back stays usable.
			await openEditor(editor);
			await fx(() => window.gitPanelFixture.setStreaming(true));
			check(await page.locator('.opt-fields').evaluate((fs) => fs.disabled), `${chevron}: busy state disables the fieldset`);
			check(await button(BACK).isEnabled(), `${chevron}: Back stays enabled while busy`);
			await press(button(BACK));
			check((await open()) === 'git-panel', `${chevron}: Back works while busy`, await open());
			await fx(() => window.gitPanelFixture.setStreaming(false));
			await press(page.locator('.gp-close'));
			check((await open()) === null, `${chevron}: close control closes the panel`, await open());
		}

		// A tall editor in a short window: the close control must survive scrolling.
		await page.setViewportSize({ width: viewport.width, height: 520 });
		await fx(() => window.gitPanelFixture.scenario('dirty'));
		await openEditor(EDITORS[4]);
		const scrolled = await page.locator('.git-panel').evaluate((panel) => {
			const tall = panel.scrollHeight > panel.clientHeight;
			panel.scrollTop = panel.scrollHeight;
			return tall;
		});
		check(scrolled, 'Ship It editor overflows a 520px-tall window, so the panel scrolls');
		await page.waitForTimeout(80);
		const hit = await page.locator('.gp-close').evaluate((close) => {
			const b = close.getBoundingClientRect();
			return close.contains(document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2));
		});
		check(hit, 'close control is the topmost element at its position after scrolling');
		await press(page.locator('.gp-close'));
		check((await open()) === null, 'close control closes the panel after scrolling', await open());
		await page.setViewportSize(viewport);

		// Outside click closes the panel (on the sheet layout this lands on the scrim).
		await openPanel();
		const outside = await page.locator('#fx-outside').boundingBox();
		if (touch) await page.touchscreen.tap(outside.x + 5, outside.y + 5);
		else await page.mouse.click(outside.x + 5, outside.y + 5);
		await page.waitForTimeout(80);
		check((await open()) === null && !(await panelVisible()), 'outside click closes the panel', await open());

		// Starting an operation opens the panel with the progress box in flow.
		await fx(() => window.gitPanelFixture.startOp('running'));
		check((await open()) === 'git-panel', 'starting an operation opens the panel', await open());
		checkInFlow(await layout('.git-progress'), 'progress box');
		check(await button('Commit options').isDisabled() && await button('Ship It options').isDisabled(), 'chevrons are disabled while an operation runs');

		// Dismissing a failed operation removes the feedback and keeps the panel open.
		await fx(() => window.gitPanelFixture.startOp('error'));
		await press(page.locator('.gp-dismiss'));
		check((await open()) === 'git-panel' && (await panelVisible()), 'Dismiss on a failed operation keeps the panel open', await open());
		check((await page.locator('.git-progress').count()) === 0, 'Dismiss removes the progress box');
		await fx(() => window.gitPanelFixture.endOp());

		check(!(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)), 'no horizontal page overflow');
	}

	try {
		await page.goto(shell === 'mobile' ? `${url}?shell=mobile` : url);
		await page.locator('.git-btn.trigger').waitFor();
		if (shell === 'mobile') await mobileShellChecks();
		else await panelChecks();
		check(errors.length === 0, 'no page errors', errors);
	} catch (error) {
		const shot = join(tmpdir(), `git-panel-${viewport.width}${shell === 'mobile' ? '-mobile' : ''}.png`);
		await page.screenshot({ path: shot }).catch(() => {});
		error.message += `\nScreenshot: ${shot}`;
		throw error;
	} finally {
		await context.close();
	}
	console.log(`${name}: ok`);
}

try {
	await harness({ width: 375, height: 812 }, { touch: true });
	await harness({ width: 768, height: 940 }, { touch: true });
	await harness({ width: 1280, height: 800 }, { touch: false });
	await harness({ width: 375, height: 812 }, { touch: true, shell: 'mobile' });
	await harness({ width: 768, height: 940 }, { touch: true, shell: 'mobile' });
	console.log(`Git panel: ${passed} checks passed`);
} catch (error) {
	console.error(`Git panel verification failed after ${passed} passing checks.\n${error.message}`);
	process.exitCode = 1;
} finally {
	await browser.close();
}
