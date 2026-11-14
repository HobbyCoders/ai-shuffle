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
//
// draft-in-stop-band: a one-line draft that fits the field without Stop but
// not beside it. Stop arriving (a turn sent from another tab) and leaving
// must not flip the phone's wrapped layout: the composer keeps its height
// and the message list its place.
import { runJourney } from './runner.mjs';
import { chatIdByToken, composer, expect, isPhone, openNewChat, scriptTag, sendButton, stopButton, typeAndSend, uniq, waitChat, waitChatOpen } from './lib/app.mjs';
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

/** Height and wrap of the composer, and where the message list ends. */
const composerPlace = (page) =>
	page.evaluate(() => {
		const box = document.querySelector('[aria-label="Chat input with file drop"]')?.getBoundingClientRect();
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]')?.getBoundingClientRect();
		return {
			composerHeight: box ? Math.round(box.height) : null,
			wrapped: !!document.querySelector('.chat-island.wrapped'),
			listBottom: log ? Math.round(log.bottom) : null,
			stop: !!document.querySelector('.send-cluster .stop-btn')
		};
	});

/** Open a first exchange, so this is a chat, where Stop can come and go under a draft. */
async function firstExchange(ctx) {
	await openNewChat(ctx);
	await ctx.page.waitForTimeout(800);
	ctx.token = `tok-${uniq()}`;
	await typeAndSend(ctx.page, `first ${ctx.token} ${scriptTag(ctx.variant, 'default')}`);
	await waitChat(ctx.page, { streaming: 'false', messagesAtLeast: 2 }, { timeout: 60_000 });
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
	// without-call overrides the voice settings with page.route, which a service worker's fetch would bypass.
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	allow: DEV_INSTANCE_NOISE,
	cases: [seatCase(true), seatCase(false), {
		name: 'double-tap-send',
		viewports: VIEWPORTS,
		async setup(ctx) {
			await withoutCalls(ctx.page);
			// A first exchange makes this a chat, where Stop takes Send's old place.
			await firstExchange(ctx);
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
	}, {
		name: 'draft-in-stop-band',
		viewports: VIEWPORTS,
		setup: firstExchange,
		async act({ page, mark, variant, log, token }) {
			// A run of letters as wide as the field without Stop, less half of
			// Stop's seat: it fits there, but not beside Stop.
			const draft = await page.evaluate(() => {
				const editor = document.querySelector('.chat-island .ProseMirror');
				const cluster = [...document.querySelectorAll('.send-cluster')].find((el) => el.getClientRects().length);
				const css = getComputedStyle(editor);
				const field = editor.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
				const seat = parseFloat(getComputedStyle(cluster).getPropertyValue('--composer-stop-seat')) || 38;
				const ruler = document.createElement('span');
				ruler.style.cssText = 'position:fixed;visibility:hidden;white-space:pre';
				for (const key of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing', 'wordSpacing']) ruler.style[key] = css[key];
				document.body.append(ruler);
				let text = '';
				while (ruler.getBoundingClientRect().width < field - seat / 2) ruler.textContent = text += 'ab'.charAt(text.length % 2);
				ruler.remove();
				return { text, field: Math.round(field), seat };
			});
			log(`draft: ${draft.text.length} letters for a ${draft.field}px field, Stop's seat ${draft.seat}px`);
			await composer(page).click();
			await page.keyboard.type(draft.text);
			await page.waitForTimeout(500);
			const before = await composerPlace(page);
			log(`idle: ${JSON.stringify(before)}`);

			await mark('Stop arrives: a turn from another tab');
			const other = await page.context().newPage();
			try {
				// The same chat in a second tab of this browser.
				const url = new URL(page.url());
				url.searchParams.set('session', await chatIdByToken(page, token));
				await other.goto(url.toString());
				await waitChatOpen(other, { messagesAtLeast: 2 });
				await typeAndSend(other, `from another tab tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
				await stopButton(page).waitFor({ timeout: 20_000 });
				await page.waitForTimeout(700);
				const working = await composerPlace(page);
				log(`working: ${JSON.stringify(working)}`);
				expect(working.stop, "Stop shows while the other tab's turn runs");
				expect(working.wrapped === before.wrapped && working.composerHeight === before.composerHeight,
					`Stop arriving keeps the composer (${JSON.stringify(before)} → ${JSON.stringify(working)})`);
				expect(working.listBottom === before.listBottom,
					`Stop arriving keeps the message list in place (bottom ${before.listBottom} → ${working.listBottom})`);

				await mark('Stop leaves: the turn ends');
				await stopButton(other).click();
				await stopButton(page).waitFor({ state: 'detached', timeout: 15_000 });
				await page.waitForTimeout(700);
				const after = await composerPlace(page);
				log(`idle again: ${JSON.stringify(after)}`);
				expect(after.wrapped === before.wrapped && after.composerHeight === before.composerHeight,
					`Stop leaving keeps the composer (${JSON.stringify(working)} → ${JSON.stringify(after)})`);
				expect(after.listBottom === before.listBottom,
					`Stop leaving keeps the message list in place (bottom ${working.listBottom} → ${after.listBottom})`);
				const kept = await composer(page).innerText();
				expect(kept.trim() === draft.text, 'the draft stays in the composer throughout');
			} finally {
				await other.close();
			}
			await clearDraft(page);
		}
	}]
});
