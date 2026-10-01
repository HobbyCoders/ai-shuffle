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
	executablePath: process.env.CHROMIUM_PATH || {
		win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
		darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	}[process.platform] || '/usr/bin/chromium',
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
	// The focused control's accessible label, or where focus fell instead.
	const focused = () => page.evaluate(() => {
		const el = document.activeElement;
		if (!el || el === document.body) return 'body';
		return el.closest('.git-panel') ? (el.getAttribute('aria-label') || el.textContent.trim()) : `outside: ${el.tagName}`;
	});
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

	// #1615: a worktree whose status reads "not a repository" keeps a labelled
	// Git button and its panel (which hosts Swarm and Private access).
	async function checkoutChecks() {
		const label = page.locator('.git-btn.trigger .btn-label');
		const labelShown = () => label.evaluate((el) => getComputedStyle(el).display !== 'none');
		// Phones (<=500px sheet) show the icon alone by design (#1544).
		const compact = touch && viewport.width <= 500;
		for (const [scenario, detail] of [['checking', 'checking'], ['unreadable', 'unavailable']]) {
			await fx((s) => window.gitPanelFixture.scenario(s), scenario);
			check(await page.locator('.git-btn.trigger').isVisible(), `${scenario}: the Git button stays in the header`);
			check((await label.textContent()) === 'feature/inline-git-editors-with-a-long-name', `${scenario}: the button names the branch`, await label.textContent());
			check((await page.locator('.git-btn.trigger').textContent()).includes(detail), `${scenario}: the button says ${detail}`);
			check((await labelShown()) === !compact, `${scenario}: label ${compact ? 'hidden on a phone' : 'visible'}`);
			if (!compact) {
				const fits = await label.evaluate((el) => el.getBoundingClientRect().width <= 181 && el.getBoundingClientRect().right <= document.documentElement.clientWidth);
				check(fits, `${scenario}: a long branch name is truncated inside the viewport`);
			}
		}
		await openPanel();
		check((await page.locator('.git-panel .gp-rows').count()) === 0, 'unreadable: no action rows in the panel');
		const before = await page.evaluate(() => window.gitPanelRetries ?? 0);
		await press(button('Check again'));
		check((await page.evaluate(() => window.gitPanelRetries ?? 0)) === before + 1, 'unreadable: Check again retries the load');
		check((await open()) === 'git-panel', 'unreadable: Check again keeps the panel open', await open());
		check(await page.getByRole('button', { name: 'Private access' }).isEnabled(), 'unreadable: Private access stays reachable');
		await fx(() => window.gitPanelFixture.scenario('dirty'));
		check(await page.locator('.gp-rows').getByRole('button', { name: 'Commit', exact: true }).isEnabled(), 'recovered: Commit is back');
		await press(page.locator('.gp-close'));
		check((await open()) === null, 'recovered: close control closes the panel', await open());
	}

	async function mobileShellChecks() {
		check((await page.locator('html').getAttribute('data-platform')) === 'mobile', 'mobile shell sets data-platform');
		check((await page.locator('.git-actions.sheet-layout').count()) === 1, 'mobile shell uses the bottom sheet');
		await checkoutChecks();
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

	// A row click runs the handler with no option bag; an editor submit passes
	// the explicit bag. Both leave the panel on the overview.
	async function handlerChecks() {
		const SUBMITS = [
			{ row: 'Commit', chevron: 'Commit options', name: 'commit', keys: ['includeUnstaged', 'message', 'nextStep'] },
			{ row: 'Ship It', chevron: 'Ship It options', name: 'shipit', keys: ['cleanup', 'includeUnstaged', 'mergeMethod'] },
		];
		await fx(() => window.gitPanelFixture.scenario('dirty'));
		for (const { row, chevron, name: handler, keys } of SUBMITS) {
			const takeCalls = () => page.evaluate(() => window.gitPanelFixture.calls.splice(0).map((c) => ({
				name: c.name, argc: c.argc, keys: c.options && typeof c.options === 'object' ? Object.keys(c.options).sort() : null,
			})));
			await takeCalls();
			await openPanel();
			await press(page.locator('.gp-pipe, .gp-rows').getByRole('button', { name: row, exact: true }));
			const rowCalls = await takeCalls();
			check(rowCalls.length === 1 && rowCalls[0].name === handler && rowCalls[0].argc === 0, `${row} row click runs the handler with no option bag`, rowCalls);
			check((await open()) === 'git-panel' && (await page.locator('.gp-rows').isVisible()), `${row} row click leaves the panel on the overview`, await open());

			await press(button(chevron));
			await press(page.locator('.options-dropdown .opt-submit'));
			const submitCalls = await takeCalls();
			check(submitCalls.length === 1 && submitCalls[0].name === handler && submitCalls[0].argc === 1, `${row} editor submit runs the handler once with an option bag`, submitCalls);
			check(JSON.stringify(submitCalls[0]?.keys) === JSON.stringify(keys), `${row} editor submit passes the explicit options`, submitCalls[0]?.keys);
			check((await open()) === 'git-panel' && (await page.locator('.gp-rows').isVisible()), `${row} editor submit returns to the overview`, await open());
			check((await focused()) === chevron, `${row} editor submit returns focus to the chevron`, await focused());
			await press(page.locator('.gp-close'));
		}
	}

	async function panelChecks() {
		const sheet = (await page.locator('.git-actions.sheet-layout').count()) === 1;
		check(sheet === touch, touch ? 'touch viewport uses the bottom sheet' : 'desktop viewport uses the anchored panel');
		if (!touch) await handlerChecks();
		await checkoutChecks();
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
			check((await focused()) === chevron, `${chevron}: Back returns focus to the chevron`, await focused());

			await press(button(chevron));
			check((await open()) === id, `${chevron}: reopens from the overview`, await open());
			await page.keyboard.press('Escape');
			await page.waitForTimeout(80);
			check((await open()) === 'git-panel', `${chevron}: Escape in the editor returns to the overview`, await open());
			check((await focused()) === chevron, `${chevron}: Escape returns focus to the chevron`, await focused());
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
			const busyFocus = await focused();
			check(busyFocus !== 'body' && busyFocus !== chevron && !busyFocus.startsWith('outside'), `${chevron}: Back while busy skips the disabled chevron and stays in the panel`, busyFocus);
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

		// An operation started by a click outside the panel (a toast action) opens
		// the panel during that click; the same click must not close it again.
		await press(page.locator('.gp-close'));
		check((await open()) === null, 'close control closes the panel before the toast check', await open());
		await press(page.locator('#fx-toast'));
		check((await open()) === 'git-panel' && (await panelVisible()) && (await page.locator('.git-progress').isVisible()), 'an operation started from outside the panel keeps it open with progress', await open());
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
