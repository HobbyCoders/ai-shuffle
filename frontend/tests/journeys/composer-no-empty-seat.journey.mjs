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
// started, also when the page is busy and handles the second press late.
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
/** Watch the action row: when its circles last changed, and each click with when it was made. */
const watchRow = (page) =>
	page.evaluate(() => {
		const row = () => [...document.querySelectorAll('.send-cluster button')].filter((b) => b.getClientRects().length).map((b) => b.className).join('|');
		window.__row = { changes: [], last: row(), clicks: [] };
		// When the row last changed before a press was made.
		window.__row.changedBefore = (made) => Math.max(-Infinity, ...window.__row.changes.filter((t) => t <= made));
		new MutationObserver(() => {
			const now = row();
			if (now !== window.__row.last) { window.__row.last = now; window.__row.changes.push(performance.now()); }
		}).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'hidden', 'disabled'] });
		document.addEventListener('click', (e) => window.__row.clicks.push({ made: e.timeStamp, on: e.target.closest?.('button')?.getAttribute('aria-label') ?? null }), true);
	});

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
		name: 'first-send-slot',
		viewports: ['phone', 'fold-cover'],
		setup: async (ctx) => { await openNewChat(ctx); await ctx.page.waitForTimeout(800); },
		async act({ page, mark, variant, log }) {
			// #1645: the slot keeps its height through the first send from the
			// start screen. (A draft too long for one line takes its own row
			// while it is typed; that is the wrapped layout, not this.)
			await composer(page).click();
			await page.keyboard.type('hi');
			await page.waitForTimeout(400);
			await page.evaluate(() => {
				const height = () => Math.round(document.querySelector('.input-slot')?.getBoundingClientRect().height ?? -1);
				window.__slot = { before: height(), seen: [] };
				const tick = () => {
					const h = height();
					const seen = window.__slot.seen;
					if (seen[seen.length - 1] !== h) seen.push(h);
					if (!window.__slot.stop) requestAnimationFrame(tick);
				};
				requestAnimationFrame(tick);
			});
			await mark('first send');
			await sendButton(page).click();
			await waitChat(page, { streaming: 'false', messagesAtLeast: 2 }, { timeout: 60_000 });
			await page.waitForTimeout(300);
			const slot = await page.evaluate(() => { window.__slot.stop = true; return window.__slot; });
			log(`slot ${slot.before}px before Send, then ${slot.seen.join(' → ')}`);
			expect(slot.seen.every((h) => h === slot.before), `the slot keeps one height through the first send (${slot.before} → ${slot.seen.join(' → ')})`);
		}
	}, {
		name: 'double-tap-send',
		viewports: VIEWPORTS,
		async setup(ctx) {
			await withoutCalls(ctx.page);
			// A first exchange makes this a chat, where Stop takes Send's old place.
			await firstExchange(ctx);
		},
		async act({ page, mark, variant, log }) {
			// Each click's own stamp (when it was made) and when the page handled it.
			await page.evaluate(() => document.addEventListener('click', (e) => window.__clicks?.push({
				made: e.timeStamp, handled: performance.now(), on: e.target.closest?.('button')?.getAttribute('aria-label') ?? null
			}), true));
			const cdp = await page.context().newCDPSession(page);
			for (const stalled of [false, true]) {
				const what = stalled ? 'a double tap the page handles late, behind a busy main thread' : 'a brisk double tap';
				await mark(what);
				let proved = false;
				for (let attempt = 1; attempt <= 3 && !proved; attempt++) {
					await composer(page).click();
					await page.keyboard.type(`double tapped tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
					const send = await sendButton(page).boundingBox();
					const at = { x: send.x + send.width / 2, y: send.y + send.height / 2 };
					await page.evaluate(() => { window.__clicks = []; });
					await (isPhone(variant) ? page.touchscreen.tap(at.x, at.y) : page.mouse.click(at.x, at.y));
					if (stalled) {
						// Stop is in Send's place; the page then stalls for 700ms, and the
						// second press is made during the stall. Press and release are sent
						// together, as a finger does, so the browser stamps both as they
						// arrive and the page handles them when it is free again.
						await stopButton(page).waitFor({ timeout: 2000 });
						void page.evaluate(() => { const end = performance.now() + 700; while (performance.now() < end); }).catch(() => {});
						await page.waitForTimeout(50);
						const point = { x: at.x, y: at.y };
						await Promise.all(isPhone(variant)
							? [cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] }),
								cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })]
							: [cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 }),
								cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 })]);
					} else {
						await page.waitForTimeout(250);
						await (isPhone(variant) ? page.touchscreen.tap(at.x, at.y) : page.mouse.click(at.x, at.y));
					}
					await page.waitForTimeout(1500);
					const [first, second] = await page.evaluate(() => window.__clicks);
					const madeAfter = second ? Math.round(second.made - first.handled) : null;
					const handledAfter = second ? Math.round(second.handled - first.handled) : null;
					log(`${what}, attempt ${attempt}: second press made ${madeAfter}ms and handled ${handledAfter}ms after the send, on ${second?.on}`);
					// Only a second press made well inside the 500ms window, landing on
					// Stop (and, stalled, handled after the window) tests the guard; a
					// late dispatch by this test is retried, not judged.
					proved = !!second && second.on === 'Stop generating' && madeAfter < 450 && (!stalled || handledAfter > 520);
					const streaming = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-streaming');
					if (proved) expect(streaming === 'true', `${what}: the second press did not stop the turn it started (streaming=${streaming})`);
					if (streaming === 'true') {
						await stopButton(page).click();
						await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
					}
					await clearDraft(page);
				}
				expect(proved, `${what}: no attempt pressed Stop inside the window as intended (see the log)`);
			}
		}
	}, {
		// #1632 case 1: Stop leaves as the turn ends and Talk takes its place. A
		// tap meant for Stop that lands just after must not start a call.
		name: 'late-tap-on-stop',
		viewports: ['phone'],
		setup: firstExchange,
		async act({ page, mark, variant, log }) {
			await watchRow(page);
			let proved = false;
			for (let attempt = 1; attempt <= 3 && !proved; attempt++) {
				await mark(`a turn ends under a tap meant for Stop, attempt ${attempt}`);
				await typeAndSend(page, `short turn tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 40 })}`);
				const box = await stopButton(page).boundingBox();
				const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
				await page.evaluate(() => { window.__row.clicks = []; });
				await page.waitForFunction(() => !document.querySelector('.send-cluster .stop-btn'), null, { polling: 'raf', timeout: 60_000 });
				await page.touchscreen.tap(at.x, at.y);
				await page.waitForTimeout(1500);
				const { click, changedAt } = await page.evaluate(() => ({ click: window.__row.clicks[0], changedAt: window.__row.changedBefore(window.__row.clicks[0]?.made ?? 0) }));
				const after = click ? Math.round(click.made - changedAt) : null;
				log(`attempt ${attempt}: the tap was made ${after}ms after the row changed, on ${click?.on}`);
				proved = !!click && /call|Talk/i.test(click.on ?? '') && after < 300;
				const call = await page.getByRole('button', { name: 'Stop voice agent' }).isVisible();
				if (proved) expect(!call, `a tap ${after}ms after Talk took Stop's place did not start a call`);
				if (call) await page.getByRole('button', { name: 'Stop voice agent' }).click();
			}
			expect(proved, 'no attempt tapped Talk just after it took Stop\'s place (see the log)');
		}
	}, {
		// #1632 case 2: no call, a turn running, a draft sent. Send stays held for
		// the double-tap window, then the mic slides into its place. A second tap
		// on Send that lands on the mic must not start dictation.
		name: 'late-tap-on-held-send',
		viewports: ['fold-cover'],
		async setup(ctx) {
			await withoutCalls(ctx.page);
			await firstExchange(ctx);
		},
		async act({ page, mark, variant, log }) {
			await watchRow(page);
			await typeAndSend(page, `long turn tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
			await stopButton(page).waitFor({ timeout: 20_000 });
			let proved = false;
			for (let attempt = 1; attempt <= 3 && !proved; attempt++) {
				await mark(`a second tap on a queued Send, attempt ${attempt}`);
				await composer(page).click();
				await page.keyboard.type(`queued tok-${uniq()}`);
				const box = await sendButton(page).boundingBox();
				const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
				await page.evaluate(() => { window.__row.clicks = []; });
				await page.touchscreen.tap(at.x, at.y);
				await page.waitForFunction(() => !document.querySelector('.send-cluster .send-btn'), null, { polling: 'raf', timeout: 5000 });
				await page.touchscreen.tap(at.x, at.y);
				await page.waitForTimeout(1200);
				const { clicks, changedAt } = await page.evaluate(() => ({ clicks: window.__row.clicks, changedAt: window.__row.changedBefore(window.__row.clicks[1]?.made ?? 0) }));
				const second = clicks[1];
				const after = second ? Math.round(second.made - changedAt) : null;
				log(`attempt ${attempt}: the second tap was made ${after}ms after the row changed, on ${second?.on}`);
				proved = !!second && second.on === 'Start voice input' && after < 300;
				const dictating = await page.evaluate(() => !!document.querySelector('.voice-btn-wrapper.expanded, .voice-btn.recording, .voice-btn.preparing')
					|| [...document.querySelectorAll('[role="alert"]')].some((el) => /record|microphone/i.test(el.textContent ?? '')));
				if (proved) expect(!dictating, `a tap ${after}ms after the mic took Send's place did not start dictation`);
				if (dictating) {
					await page.keyboard.press('Escape');
					const dismiss = page.getByRole('button', { name: 'Dismiss' });
					if (await dismiss.isVisible()) await dismiss.click();
				}
			}
			expect(proved, 'no attempt tapped the mic just after it took Send\'s place (see the log)');
			await stopButton(page).click();
			await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
		}
	}, {
		// #1632 comment: a double click on Send in a new chat (desktop, a project
		// picked) left the first message "Not sent (stopped)".
		name: 'new-chat-double-click-send',
		viewports: ['desktop', 'phone'],
		setup: async (ctx) => { await openNewChat(ctx, { project: 'Dev other' }); await ctx.page.waitForTimeout(800); },
		async act({ page, mark, variant, log, token }) {
			const text = `Let's work on the work project tests today tok-${uniq()} ${scriptTag(variant, 'default')}`;
			await composer(page).click();
			await page.keyboard.type(text);
			await page.evaluate(() => {
				window.__clicks = [];
				document.addEventListener('click', (e) => window.__clicks.push({ made: Math.round(e.timeStamp), on: e.target.closest?.('button')?.getAttribute('aria-label') ?? e.target.tagName }), true);
			});
			await mark('double click Send');
			const box = await sendButton(page).boundingBox();
			const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
			if (isPhone(variant)) {
				await page.touchscreen.tap(at.x, at.y);
				await page.waitForTimeout(120);
				await page.touchscreen.tap(at.x, at.y);
			} else {
				await page.mouse.dblclick(at.x, at.y);
			}
			await waitChat(page, { messagesAtLeast: 2, streaming: 'false' }, { timeout: 60_000 });
			await page.waitForTimeout(500);
			log(`clicks: ${JSON.stringify(await page.evaluate(() => window.__clicks))}`);
			const notSent = await page.locator('[data-not-sent="true"]').count();
			expect(notSent === 0, `the first message was sent, not marked "Not sent" (${notSent} not-sent rows)`);
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
