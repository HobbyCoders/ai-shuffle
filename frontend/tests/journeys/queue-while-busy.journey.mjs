// Journey: send a second message while the first reply is still streaming.
// The second message must show as queued/pending right away (the user can see
// it was taken), be delivered exactly once after the first turn, and get its
// own reply. At no painted frame may it show twice (queued bubble and
// delivered row together, or two rows), and nothing may remount.
import { fakeAiTag, runJourney } from './runner.mjs';
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

await runJourney({
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
});
