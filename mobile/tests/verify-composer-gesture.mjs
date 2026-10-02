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
const baseUrl = process.env.COMPOSER_FIXTURE_URL || 'http://127.0.0.1:5181/mobile/tests/composer-gesture.html';
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

// #1612: composer controls are 44px touch targets and the editor text is 16px,
// in both composer states and with the controls sheet open.
async function verifyTouchSizing(width, height, layout) {
	const h = await harness(width, height);
	await h.load({ layout, mode: 'code' });
	const label = `${width}x${height}-${layout}`;
	const check = async state => {
		const result = await h.page.evaluate(() => {
			const visible = selector => [...document.querySelectorAll(selector)].filter(node => node.getClientRects().length);
			const small = visible('.send-btn, .send-cluster .stop-btn, .controls-dock .dock-ico, .controls-dock .voice-btn, .controls-dock .voice-call-btn, .controls-dock .usage-indicator, .model-control, .quick-choice')
				.map(node => ({ cls: node.className, ...node.getBoundingClientRect().toJSON() }))
				.filter(box => box.width < 44 || box.height < 44);
			const editor = document.querySelector('.chat-input-wrapper .ProseMirror');
			return { small, font: editor && getComputedStyle(editor).fontSize, overflow: document.documentElement.scrollWidth > window.innerWidth };
		});
		assert.deepEqual(result.small, [], `${label} ${state}: touch targets under 44px`);
		assert.equal(result.font, '16px', `${label} ${state}: editor text size`);
		assert.equal(result.overflow, false, `${label} ${state}: no horizontal page scroll`);
	};
	await check('idle');
	await h.page.locator('.tiptap').tap();
	await h.page.waitForTimeout(350);
	await check('expanded');
	await h.setOpen(true);
	await check('sheet open');
	console.log(`PASS ${label}: 44px composer touch targets and 16px editor text`);
	await h.close();
}

// #1612: at 360px and narrower the code lane's placeholder is shortened so it
// is never cut off with an ellipsis; wider phones and chat keep their wording.
const LONG_PLACEHOLDER = 'Describe a task or ask a question';
const SHORT_PLACEHOLDER = 'Ask or describe a task';
async function verifyPlaceholder(width) {
	const seen = new Set();
	for (const mode of ['chat', 'code']) {
		const h = await harness(width, 844);
		// The lane store reads its stored mode before the fixture writes it, so
		// the lane takes effect on the second load in a context.
		await h.load({ layout: 'compact', mode });
		await h.load({ layout: 'compact', mode });
		const result = await h.page.evaluate(() => {
			const line = document.querySelector('.chat-input-wrapper .ProseMirror p.is-editor-empty');
			const before = getComputedStyle(line, '::before');
			const text = before.content.replace(/^"|"$/g, '');
			const probe = document.createElement('span');
			probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${before.font};letter-spacing:${before.letterSpacing}`;
			probe.textContent = text;
			document.body.append(probe);
			const textWidth = probe.getBoundingClientRect().width;
			probe.remove();
			// The placeholder box is absolutely positioned across the line on mobile.
			return { set: line.dataset.placeholder, text, textWidth, room: parseFloat(before.width) || line.clientWidth, overflow: before.textOverflow };
		});
		seen.add(result.set);
		const label = `${width}px "${result.set}"`;
		const expected = width <= 360 && result.set === LONG_PLACEHOLDER ? SHORT_PLACEHOLDER : result.set;
		assert.equal(result.text, expected, `${label}: shown placeholder`);
		if (width <= 360) assert.ok(result.textWidth <= result.room, `${label}: placeholder fits without an ellipsis: ${JSON.stringify(result)}`);
		await h.page.screenshot({ path: `${artifacts}/${width}-${mode}-placeholder.png` });
		console.log(`PASS ${label}: shows "${result.text}" (${Math.round(result.textWidth)} of ${result.room}px)`);
		await h.close();
	}
	assert.ok(seen.has(LONG_PLACEHOLDER) && seen.has('Ask anything'), `both lane placeholders covered: ${[...seen]}`);
}

// #1616 (owner decision 2026-10-01): the new-session composer offers the call
// button, and a tap creates a normal conversation from the composer's picks,
// then starts the call on it. The fixture stubs both requests and denies the
// microphone, so the call stops before any audio or realtime connection.
async function verifyFreshComposerCall(width, worktree) {
	const h = await harness(width, 844);
	const { page } = h;
	await h.load({ layout: 'compact', mode: 'code', worktree, voice: 'on' });
	await page.locator('.tiptap').tap();
	await page.waitForTimeout(350);
	const call = page.locator('.controls-dock .voice-call-btn');
	await call.waitFor();
	assert.equal(await call.getAttribute('aria-label'), 'Start a call in a new conversation');
	assert.equal(await call.getAttribute('aria-disabled'), null, 'fresh composer call button is available');
	const box = await call.boundingBox();
	assert.ok(box.width >= 44 && box.height >= 44, `call button is a 44px touch target: ${JSON.stringify(box)}`);
	// A double tap must still create one session and start one call.
	await call.tap();
	await call.tap({ force: true }).catch(() => {});
	await page.waitForFunction(() => window.fixtureCalls.some(c => c.method === 'POST' && c.path.endsWith('/attach')));
	await page.waitForTimeout(300);
	const posts = await page.evaluate(() => window.fixtureCalls.filter(c => c.method === 'POST'));
	const creates = posts.filter(c => c.path === '/api/v1/sessions' || c.path === '/api/v1/sessions/with-worktree');
	assert.equal(creates.length, 1, `one session created: ${JSON.stringify(posts)}`);
	const [create] = creates;
	if (worktree === 'off') {
		assert.equal(create.path, '/api/v1/sessions');
		assert.equal(create.body.project_id, 'composer-fixture');
		assert.equal(create.body.worktree_id, undefined);
	} else {
		// Worktree on, with the branch chip on Auto.
		assert.equal(create.path, '/api/v1/sessions/with-worktree');
		assert.match(create.body.branch_name, /^call\//);
		assert.equal(create.body.create_new_branch, true);
		assert.equal(create.body.prompt, undefined, 'no first message is sent');
	}
	const attaches = posts.filter(c => c.path === '/api/v1/voice/sessions/fixture-call-session/attach');
	assert.equal(attaches.length, 1, 'the call attaches to the created session once');
	assert.ok(posts.indexOf(create) < posts.indexOf(attaches[0]), 'session is created before the call starts');
	assert.ok(!posts.some(c => c.path === '/api/v1/voice/sessions'), 'no voice-lane session is created');
	await page.screenshot({ path: `${artifacts}/${width}-worktree-${worktree}-fresh-call.png` });
	console.log(`PASS ${width}px worktree ${worktree}: fresh-composer call creates ${create.path} then attaches the call`);
	await h.close();
}

// A call only attaches to a conversation on this install, so with another
// device picked as the runner the button is unavailable and a tap says why.
async function verifyFreshComposerCallOnWorker(width) {
	const h = await harness(width, 844);
	const { page } = h;
	await h.load({ layout: 'compact', mode: 'chat', voice: 'on', device: 'worker' });
	await page.locator('.tiptap').tap();
	await page.waitForTimeout(350);
	const call = page.locator('.controls-dock .voice-call-btn');
	await call.waitFor();
	assert.equal(await call.getAttribute('aria-disabled'), 'true');
	const reason = await call.getAttribute('title');
	assert.match(reason, /Switch the runner to Local.*Studio PC/);
	await call.tap({ force: true });
	const banner = page.locator('.banner-host');
	await banner.waitFor();
	assert.ok((await banner.innerText()).includes(reason), 'tap shows the tooltip reason');
	const posts = await page.evaluate(() => window.fixtureCalls.filter(c => c.method === 'POST'));
	assert.deepEqual(posts, [], 'nothing is created or called');
	console.log(`PASS ${width}px: fresh-composer call with a worker runner explains itself and creates nothing`);
	await h.close();
}

try {
	for (const width of [320, 360, 390]) await verifyPlaceholder(width);
	for (const worktree of ['off', 'on']) await verifyFreshComposerCall(320, worktree);
	await verifyFreshComposerCallOnWorker(320);
	for (const width of [320, 390]) {
		await verifyTouchSizing(width, 844, 'compact');
		await verifyTouchSizing(width, 430, 'compact');
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
