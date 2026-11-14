// Journey: the composer's action row holds no empty seat. Owner decision
// (2026-11-14): when Send or Stop is not shown, the other actions move over
// and take its space; nothing reserves room for a button that is not there.
// This replaces the reserved Stop seat of #1123/#1166.
//
// Every state the circles can be in, with a call available and without one:
//   idle, empty        mic · Talk            mic · Send (disabled)
//   idle, text         mic · Send            mic · Send
//   working, empty     mic · Talk · Stop     mic · Stop
//   working, text      mic · Send · Stop     mic · Send · Stop
// The cluster's every box shows a button, and its last button sits at its
// right edge. Stop arriving and leaving keeps the composer's height.
//
// double-tap-send: Stop now arrives where Send was (Send moves one seat left).
// A double tap on Send must not land on that Stop and cancel the turn it just
// started.
import { runJourney } from './runner.mjs';
import { composer, expect, isPhone, openNewChat, scriptTag, sendButton, stopButton, typeAndSend, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const VIEWPORTS = ['desktop', 'phone', 'fold-cover'];

/** The visible circles by kind, any box in the cluster that shows no button, and the cluster's height. */
const actionRow = (page) =>
	page.evaluate(() => {
		const cluster = [...document.querySelectorAll('.send-cluster')].find((el) => el.getClientRects().length);
		if (!cluster) return null;
		const shown = (el) => el.getClientRects().length > 0 && el.getBoundingClientRect().width > 0;
		const kind = (b) =>
			b.classList.contains('voice-btn') ? 'mic'
			: b.classList.contains('call-circle') ? 'talk'
			: b.classList.contains('creating-worktree') ? 'busy'
			: b.classList.contains('send-btn') ? 'send'
			: b.classList.contains('stop-btn') ? 'stop' : null;
		const buttons = [...cluster.querySelectorAll('button')].filter((b) => shown(b) && kind(b));
		// Boxes laid out in the row (display: contents unwrapped).
		const boxes = [];
		const walk = (el) => {
			for (const child of el.children) {
				if (getComputedStyle(child).display === 'contents') walk(child);
				else if (shown(child)) boxes.push(child);
			}
		};
		walk(cluster);
		const empty = boxes
			.filter((box) => !(box.tagName === 'BUTTON' || [...box.querySelectorAll('button')].some(shown)))
			.map((box) => `${box.tagName.toLowerCase()}.${[...box.classList].join('.')} ${Math.round(box.getBoundingClientRect().width)}px`);
		const right = cluster.getBoundingClientRect().right;
		const lastRight = Math.max(...buttons.map((b) => b.getBoundingClientRect().right));
		const box = document.querySelector('[aria-label="Chat input with file drop"]')?.getBoundingClientRect();
		return {
			circles: buttons.map(kind),
			empty,
			trailing: Math.round(right - lastRight),
			composerHeight: box ? Math.round(box.height) : null
		};
	});

async function expectRow(page, want, what, log) {
	// Talk mounts after voice settings load; give the row a moment to settle.
	let row;
	for (let i = 0; i < 20; i++) {
		row = await actionRow(page);
		if (row && JSON.stringify(row.circles) === JSON.stringify(want)) break;
		await page.waitForTimeout(150);
	}
	log(`${what}: ${JSON.stringify(row)}`);
	expect(row, `${what}: the action row is on screen`);
	expect(JSON.stringify(row.circles) === JSON.stringify(want), `${what}: the row shows ${want.join(' · ')}, not ${row.circles.join(' · ')}`);
	expect(row.empty.length === 0, `${what}: no empty seat in the row (${row.empty.join(', ')})`);
	expect(row.trailing <= 1, `${what}: the last action sits at the row's right edge (${row.trailing}px after it)`);
	return row;
}

async function clearDraft(page) {
	const box = composer(page);
	await box.click();
	await box.press('ControlOrMeta+A');
	await box.press('Backspace');
}

/** Voice calls off for this page only: the composer then has no Talk circle. */
async function withoutCalls(page) {
	await page.route('**/api/v1/settings/voice', async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		const response = await route.fetch();
		const json = await response.json();
		await route.fulfill({ response, json: { ...json, voice_mode_enabled: false } });
	});
}

const seatCase = (call) => ({
	name: call ? 'with-call' : 'without-call',
	viewports: VIEWPORTS,
	async setup(ctx) {
		if (!call) await withoutCalls(ctx.page);
		await openNewChat(ctx);
		await ctx.page.waitForTimeout(800);
	},
	async act({ page, mark, variant, log }) {
		const idleEmpty = call ? ['mic', 'talk'] : ['mic', 'send'];
		const workingEmpty = call ? ['mic', 'talk', 'stop'] : ['mic', 'stop'];

		await mark('new chat: idle, empty');
		await expectRow(page, idleEmpty, 'new chat, idle, empty', log);
		await composer(page).click();
		await page.keyboard.type('draft');
		await expectRow(page, ['mic', 'send'], 'new chat, idle, text', log);

		await mark('send: working');
		await typeAndSend(page, `stream slowly tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
		await waitChat(page, { streaming: 'true' });
		await stopButton(page).waitFor();
		await page.waitForTimeout(700);
		const working = await expectRow(page, workingEmpty, 'chat, working, empty', log);

		await mark('working: type');
		await composer(page).click();
		await page.keyboard.type('next');
		await expectRow(page, ['mic', 'send', 'stop'], 'chat, working, text', log);
		await clearDraft(page);
		await expectRow(page, workingEmpty, 'chat, working, cleared', log);

		await mark('stop: idle');
		await stopButton(page).click();
		await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
		await stopButton(page).waitFor({ state: 'detached', timeout: 5000 });
		const idle = await expectRow(page, idleEmpty, 'chat, idle, empty', log);
		expect(idle.composerHeight === working.composerHeight,
			`Stop leaving keeps the composer's height (${working.composerHeight} → ${idle.composerHeight})`);
		await composer(page).click();
		await page.keyboard.type('later');
		await expectRow(page, ['mic', 'send'], 'chat, idle, text', log);
		await clearDraft(page);
	}
});

await runJourney({
	name: 'composer-no-empty-seat',
	allow: DEV_INSTANCE_NOISE,
	cases: [seatCase(true), seatCase(false), {
		name: 'double-tap-send',
		viewports: VIEWPORTS,
		async setup(ctx) {
			await withoutCalls(ctx.page);
			await openNewChat(ctx);
			await ctx.page.waitForTimeout(800);
			// A first exchange makes this a chat, where Stop takes Send's old place.
			await typeAndSend(ctx.page, `first tok-${uniq()} ${scriptTag(ctx.variant, 'default')}`);
			await waitChat(ctx.page, { streaming: 'false', messagesAtLeast: 2 }, { timeout: 60_000 });
		},
		async act({ page, mark, variant, log }) {
			await composer(page).click();
			await page.keyboard.type(`double tapped tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
			const send = await sendButton(page).boundingBox();
			const at = { x: send.x + send.width / 2, y: send.y + send.height / 2 };
			const tap = () => (isPhone(variant) ? page.touchscreen.tap(at.x, at.y) : page.mouse.click(at.x, at.y));

			await mark('double tap Send');
			await tap();
			// A brisk double tap: the second press 250ms after the first.
			await page.waitForTimeout(250);
			const under = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('button')?.getAttribute('aria-label') ?? null, at);
			log(`second tap lands on: ${under}`);
			await tap();
			await page.waitForTimeout(1500);
			const streaming = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-streaming');
			expect(streaming === 'true', `the second tap did not stop the turn it started (streaming=${streaming}, under the finger: ${under})`);
			await stopButton(page).click();
			await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
		}
	}]
});
