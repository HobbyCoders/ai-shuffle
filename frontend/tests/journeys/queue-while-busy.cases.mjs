// Journey: send a second message while the first reply is still streaming.
// The second message must show as queued/pending right away (the user can see
// it was taken), be delivered exactly once after the first turn, and get its
// own reply. At no point may it show twice (queued bubble and
// delivered row together, or two rows), and nothing may remount.
import { fakeAiTag } from './runner.mjs';
import {
	SEEDED,
	SLOW_REPLY,
	api,
	chatIdByToken,
	countUserRows,
	expect,
	holdBootLoad,
	openFromSidebar,
	openNewChat,
	scriptTag,
	scrollTranscriptTo,
	seededChatId,
	showSidebar,
	stopButton,
	transcriptScroll,
	typeAndSend,
	uniq,
	waitChat,
	waitChatOpen,
	waitTurnSettled,
	watchUserText
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';
import { openStopChat, watchStopFrames } from './lib/stop-queue.mjs';

// #1052: from its first sighting until the watch ends, a message must show
// exactly once (a pending bubble or a transcript row): never twice, never
// nowhere. The watch judges every DOM state script can observe, not painted
// frames. A MutationObserver callback runs at each microtask checkpoint after a
// change, and no script runs between the last checkpoint of a rendering update
// (rAF and ResizeObserver callbacks included) and its paint, so every painted
// state is one the watch judged. That makes a pass sound for every frame. It is
// deliberately stricter than paint: a state that exists between two
// checkpoints of one task and is gone before paint also fails the watch, so the
// product must swap a bubble for its row in one update, as it does.
async function watchExactlyOnce(page, needles) {
	await page.evaluate((needles) => {
		const started = performance.now();
		const state = (window.__journeyOnce = { checks: 0, seen: {}, gaps: {}, twice: {} });
		const check = () => {
			state.checks++;
			const at = Math.round(performance.now() - started);
			const rows = [...document.querySelectorAll('[data-user-turn-id], .pending-messages [role="article"]')];
			for (const needle of needles) {
				// A copy counts only while it has a layout box (not removed, not display:none).
				const count = rows.filter((el) => (el.textContent || '').includes(needle) && el.getClientRects().length > 0).length;
				if (count > 0 && state.seen[needle] === undefined) state.seen[needle] = at;
				if (count === 0 && state.seen[needle] !== undefined) (state.gaps[needle] ||= []).push(at);
				if (count > 1) (state.twice[needle] ||= []).push(at);
			}
		};
		const observer = new MutationObserver(check);
		observer.observe(document.documentElement, {
			subtree: true, childList: true, characterData: true,
			attributes: true, attributeFilter: ['class', 'style', 'hidden', 'role', 'data-user-turn-id']
		});
		state.stop = () => { check(observer.takeRecords()); observer.disconnect(); };
		check();
	}, needles);
	return () => page.evaluate(() => {
		const state = window.__journeyOnce;
		state.stop();
		const span = (list) => list && { checks: list.length, first: list[0], last: list[list.length - 1] };
		return { checks: state.checks, seen: state.seen,
			gaps: Object.fromEntries(Object.entries(state.gaps).map(([k, v]) => [k, span(v)])),
			twice: Object.fromEntries(Object.entries(state.twice).map(([k, v]) => [k, span(v)])) };
	});
}

// The watch must catch a gap and a duplicate wherever the page makes them: from
// an animation frame callback, from a ResizeObserver callback late in the
// rendering update (after other observers, and cascaded into a deeper element),
// and by hiding the row rather than removing it. Each probe is its own needle.
async function expectWatcherCatchesEveryState(page, log) {
	const kinds = ['raf-gap', 'raf-twice', 'ro-gap', 'ro-cascade-gap', 'ro-twice', 'hidden-gap'];
	const needles = Object.fromEntries(kinds.map((kind) => [kind, `probe-${kind}-${uniq()}`]));
	const done = await watchExactlyOnce(page, Object.values(needles));
	await page.evaluate((needles) => new Promise((resolve) => {
		const host = document.body.appendChild(document.createElement('div'));
		host.dataset.journeyProbe = '1';
		host.style.cssText = 'position:fixed;left:0;top:0;width:4px;overflow:hidden';
		const row = (needle, parent = host) => {
			const el = document.createElement('div');
			el.setAttribute('data-user-turn-id', 'journey-probe');
			el.textContent = needle;
			el.style.cssText = 'height:1px';
			return parent.appendChild(el);
		};
		const rows = Object.fromEntries(Object.entries(needles).map(([kind, needle]) => [kind, row(needle)]));
		// A deeper copy for the cascade: an earlier observer resizes its wrapper,
		// and the wrapper's own observer (delivered in a later pass) removes it.
		const wrap = host.appendChild(document.createElement('div'));
		wrap.style.cssText = 'height:1px';
		wrap.appendChild(rows['ro-cascade-gap']);
		const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
		const inLateObserver = (target, fn) => new Promise((r) => {
			const ro = new ResizeObserver(() => { ro.disconnect(); fn(); r(); });
			ro.observe(target);
			target.style.height = target.style.height === '2px' ? '1px' : '2px';
		});
		(async () => {
			await nextFrame(); await nextFrame();
			// rAF: missing for one frame, then shown twice for one frame.
			rows['raf-gap'].remove(); await nextFrame(); host.appendChild(rows['raf-gap']); await nextFrame();
			const extra = row(needles['raf-twice']); await nextFrame(); extra.remove(); await nextFrame();
			// Late ResizeObserver callbacks, then put right at the next frame.
			await inLateObserver(host, () => rows['ro-gap'].remove()); await nextFrame(); host.appendChild(rows['ro-gap']);
			await nextFrame();
			// Cascade: the wrapper's observer first takes its initial notification
			// (which does nothing), so the only later one it gets is the resize the
			// outer observer makes, delivered in a second observer pass.
			let armed = false;
			let removed;
			const cascaded = new Promise((r) => { removed = r; });
			const inner = new ResizeObserver(() => {
				if (!armed) return;
				inner.disconnect(); rows['ro-cascade-gap'].remove(); removed();
			});
			inner.observe(wrap);
			await nextFrame(); await nextFrame();
			armed = true;
			const outer = new ResizeObserver(() => { outer.disconnect(); wrap.style.height = '3px'; });
			outer.observe(host);
			// No cascade in 2s means no gap, and the probe's own check then fails.
			await Promise.race([cascaded, new Promise((r) => setTimeout(r, 2000))]);
			inner.disconnect(); await nextFrame(); wrap.appendChild(rows['ro-cascade-gap']);
			await nextFrame();
			let dup = null;
			await inLateObserver(host, () => { dup = row(needles['ro-twice']); }); await nextFrame(); dup.remove();
			await nextFrame();
			// Hidden, not removed.
			rows['hidden-gap'].style.display = 'none'; await nextFrame(); rows['hidden-gap'].style.display = ''; await nextFrame();
			resolve();
		})();
	}), needles);
	const result = await done();
	await page.evaluate(() => document.querySelectorAll('[data-journey-probe]').forEach((el) => el.remove()));
	log(`watcher probe: ${JSON.stringify(result)}`);
	for (const kind of kinds) {
		const needle = needles[kind];
		expect(result.seen[needle] !== undefined, `the ${kind} probe row was seen`);
		const caught = kind.endsWith('twice') ? result.twice[needle] : result.gaps[needle];
		expect(!!caught, `the watcher caught the ${kind} probe (${JSON.stringify(result)})`);
		const other = kind.endsWith('twice') ? result.gaps[needle] : result.twice[needle];
		expect(!other, `the ${kind} probe made only the change it was meant to (${JSON.stringify(other)})`);
	}
}

// Type and send without per-key round trips, so sends land tens of ms apart.
async function quickSend(page, text) {
	const box = page.getByRole('textbox', { name: 'Message' });
	await box.click();
	await page.keyboard.insertText(text);
	await page.getByRole('button', { name: 'Send', exact: true }).click();
}

function expectEachOnce(result, needles, log) {
	log(`each-once: ${JSON.stringify(result)}`);
	for (const needle of needles) {
		expect(result.seen[needle] !== undefined, `${needle} never showed`);
		const gap = result.gaps[needle];
		expect(!gap, `${needle} showed nowhere at ${gap?.checks} checks (${gap?.first}..${gap?.last}ms)`);
		const twice = result.twice[needle];
		expect(!twice, `${needle} showed twice at ${twice?.checks} checks (${twice?.first}..${twice?.last}ms)`);
	}
}

export const journey = {
	name: 'queue-while-busy',
	recorder: { selectors: [STOP_SELECTOR, 'pending=.pending-messages [role="article"]'] },
	cases: [
		{
			name: 'queue',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				await typeAndSend(page, `first tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 5 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('send while busy');
				const second = `tok-${uniq()}`;
				const seen = await watchUserText(page, second);
				const t0 = Date.now();
				await typeAndSend(page, `queued ${second} ${fakeAiTag(variant)}`.trim());
				// Taken at once: the message shows (queued, or already a row) within a moment.
				await page.waitForFunction(
					(n) =>
						[...document.querySelectorAll('[data-user-turn-id], .pending-messages [role="article"]')].some((el) =>
							(el.textContent || '').includes(n)
						),
					second,
					{ timeout: 3000, polling: 'raf' }
				);
				log(`second message on screen ${Date.now() - t0}ms after send`);

				// The first reply finishes, then the second is delivered and answered.
				await page.getByText(SLOW_REPLY.end, { exact: false }).first().waitFor({ timeout: 90_000 });
				await waitTurnSettled(page, { text: `You said: “queued ${second}`, messagesAtLeast: 4, timeout: 90_000 });
				const peak = await seen();
				expect(peak.max === 1, `the queued message showed ${peak.max} times at once (at ${peak.at}ms)`);
				expect((await countUserRows(page, second)) === 1, 'the queued message is delivered once');
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no pending bubble left behind');
			},
			verify({ recording }) {
				expectStopFollowsStreaming(recording, { from: markTime(recording, 'send the first') });
			}
		},
		{
			// #1052: three messages sent tens of ms apart into a running turn. Each
			// queue snapshot can predate a later send; none may drop its bubble.
			name: 'rapid-sends-each-show-once',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				await typeAndSend(page, `first tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 10 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await expectWatcherCatchesEveryState(page, log);
				await mark('send three at once');
				const needles = [`tok-${uniq()}`, `tok-${uniq()}`, `tok-${uniq()}`];
				const done = await watchExactlyOnce(page, needles);
				for (const needle of needles) await quickSend(page, `queued ${needle}`);
				await page.waitForFunction((needles) => needles.every((n) =>
					[...document.querySelectorAll('[data-user-turn-id]')].some((el) => (el.textContent || '').includes(n)))
					&& !document.querySelector('.pending-messages [role="article"]'), needles, { timeout: 120_000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 90_000 });
				expectEachOnce(await done(), needles, log);
			}
		},
		{
			// #1052 "rapid start": the second message goes out the moment the first
			// shows as streaming, before any reply prose (the turn is still starting).
			name: 'rapid-start-each-shows-once',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const first = `tok-${uniq()}`;
				const second = `tok-${uniq()}`;
				const done = await watchExactlyOnce(page, [first, second]);
				await mark('send two at start');
				await quickSend(page, `first ${first} ${scriptTag(variant, 'slow', { tps: 10 })}`);
				await waitChat(page, { streaming: 'true' }, { timeout: 10_000 });
				await quickSend(page, `second ${second}`);
				await page.waitForFunction((needles) => needles.every((n) =>
					[...document.querySelectorAll('[data-user-turn-id]')].some((el) => (el.textContent || '').includes(n)))
					&& !document.querySelector('.pending-messages [role="article"]'), [first, second], { timeout: 120_000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 90_000 });
				expectEachOnce(await done(), [first, second], log);
			}
		},
		{
			// #1110: messages queued before the reply's first text. That text arrives
			// above the bubbles; they must not be pushed down by it with no input.
			name: 'queue-before-the-first-text-keeps-the-bubbles-still',
			async setup(ctx) {
				await openStopChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				// The first text waits long enough for a slow browser to queue both. Only Stop
				// ends the turn: a slow browser can paint the whole reply and the first queued
				// row's delivery as one frame, which would leave no frame of the reply to judge.
				await typeAndSend(page, `first tok-${uniq()} [script:slow latency_ms=7000 tps=300 hold_open=true]`);
				await waitChat(page, { streaming: 'true' });
				const texts = [`tok-${uniq()}`, `tok-${uniq()}`];
				await mark('queue before the text');
				for (const text of texts) {
					// Typing key by key can take a slow browser longer than the first text waits.
					await quickSend(page, `queued ${text}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
				}
				// Watch once both bubbles have settled: the second one arriving moves the
				// first, and on a slow browser that move can take more than one frame.
				await page.waitForTimeout(500);
				await page.evaluate(() => {
					const scores = (window.__bubbleShifts = []);
					new PerformanceObserver((list) => {
						for (const entry of list.getEntries()) {
							if (entry.hadRecentInput) continue;
							if (entry.sources.some((s) => s.node?.closest?.('.pending-messages'))) scores.push(Math.round(entry.value * 1000) / 1000);
						}
					}).observe({ type: 'layout-shift' });
				});
				const collect = await watchStopFrames(page, texts);
				expect((await page.getByText(SLOW_REPLY.start, { exact: false }).count()) === 0, 'both were queued and watched before the first text');
				await mark('the first text arrives');
				await page.getByText(SLOW_REPLY.end, { exact: false }).first().waitFor({ timeout: 60_000 });
				await page.waitForTimeout(300);
				const frames = (await collect()).filter((f) => f.messages.every((m) => m.bubbles === 1));
				expect(new Set(frames.map((f) => f.replyHeight)).size > 1, `the reply started while both bubbles were watched (${frames.length} frames)`);
				const scores = await page.evaluate(() => window.__bubbleShifts);
				await mark('stop');
				await stopButton(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				for (const text of texts) {
					await page.locator('[data-not-sent="true"]').filter({ hasText: text }).waitFor({ timeout: 15_000 });
				}
				const steps = frames.filter((f, i) => i === 0 || f.messages[0].top !== frames[i - 1].messages[0].top);
				log(`bubble top by time: ${steps.map((f) => `${f.at}ms ${f.messages[0].top} (scrollTop ${f.scrollTop}, reply ${f.replyHeight})`).join(', ')}`);
				log(`bubble shift scores: ${JSON.stringify(scores)}`);
				expect(frames.length > 5, `watched the bubbles while the reply started (${frames.length} frames)`);
				frames.forEach((frame, i) => frame.messages.forEach((message, m) => {
					const previous = frames[i - 1]?.messages[m];
					if (!previous || Math.abs(frame.composerTop - frames[i - 1].composerTop) >= 1) return;
					const moved = Math.abs(message.top - previous.top);
					expect(moved <= 1, `queued message ${m} stayed put as the reply started: moved ${moved}px in frame ${i} (${frame.at}ms)`);
				}));
				expect(scores.length === 0, `the reply's first text did not shift the bubbles: ${JSON.stringify(scores)}`);
			}
		},
		{
			// #863: a reader who scrolled up while a turn runs stays where they are
			// when the queued turn starts. Only a reader at the bottom follows it.
			name: 'queue-scrolled-up',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
				// Enough transcript above the live turn to scroll up into.
				await typeAndSend(ctx.page, `history tok-${uniq()} ${scriptTag(ctx.variant, 'long', { tps: 400 })}`);
				await waitTurnSettled(ctx.page, { text: 'Here is a long answer', messagesAtLeast: 2, timeout: 90_000 });
			},
			async act({ page, mark, variant, log }) {
				await mark('send the first');
				await typeAndSend(page, `first tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 5 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('queue the second');
				const second = `tok-${uniq()}`;
				await typeAndSend(page, `queued ${second} ${fakeAiTag(variant)}`.trim());
				await page.locator('.pending-messages [role="article"]').filter({ hasText: second }).waitFor({ timeout: 5000 });

				await mark('scroll up to read');
				await scrollTranscriptTo(page, 0.15);
				const before = await transcriptScroll(page);
				expect(!before.atBottom && before.firstRow, `scrolled up while the turn runs (${JSON.stringify(before)})`);

				await mark('the queued turn starts');
				await page.locator('[data-user-turn-id]').filter({ hasText: second }).first().waitFor({ state: 'attached', timeout: 90_000 });
				await page.waitForTimeout(1500);
				const after = await transcriptScroll(page);
				log(`before ${JSON.stringify(before)}, after ${JSON.stringify(after)}`);
				// Rows can be taller than the screen, so the same first row alone is not
				// proof: the scroll offset must hold too (new content grows below it).
				expect(
					!after.atBottom && after.firstRow === before.firstRow && Math.abs(after.top - before.top) <= 150,
					`the queued turn starting moved the reader (before ${JSON.stringify(before)}, after ${JSON.stringify(after)})`
				);
				await waitTurnSettled(page, { text: `You said: “queued ${second}`, messagesAtLeast: 6, timeout: 90_000 });
			}
		},
		{
			// Stop aborts queued messages on purpose (they must not run), but the
			// words must survive: each one becomes its own "Not sent (stopped)" row
			// that stays across a reload and never touches what is in the composer,
			// and restoring one keeps whatever is already typed there.
			name: 'stop-keeps-queued-text',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				const first = `tok-${uniq()}`;
				const second = `tok-${uniq()}`;
				const draft = `half-typed tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const composerText = () => page.locator('[contenteditable="true"]').first().innerText();

				await mark('send');
				await typeAndSend(page, `stream tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });

				await mark('queue two');
				await typeAndSend(page, `queued ${first}`);
				await page.locator('.pending-messages [role="article"]').filter({ hasText: first }).waitFor({ timeout: 5000 });
				await typeAndSend(page, `queued ${second}`);
				await page.locator('.pending-messages [role="article"]').filter({ hasText: second }).waitFor({ timeout: 5000 });

				await mark('type a draft');
				await page.locator('[contenteditable="true"]').first().click();
				await page.keyboard.type(draft);

				await mark('stop');
				await stopButton(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await stopButton(page).waitFor({ state: 'detached', timeout: 5000 });
				await notSent(first).waitFor({ timeout: 5000 });
				await notSent(second).waitFor({ timeout: 5000 });
				expect((await page.locator('.pending-messages [role="article"]').count()) === 0, 'no pending bubble left behind');
				expect((await composerText()).includes(draft), 'Stop left the draft in the composer alone');
				expect(!(await composerText()).includes(first), 'Stop did not write queued text into the composer');
				await page.getByText('[Stopped]', { exact: false }).first().waitFor({ timeout: 5000 });

				// Stay stopped: neither message may start a turn on its own.
				await page.waitForTimeout(3000);
				const state = await page.evaluate(() => document.querySelector('[data-ui-state="chat"]').getAttribute('data-ui-streaming'));
				expect(state === 'false', 'still not streaming 3s after Stop');
				expect((await page.getByText(`You said: “queued ${first}`, { exact: false }).count()) === 0, 'the queued message never ran');

				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				// A new step once the recorder has seen the old page go: the reload
				// itself is not the app going backwards; what boots next is judged.
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await notSent(first).waitFor({ timeout: 30_000 });
				await notSent(second).waitFor({ timeout: 5000 });
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				await page.getByText('[Stopped]', { exact: false }).first().waitFor({ timeout: 5000 });
				// A not-sent row must not take over the viewport: the reply it follows
				// stays on screen with it after a reload.
				await page.waitForTimeout(500);
				const onScreen = await page.evaluate(() => {
					const box = document.querySelector('.msg-scroll').getBoundingClientRect();
					const stopped = [...document.querySelectorAll('.msg-scroll *')].find((el) => !el.children.length && el.textContent.includes('[Stopped]'));
					const top = stopped.getBoundingClientRect().top;
					return top >= box.top && top < box.bottom;
				});
				expect(onScreen, 'the [Stopped] reply is in view after reload');

				// A composer that already holds text keeps it when a row is restored.
				await page.locator('[contenteditable="true"]').first().click();
				await page.keyboard.type(draft);

				await mark('restore one');
				await notSent(first).getByRole('button', { name: 'Restore to composer' }).click();
				await notSent(first).waitFor({ state: 'detached', timeout: 10_000 });
				await page.waitForFunction((t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), `queued ${first}`, { timeout: 10_000 });
				const merged = await composerText();
				log(`composer after restore: ${merged.replace(/\s+/g, ' ')}`);
				expect(merged.includes(draft), 'restoring kept the draft');
				expect((await notSent(second).count()) === 1, 'the other not-sent row is still there');

				await mark('reload after restore');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded after restore');
				await notSent(second).waitFor({ timeout: 30_000 });
				expect((await notSent(first).count()) === 0, 'a restored row stays gone after reload');
			},
			verify({ recording }) {
				expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') });
			}
		},
		{
			// The server drops a restored row whether or not this page hears back, so
			// the words must reach this chat's composer in every one of these cases.
			name: 'restore-survives-a-chat-switch-and-a-lost-reply',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const opening = `opening tok-${uniq()}`;
				const switched = `tok-${uniq()}`;
				const lost = `tok-${uniq()}`;
				const reloaded = `tok-${uniq()}`;
				const unheard = `tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const restoreButton = (text) => notSent(text).getByRole('button', { name: /^Restore/ });
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const REWIND = '**/rewind/execute/**';

				await mark('send');
				await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				const { title } = await api(page, 'GET', `/sessions/${await chatIdByToken(page, opening)}`);
				for (const words of [switched, lost, reloaded, unheard]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await mark('stop');
				await stopButton(page).click();
				await notSent(unheard).waitFor({ timeout: 15_000 });

				await mark('restore and leave before the reply');
				let release;
				const held = new Promise((done) => { release = done; });
				await page.route(REWIND, async (route) => { await held; await route.continue(); });
				await restoreButton(switched).click();
				await page.waitForFunction(() => [...document.querySelectorAll('[data-not-sent="true"] button')].some((b) => b.textContent.includes('Restoring')));
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				const elsewhere = page.waitForResponse(REWIND);
				release();
				await elsewhere;
				await page.unroute(REWIND);
				expect(!(await page.locator('[contenteditable="true"]').first().innerText()).includes(switched),
					'the words did not land in the chat on screen');

				await mark('back to the first chat');
				await openFromSidebar(page, title);
				await notSent(lost).waitFor({ timeout: 30_000 });
				await composerHas(`queued ${switched}`);
				await notSent(switched).waitFor({ state: 'detached', timeout: 15_000 });

				await mark('restore with a lost reply');
				await page.route(REWIND, async (route) => { await route.fetch(); await route.abort('connectionreset'); });
				await restoreButton(lost).click();
				await composerHas(`queued ${lost}`);
				await page.unroute(REWIND);
				await notSent(lost).waitFor({ state: 'detached', timeout: 15_000 });
				const composed = await page.locator('[contenteditable="true"]').first().innerText();
				expect(composed.includes(`queued ${switched}`), 'the earlier restored words are still in the composer');
				expect(composed.split(`queued ${lost}`).length === 2, 'the words arrived once');

				await mark('reload while the answer is still on its way');
				await page.route(REWIND, async (route) => {
					await route.fetch();
					await new Promise((done) => setTimeout(done, 60_000));
				});
				await restoreButton(reloaded).click();
				await page.waitForTimeout(1500);
				// Removing the route can deliver the held reply. Reload first so this
				// page really leaves before the restore answers, as this step requires.
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await notSent(unheard).waitFor({ timeout: 60_000 });
				await composerHas(`queued ${reloaded}`);
				expect((await notSent(reloaded).count()) === 0, 'the row the server dropped stayed gone');
				expect((await notSent(unheard).getByRole('button', { name: 'Restore to composer' }).count()) === 1, 'the other row was left alone');
				await page.waitForTimeout(1500);
				const afterReload = await page.locator('[contenteditable="true"]').first().innerText();
				expect(afterReload.split(`queued ${reloaded}`).length === 2, 'the words came back once');
				expect(afterReload.split(`queued ${lost}`).length <= 2, 'the earlier lost reply did not bring its words back again');

				await mark('restore the server never heard');
				await page.route(REWIND, (route) => route.abort('connectionreset'));
				await restoreButton(unheard).click();
				await composerHas(`queued ${unheard}`);
				await page.unroute(REWIND);
				await notSent(unheard).getByRole('alert').filter({ hasText: "Couldn't confirm the restore" }).waitFor({ timeout: 5000 });
				expect(await notSent(unheard).getByRole('button', { name: 'Restored' }).isDisabled(), 'a second click cannot copy the words again');
			}
		},
		{
			// #1031: a restore is already in the chat's saved draft by the time it
			// is done, so a reload has to bring it back, once, with whatever else was
			// in the composer. Boot is held so the composer always saves its empty
			// first state before boot says which chat this tab was on, the order that
			// used to wipe the draft. The Stop that stages the rows is unrecorded here:
			// stop-keeps-queued-text and the case above record it.
			name: 'restored-words-survive-a-reload-after-the-answer',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				const { page, variant } = ctx;
				await openNewChat(ctx);
				await page.waitForTimeout(800);
				ctx.rows = { lost: `tok-${uniq()}`, answered: `tok-${uniq()}`, unheard: `tok-${uniq()}` };
				await typeAndSend(page, `opening tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				for (const words of [ctx.rows.lost, ctx.rows.answered, ctx.rows.unheard]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await stopButton(page).click();
				await page.locator('[data-not-sent="true"]').filter({ hasText: ctx.rows.unheard }).waitFor({ timeout: 15_000 });
				await holdBootLoad(page, '/api/v1/preferences/all', 30_000, { releaseOnSignal: true });
			},
			async act({ page, mark, rows: { lost, answered, unheard } }) {
				const duringBoot = `mid-boot tok-${uniq()}`;
				const typed = `half-typed tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const restoreButton = (text) => notSent(text).getByRole('button', { name: /^Restore/ });
				const editor = () => page.locator('[contenteditable="true"]').first();
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const expectOnce = async (words) => {
					const composed = await editor().innerText();
					for (const text of words) expect(composed.split(text).length === 2, `"${text}" is in the composer once (${composed.replace(/\s+/g, ' ')})`);
				};
				// The composer is up and past its 100 ms draft save, and boot has not yet
				// said which chat it is on.
				const reloadWithBootHeld = async (name) => {
					await page.reload({ waitUntil: 'domcontentloaded' });
					await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
					await mark(name);
					await editor().waitFor({ timeout: 60_000 });
					await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function', null, { timeout: 60_000 });
					await page.waitForTimeout(400);
				};
				const finishBoot = async () => {
					await page.evaluate(() => window.__journeyBootHold.release());
					await notSent(unheard).waitFor({ timeout: 60_000 });
				};

				await mark('restore with a lost answer, reload at once, and type during boot');
				const REWIND = '**/rewind/execute/**';
				await page.route(REWIND, async (route) => { await route.fetch(); await route.abort('connectionreset'); });
				await restoreButton(lost).click();
				await composerHas(`queued ${lost}`);
				await reloadWithBootHeld('page reloaded after the lost answer');
				await page.unroute(REWIND);
				await editor().click();
				await page.keyboard.press('Control+End');
				await page.keyboard.type(duringBoot);
				await finishBoot();
				await composerHas(`queued ${lost}`);
				await composerHas(duringBoot);
				// Long enough for a second hand-back to have shown.
				await page.waitForTimeout(1500);
				await expectOnce([`queued ${lost}`, duringBoot]);
				expect((await notSent(lost).count()) === 0, 'the lost-answer row stayed gone');

				await mark('restore over a draft, and reload as the answer lands');
				await editor().click();
				// The click lands mid-text once the words wrap on a phone.
				await page.keyboard.press('Control+End');
				await page.keyboard.type(` ${typed}`);
				await restoreButton(answered).click();
				await composerHas(`queued ${answered}`);
				await reloadWithBootHeld('page reloaded after the answer');
				await finishBoot();
				await composerHas(`queued ${answered}`);
				await composerHas(typed);
				await page.waitForTimeout(1500);
				await expectOnce([`queued ${lost}`, `queued ${answered}`, duringBoot, typed]);
				expect((await notSent(answered).count()) === 0, 'the answered row stayed gone');
			}
		},
		{
			name: 'restore-finishing-with-no-composer-on-screen',
			allow: [{ kind: 'failed_requests', url: /\/commands\/rewind\/execute\//, reason: 'this case cuts the connection to a restore on purpose' }],
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const opening = `opening tok-${uniq()}`;
				const answered = `tok-${uniq()}`;
				const departed = `tok-${uniq()}`;
				const notSent = (text) => page.locator('[data-not-sent="true"]').filter({ hasText: text });
				const composerHas = (text) => page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), text, { timeout: 15_000 });
				const REWIND = '**/rewind/execute/**';
				const openSettings = async () => {
					const sessions = await showSidebar(page);
					await sessions.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Settings', exact: true }).click();
					await page.getByRole('navigation', { name: 'Settings' }).waitFor({ timeout: 15_000 });
				};
				// The server commits, then the answer is held until released and lost.
				const holdAnswer = async () => {
					let release;
					const held = new Promise((done) => { release = done; });
					const committed = new Promise((done) => {
						page.route(REWIND, async (route) => {
							await route.fetch();
							done();
							await held;
							await route.abort('connectionreset');
						});
					});
					return { release, committed };
				};
				const restoreBehindSettings = async (words, beforeRelease) => {
					const lost = page.waitForEvent('requestfailed', (r) => r.url().includes('/rewind/execute/'));
					const { release, committed } = await holdAnswer();
					await notSent(words).getByRole('button', { name: /^Restore/ }).click();
					await committed;
					await openSettings();
					expect((await page.locator('[contenteditable="true"]').count()) === 0, 'no composer is on screen');
					await beforeRelease?.();
					release();
					await lost;
					await page.unrouteAll({ behavior: 'ignoreErrors' });
				};

				await mark('send');
				await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				const { title } = await api(page, 'GET', `/sessions/${await chatIdByToken(page, opening)}`);
				for (const words of [answered, departed]) {
					await typeAndSend(page, `queued ${words}`);
					await page.locator('.pending-messages [role="article"]').filter({ hasText: words }).waitFor({ timeout: 5000 });
				}
				await mark('stop');
				await stopButton(page).click();
				await notSent(departed).waitFor({ timeout: 15_000 });

				await mark('the answer is lost while Settings is open');
				await restoreBehindSettings(answered);
				await mark('another chat and back');
				await page.waitForTimeout(300);
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				// Its own step: leaving Settings shows this chat first, so A, B, A is the route, not a flicker.
				await mark('back to the chat');
				await openFromSidebar(page, title);
				await notSent(departed).waitFor({ timeout: 30_000 });
				await composerHas(`queued ${answered}`);

				await mark('same, with the departure cancelled first');
				await restoreBehindSettings(departed, () => page.evaluate(() => window.dispatchEvent(new Event('beforeunload'))));
				// Past the wait for the page to go, when the words are handed over.
				await page.waitForTimeout(1500);
				await mark('another chat and back');
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
				await mark('back to the chat');
				await openFromSidebar(page, title);
				await composerHas(`queued ${departed}`);
				await notSent(departed).waitFor({ state: 'detached', timeout: 30_000 });
				const composed = await page.locator('[contenteditable="true"]').first().innerText();
				expect(composed.split(`queued ${answered}`).length === 2, 'the first words arrived once');
				expect(composed.split(`queued ${departed}`).length === 2, 'the second words arrived once');
				expect(await page.evaluate(() => !Object.keys(sessionStorage).some((k) => k.startsWith('restore-note:'))), 'no note is left over');
			}
		},
		{
			// A note is planted here, because a chat cannot be stopped down to a
			// single not-sent row from the UI. What it proves is the next page's
			// side: a chat with no rows at all, whose layout has no transcript.
			name: 'reload-after-restoring-the-only-row',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, status }) {
				const words = `only row tok-${uniq()}`;
				const chatId = seededChatId(status, 'cleared');
				const composer = page.locator('[contenteditable="true"]').first();

				await mark('open the empty chat');
				await openFromSidebar(page, SEEDED.cleared);
				await waitChat(page, { surface: 'cleared', loading: 'false' });
				await page.evaluate(([id, text]) => sessionStorage.setItem(`restore-note:${id}:only-row`,
					JSON.stringify({ rowId: 'only-row', prompt: text, uploadPaths: [], page: 'an-earlier-page' })), [chatId, words]);

				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
				await mark('page reloaded');
				await page.waitForFunction(
					(t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), words, { timeout: 30_000 });
				await waitChat(page, { surface: 'cleared', loading: 'false' });
				await page.waitForTimeout(1500);
				expect((await composer.innerText()).split(words).length === 2, 'the words came back once');
				expect(await page.evaluate((id) => sessionStorage.getItem(`restore-note:${id}:only-row`) === null, chatId), 'the note is used up');

				// This is a shared seeded chat: leave its composer as it was found.
				await mark('clear the composer');
				await composer.click();
				await page.keyboard.press('ControlOrMeta+A');
				await page.keyboard.press('Backspace');
				await page.waitForTimeout(1000);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
};

// The cases queue-while-busy.journey.mjs runs: the seven its hosted measurement timed.
// queue-while-busy-2.journey.mjs runs every other one, so a new case lands there.
export const FIRST_HALF = [
	'queue',
	'queue-scrolled-up',
	'stop-keeps-queued-text',
	'restore-survives-a-chat-switch-and-a-lost-reply',
	'restored-words-survive-a-reload-after-the-answer',
	'restore-finishing-with-no-composer-on-screen',
	'reload-after-restoring-the-only-row'
];
