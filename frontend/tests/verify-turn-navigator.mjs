// Real-browser check for the transcript turn rail (TurnNavigator) inside the
// real ChatView, with short (10) and long (80) histories at desktop and phone
// widths. One rail at every length: it must scroll by wheel/touch/keyboard,
// keep the current turn and both ends reachable, and never clip a target.
//
// Run from frontend/ while the fixture is serving:
//   npx vite dev --config tests/vite.turn-navigator.config.js --host 127.0.0.1 --port 5283 --strictPort
//   node tests/verify-turn-navigator.mjs
// Needs browser-mcp's playwright-core (npm ci --ignore-scripts --prefix ../browser-mcp)
// and Chrome/Chromium (CHROMIUM_PATH). TURN_NAV_FIXTURE_URL overrides the URL;
// TURNS=10,80,300 picks history lengths; SCREENSHOT_DIR keeps screenshots.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const browser = await chromium.launch({
	executablePath:
		process.env.CHROMIUM_PATH ||
		{
			win32: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
			darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
		}[process.platform] ||
		'/usr/bin/chromium',
	headless: true,
	args: ['--no-sandbox', '--disable-gpu']
});
const base = process.env.TURN_NAV_FIXTURE_URL || 'http://127.0.0.1:5283/tests/turn-navigator.html';
const shots = process.env.SCREENSHOT_DIR || join(tmpdir(), 'turn-navigator-browser');
await mkdir(shots, { recursive: true });
let passed = 0;

/** Geometry of the rail and its ticks, in viewport pixels. */
const railState = (page) =>
	page.evaluate(() => {
		const nav = document.querySelector('nav[aria-label="Navigate user turns"]');
		const rail = nav.querySelector('.turn-rail');
		const ticks = [...rail.querySelectorAll('button')];
		const railBox = rail.getBoundingClientRect();
		const visible = ticks
			.map((tick, index) => ({ index, box: tick.getBoundingClientRect() }))
			.filter(({ box }) => box.top >= railBox.top - 0.5 && box.bottom <= railBox.bottom + 0.5)
			.map(({ index }) => index);
		const current = ticks.findIndex((tick) => tick.getAttribute('aria-current') === 'true');
		const columnEl = [...document.querySelectorAll('.msg-scroll .chat-column')].find(
			(el) => el.getBoundingClientRect().width > 0
		);
		const column = columnEl.getBoundingClientRect();
		const columnPad = parseFloat(getComputedStyle(columnEl).paddingLeft);
		return {
			count: ticks.length,
			buttons: nav.querySelectorAll('button').length,
			tabStops: ticks.filter((tick) => tick.tabIndex >= 0).length,
			display: getComputedStyle(nav).display,
			rail: { top: railBox.top, bottom: railBox.bottom, right: nav.getBoundingClientRect().right },
			viewport: { width: innerWidth, height: innerHeight },
			textLeft: column.left + columnPad,
			minTickHeight: Math.min(...ticks.map((tick) => tick.getBoundingClientRect().height)),
			scrollTop: rail.scrollTop,
			scrollable: rail.scrollHeight > rail.clientHeight + 1,
			visible,
			current,
			pageScrollWidth: document.documentElement.scrollWidth
		};
	});

const transcriptTop = (page) => page.evaluate(() => document.querySelector('.msg-scroll').scrollTop);
const tick = (page, n, total) =>
	page.getByRole('button', { name: new RegExp(`^User message ${n} of ${total}:`) });

async function run(viewport, touch, turns) {
	const context = await browser.newContext({
		viewport,
		isMobile: touch,
		hasTouch: touch,
		deviceScaleFactor: 1,
		serviceWorkers: 'block'
	});
	await context.route((u) => u.pathname.startsWith('/api/'), (route) => route.abort());
	const page = await context.newPage();
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.goto(`${base}?turns=${turns}`);
	await page.waitForFunction(() => window.turnFixtureReady);
	await page.locator('nav[aria-label="Navigate user turns"] .turn-rail button').first().waitFor();
	await page.waitForTimeout(600);
	const label = `${viewport.width}x${viewport.height} ${turns} turns`;
	let state = await railState(page);
	await page.screenshot({ path: join(shots, `${viewport.width}-${turns}-initial.png`) });

	// Same rail at every length: one tick per turn, nothing else.
	assert.equal(state.display, 'flex', `${label}: rail hidden`);
	assert.equal(state.count, turns, `${label}: tick count`);
	assert.equal(state.buttons, turns, `${label}: extra controls`);
	assert.equal(state.tabStops, 1, `${label}: ${state.tabStops} Tab stops in the rail`);
	assert.ok(state.minTickHeight >= (touch ? 16 : 10) - 0.5, `${label}: ticks shrank to ${state.minTickHeight}px`);
	assert.ok(state.rail.top >= 0 && state.rail.bottom <= state.viewport.height, `${label}: rail off-screen`);
	assert.ok(state.rail.right <= state.textLeft + 0.5, `${label}: rail ${state.rail.right} overlaps text ${state.textLeft}`);
	assert.equal(state.pageScrollWidth, viewport.width, `${label}: horizontal page scroll`);
	assert.ok(state.visible.includes(state.current), `${label}: current tick ${state.current} not in view`);
	assert.equal(state.scrollable, turns > 30, `${label}: rail scrollability`);

	// Wheel (desktop) or touch drag (phone) scrolls the rail to the first turn.
	const railBox = await page.locator('.turn-rail').boundingBox();
	const transcriptBefore = await transcriptTop(page);
	if (turns > 30) {
		if (touch) {
			// A finger dragging down the rail (raw touch events; Chrome turns them
			// into a scroll gesture the same way it does on a phone).
			const cdp = await context.newCDPSession(page);
			const x = Math.round(railBox.x + 8);
			for (let drag = 0; drag < Math.ceil(turns / 16); drag++) {
				let y = Math.round(railBox.y + 40);
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
				for (let step = 0; step < 20; step++) {
					y += 15;
					await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y }] });
					await page.waitForTimeout(16);
				}
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
				await page.waitForTimeout(300);
			}
		} else {
			await page.mouse.move(railBox.x + 8, railBox.y + railBox.height / 2);
			for (let i = 0; i < Math.ceil(turns / 4); i++) await page.mouse.wheel(0, -400);
		}
		await page.waitForTimeout(400);
		state = await railState(page);
		assert.equal(state.scrollTop, 0, `${label}: rail did not scroll to the top`);
		assert.ok(state.visible.includes(0), `${label}: first tick not visible after scrolling`);
		assert.equal(await transcriptTop(page), transcriptBefore, `${label}: rail scroll leaked into the transcript`);
		await page.screenshot({ path: join(shots, `${viewport.width}-${turns}-scrolled-top.png`) });
	}

	// `soon` samples inside MessageArea's 600ms manual-navigation window; after
	// `settle` the highlight comes from its IntersectionObserver tracking, which
	// must agree once the jump has landed (settleSkippedTurns).
	const settle = () => page.waitForTimeout(1500);
	const soon = () => page.waitForTimeout(250);

	// First turn: reachable and navigates the transcript.
	const first = tick(page, 1, turns);
	if (touch) await first.tap();
	else await first.click();
	await soon();
	state = await railState(page);
	assert.equal(state.current, 0, `${label}: first tick did not become current`);
	assert.ok(state.visible.includes(0), `${label}: first tick not in view`);
	await settle();
	assert.ok((await transcriptTop(page)) < 60, `${label}: transcript not at the first turn`);

	// Keyboard: End moves focus and selection to the last turn and keeps it in
	// view; ArrowUp steps back from the focused tick.
	await first.focus();
	await page.keyboard.press('End');
	await soon();
	state = await railState(page);
	assert.equal(state.current, turns - 1, `${label}: End did not reach the last turn`);
	assert.ok(state.visible.includes(turns - 1), `${label}: last tick not in view after End`);
	assert.match(
		await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
		new RegExp(`^User message ${turns} of ${turns}:`)
	);
	await settle();
	assert.equal((await railState(page)).current, turns - 1, `${label}: highlight drifted after End landed`);
	const lastAnchorTop = await page.evaluate(() => {
		const s = document.querySelector('.msg-scroll');
		const anchors = s.querySelectorAll('[data-user-turn-id]');
		return anchors[anchors.length - 1].getBoundingClientRect().top - s.getBoundingClientRect().top;
	});
	assert.ok(lastAnchorTop >= 0 && lastAnchorTop < 140, `${label}: last turn not at the top (${lastAnchorTop})`);
	await page.keyboard.press('ArrowUp');
	await soon();
	state = await railState(page);
	assert.equal(state.current, turns - 2, `${label}: ArrowUp did not step back`);
	assert.ok(state.visible.includes(turns - 2), `${label}: previous tick not in view`);
	await settle();
	await page.screenshot({ path: join(shots, `${viewport.width}-${turns}-keyboard.png`) });

	// Middle turn: click/tap a tick that is in view after scrolling the rail.
	if (turns > 30) {
		const middle = Math.floor(turns / 2);
		await page.evaluate((index) => {
			const rail = document.querySelector('.turn-rail');
			const target = rail.children[index];
			rail.scrollTop = target.offsetTop - rail.clientHeight / 2;
		}, middle);
		await page.waitForTimeout(200);
		const target = tick(page, middle + 1, turns);
		if (touch) await target.tap();
		else await target.click();
		await soon();
		state = await railState(page);
		assert.equal(state.current, middle, `${label}: middle tick did not become current`);
		assert.ok(state.visible.includes(middle), `${label}: middle tick left the view`);
		if (touch) {
			// A tap must not leave the tick focused/lit once the reader scrolls on.
			const lit = await page.evaluate(() => {
				const el = document.activeElement;
				return !!el?.closest('.turn-rail') || !!document.querySelector('.turn-rail .is-hovered');
			});
			assert.equal(lit, false, `${label}: tapped tick stayed focused`);
		}
		await settle();
		assert.equal((await railState(page)).current, middle, `${label}: highlight drifted after the jump landed`);
	}

	// Reading the transcript upward in small steps moves the current turn and
	// the rail follows it.
	const before = (await railState(page)).current;
	const transcriptBox = await page.locator('.msg-scroll').boundingBox();
	for (let i = 0; i < 40; i++) {
		if (touch) {
			await page.evaluate(() => document.querySelector('.msg-scroll').scrollBy(0, -100));
		} else {
			await page.mouse.move(transcriptBox.x + transcriptBox.width / 2, transcriptBox.y + 200);
			await page.mouse.wheel(0, -100);
		}
		await page.waitForTimeout(40);
	}
	await settle();
	state = await railState(page);
	assert.ok(state.current < before, `${label}: current turn did not follow the transcript (${before} -> ${state.current})`);
	assert.ok(state.visible.includes(state.current), `${label}: rail did not follow the current turn`);
	await page.screenshot({ path: join(shots, `${viewport.width}-${turns}-reading.png`) });

	// Desktop hover preview sits beside the hovered tick and is not clipped by the rail.
	if (!touch) {
		const hovered = tick(page, turns - 1, turns);
		await hovered.hover();
		await page.waitForTimeout(300);
		const preview = await page.evaluate(() => {
			const box = document.querySelector('.turn-nav .preview').getBoundingClientRect();
			return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, opacity: getComputedStyle(document.querySelector('.turn-nav .preview')).opacity };
		});
		const tickBox = await hovered.boundingBox();
		const center = tickBox.y + tickBox.height / 2;
		assert.equal(preview.opacity, '1', `${label}: preview hidden`);
		assert.ok(preview.top < center && preview.bottom > center, `${label}: preview not beside its tick`);
		assert.ok(preview.right <= viewport.width, `${label}: preview off-screen`);
		await page.screenshot({ path: join(shots, `${viewport.width}-${turns}-hover.png`) });
	}

	assert.deepEqual(errors, [], `${label}: page errors`);
	await context.close();
	passed++;
	console.log(`ok ${label}`);
}

try {
	for (const turns of (process.env.TURNS || '10,80').split(',').map(Number)) {
		await run({ width: 1440, height: 900 }, false, turns);
		await run({ width: 390, height: 844 }, true, turns);
	}
	console.log(`${passed} scenarios passed; screenshots in ${shots}`);
} finally {
	await browser.close();
}
