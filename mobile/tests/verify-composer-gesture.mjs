import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu']
});
const baseUrl = process.env.COMPOSER_FIXTURE_URL || 'http://127.0.0.1:5181/assets/mobile-ui/tests/composer-gesture.html';
const artifacts = process.env.COMPOSER_ARTIFACTS || '/tmp/composer-gesture-artifacts';
await mkdir(artifacts, { recursive: true });

function fixtureUrl(params) {
	const url = new URL(baseUrl);
	for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
	return url.href;
}

async function harness(width, height) {
	const context = await browser.newContext({
		viewport: { width, height }, isMobile: true, hasTouch: true,
		deviceScaleFactor: 1, serviceWorkers: 'block'
	});
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', error => errors.push(error.message));
	// The fixture installs its own shape-correct fetch responses. This route is
	// only a final guard against accidental backend traffic outside that mock.
	await context.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({ json: [] }));
	const cdp = await context.newCDPSession(page);
	const isOpen = () => page.locator('.panel-reveal').getAttribute('aria-hidden').then(value => value === 'false');
	async function load(params) {
		// The shell is a build-time decision: this fixture is the mobile bundle's
		// composer, so the platform is always mobile whatever the viewport.
		await page.goto(fixtureUrl(params));
		await page.locator('.tiptap').waitFor();
		assert.equal(await page.locator('html').getAttribute('data-platform'), 'mobile');
	}
	async function swipe(selector, dy, { dx = 0, cancel = false, multitouch = false } = {}) {
		const target = page.locator(selector).first();
		await target.scrollIntoViewIfNeeded();
		const box = await target.boundingBox();
		assert.ok(box, selector);
		const x = box.x + Math.min(box.width / 2, 80), y = box.y + box.height / 2;
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }] });
		for (let step = 1; step <= 8; step++) {
			await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx * step / 8, y: y + dy * step / 8, id: 1 }] });
			if (multitouch && step === 2) {
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: y + dy / 4, id: 1 }, { x: x + 30, y: y + dy / 4, id: 2 }] });
				break;
			}
			await page.waitForTimeout(25);
		}
		await cdp.send('Input.dispatchTouchEvent', { type: cancel ? 'touchCancel' : 'touchEnd', touchPoints: [] });
		await page.waitForTimeout(350); // Measured panel reveal lasts 280ms.
	}
	async function setOpen(open) {
		if (await isOpen() !== open) await page.locator('.mobile-settings-trigger').tap();
		await page.waitForTimeout(350);
		assert.equal(await isOpen(), open);
	}
	async function traceTouches() {
		await page.evaluate(() => {
			window.fixtureTouchPrevented = false;
			if (!window.fixtureTouchTraceInstalled) {
				window.fixtureTouchTraceInstalled = true;
				window.addEventListener('touchmove', event => {
					if (event.defaultPrevented) window.fixtureTouchPrevented = true;
				}, { passive: true });
			}
		});
	}
	return { page, load, swipe, isOpen, setOpen, traceTouches, async close() { assert.deepEqual(errors, []); await context.close(); } };
}

async function verifyGestures(width, layout) {
	const h = await harness(width, 844);
	const { page, swipe, isOpen, setOpen } = h;
	const opening = layout === 'compact' ? -70 : 70;
	await h.load({ layout });
	for (const initiallyOpen of [false, true]) {
		assert.equal(await isOpen(), initiallyOpen);
		const direction = initiallyOpen ? -opening : opening;
		await swipe('.tiptap', -direction);
		assert.equal(await isOpen(), initiallyOpen, `${layout}: wrong direction is native`);
		await swipe('.tiptap', direction, { cancel: true });
		assert.equal(await isOpen(), initiallyOpen, `${layout}: cancelled swipe`);
		await swipe('.tiptap', direction, { multitouch: true });
		assert.equal(await isOpen(), initiallyOpen, `${layout}: multitouch swipe`);
		await swipe('.tiptap', Math.sign(direction) * 5, { dx: 85 });
		assert.equal(await isOpen(), initiallyOpen, `${layout}: horizontal swipe`);
		await swipe('.tiptap', direction);
		assert.equal(await isOpen(), !initiallyOpen, `${layout}: composer ${initiallyOpen ? 'closes' : 'opens'}`);
		assert.equal(await page.locator('.tiptap').evaluate(editor => editor.contains(document.activeElement)), false, 'swiping does not focus the editor');
		await page.screenshot({ path: `${artifacts}/${width}-${layout}-${initiallyOpen ? 'closed' : 'open'}.png` });
	}

	await page.locator('.tiptap').tap();
	assert.equal(await isOpen(), false, 'expanding tap never activates a reflowed toolbar control');
	await setOpen(true);
	await setOpen(false);
	await swipe('.controls-dock', opening);
	assert.equal(await isOpen(), true, 'expanded non-editor surface opens');
	await setOpen(false);

	const draft = Array.from({ length: 35 }, (_, index) => `Draft line ${index}: preserve native editor scrolling.`).join('\n');
	await page.locator('.tiptap').fill(draft);
	for (const open of [false, true]) {
		await setOpen(open);
		await page.locator('.tiptap').evaluate(editor => {
			let node = editor;
			while (node && !(node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY))) node = node.parentElement;
			if (!node) throw new Error('Long draft has no native scroll owner');
			node.scrollTop = 0;
			node.setAttribute('data-fixture-scroll', 'true');
		});
		await h.traceTouches();
		await swipe('[data-fixture-scroll]', -90);
		assert.equal(await isOpen(), open, 'draft scroll does not change sheet state');
		assert.equal(await page.evaluate(() => window.fixtureTouchPrevented), false, 'draft events remain native');
		assert.ok(await page.locator('[data-fixture-scroll]').evaluate(node => node.scrollTop) > 0, 'native draft scroll moved');
		assert.ok((await page.locator('.tiptap').innerText()).includes('Draft line 34'), 'draft preserved');
	}

	// Drawer swipes never change state, including when the list starts at the top.
	await page.locator('.model-control').tap();
	await page.locator('.mpp-list').waitFor();
	await h.traceTouches();
	await swipe('.mpp-list', 65);
	await swipe('.mpp-list', -65);
	assert.equal(await isOpen(), true, 'model list swipes do not dismiss');
	assert.equal(await page.evaluate(() => window.fixtureTouchPrevented), false, 'model list events remain native');
	await page.locator('.controls-panel').focus();
	await page.keyboard.press('Escape'); // Models -> overview.
	await page.locator('.controls-panel').evaluate(panel => { panel.scrollTop = 0; });
	await swipe('.model-control .control-label', 65);
	await swipe('.model-control .control-label', -65);
	assert.equal(await isOpen(), true, 'overview drawer swipes do not dismiss');
	await page.locator('.controls-panel').focus();
	await page.keyboard.press('Escape');
	assert.equal(await isOpen(), false, 'Escape still closes overview');
	assert.equal(await page.locator('.mobile-settings-trigger').evaluate(button => button === document.activeElement), true, 'closing restores panel-owned focus');
	console.log(`PASS ${width}px ${layout}: both composer states, wrong direction, cancellation, taps, native draft/model touches, Escape`);
	await h.close();
}

async function assertContextGeometry(page, label) {
	const measurements = await page.locator('.mobile-composer-controls .context-pill-bar').evaluate(root => {
		const box = selector => {
			const node = root.querySelector(selector);
			if (!node) throw new Error(`Missing context control: ${selector}`);
			const { left, right, top, bottom, width } = node.getBoundingClientRect();
			return { left, right, top, bottom, width };
		};
		return { repo: box('.workspace-selector'), branch: box('.branch-selector > button'), worktree: box('.wt-chip, .wt-indicator'), width: window.innerWidth, grid: getComputedStyle(root.querySelector('.branch-wt-row')).display === 'grid' };
	});
	const { repo, branch, worktree, width } = measurements;
	for (const [name, rect] of Object.entries({ repo, branch, worktree })) {
		assert.ok(rect.width > 0 && rect.left >= 0 && rect.right <= width + 1, `${label}: ${name} stays within viewport: ${JSON.stringify(measurements)}`);
	}
	assert.ok(repo.bottom <= branch.top + 1, `${label}: repository has its own row`);
	assert.ok(branch.right <= worktree.left + 1, `${label}: branch and worktree do not overlap`);
	if (measurements.grid) assert.ok(Math.abs(branch.top - worktree.top) <= 1, `${label}: grid keeps worktree aligned when the branch menu opens`);
}

async function verifyContext(width, height, layout, context = 'new') {
	const h = await harness(width, height);
	await h.load({ layout, mode: 'code', context });
	await h.swipe('.tiptap', layout === 'compact' ? -65 : 65);
	assert.equal(await h.isOpen(), true);
	const label = `${width}x${height}-${layout}-${context}`;
	for (const worktree of context === 'new' ? [true, false] : [false]) {
		if (context === 'new') {
			if (await h.page.locator('.mobile-composer-controls .wt-chip input').isChecked() !== worktree) {
				await h.page.locator('.mobile-composer-controls .wt-chip').tap();
			}
			await h.page.waitForTimeout(350);
		}
		await assertContextGeometry(h.page, label);
		await h.page.locator('.mobile-composer-controls .repo-chip').tap();
		await h.page.locator('.mobile-composer-controls .workspace-dropdown').waitFor();
		await assertContextGeometry(h.page, `${label}-repository-open`);
		await h.page.locator('.mobile-composer-controls .repo-chip').tap();
		const branch = h.page.locator('.mobile-composer-controls .branch-selector > button');
		await branch.tap();
		await h.page.locator('.mobile-composer-controls .branch-dropdown').waitFor();
		await assertContextGeometry(h.page, `${label}-branch-open`);
		await h.page.screenshot({ path: `${artifacts}/${label}-worktree-${worktree}.png` });
		await branch.tap();
	}
	console.log(`PASS ${label}: long repository/branch names, worktree columns, repository and branch menus`);
	await h.close();
}

try {
	for (const width of [320, 390]) {
		for (const layout of ['compact', 'centered']) {
			await verifyGestures(width, layout);
			await verifyContext(width, 844, layout);
			await verifyContext(width, 430, layout);
		}
		await verifyContext(width, 844, 'compact', 'active');
	}
	// The worker-served web UI has no composer gesture and no controls sheet at
	// all (frontend/src/lib/components/chat/composer-shell.ts keeps the desktop
	// defaults; frontend/scripts/check-no-mobile-ui.mjs guards the bundle), so
	// there is no desktop leg to drive here.
	console.log(`PASS all composer fixture regressions; screenshots: ${artifacts}`);
} finally {
	await browser.close();
}
