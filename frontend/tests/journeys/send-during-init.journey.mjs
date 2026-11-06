// #821: a first send while init awaits preferences owns the chat, not the
// saved New Chat view. Hold preferences; one additional case holds tags too.
// #930: a project picked on a fresh browser's new-session screen while init
// awaits preferences is still the chat's project after boot reconciles.
import { fakeAiTag, runJourney, VIEWPORTS } from './runner.mjs';
import {
	PROJECT, composer, countUserRows, expect, holdBootLoad, openNewChat, sendButton,
	settleServiceWorker, showSidebar, typeAndSend, uniq, waitChat, waitTurnSettled, withChatSetup
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { waitForBootReconciled, waitForCreatedChat } from './lib/boot.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const BOOT_HOLD = { path: '/api/v1/preferences/all', ms: 12_000 };
if (process.argv.includes('--review-desktop')) VIEWPORTS.desktop = { viewport: { width: 1280, height: 800 } };
const privateScreenshot = async (page, variant, stage) => {
	if (process.env.JOURNEY_OUT) await page.screenshot({ path: `${process.env.JOURNEY_OUT}/${stage}@${variant.viewport}-${variant.throttle.cpu}x.png` });
};

await runJourney({
	name: 'send-during-init',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [...[
		'first-send', 'first-send-late-tags', 'first-send-reload-before-reconciliation',
		'new-chat-after-init-reload-before-reconciliation', 'bootstrap-reset-reload-before-reconciliation'
	].map(name => ({
		name,
		async setup(ctx) {
			const { page, status } = ctx;
			await openNewChat(ctx);
			await settleServiceWorker(page, status);
			if (name === 'bootstrap-reset-reload-before-reconciliation') {
				// Produce both independent snapshots through the real UI. Replay the
				// older New Chat layout beside the newly produced tab chat to model a
				// stale layout write: bootstrap must execute layoutNewChatWon itself.
				const newChatLayout = await page.evaluate(() => localStorage.getItem('ai-shuffle-layout'));
				expect(JSON.parse(newChatLayout)?.activeView?.type === 'session' && JSON.parse(newChatLayout).activeView.sessionId === null,
					'the stale New Chat layout snapshot was written by the actual UI');
				await typeAndSend(page, `old chat before bootstrap reset ${uniq()} ${fakeAiTag(ctx.variant)}`.trim());
				await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2 });
				await waitForCreatedChat(page);
				ctx.staleSession = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ai-shuffle-active-session')).sessionId);
				await page.evaluate(snapshot => localStorage.setItem('ai-shuffle-layout', snapshot), newChatLayout);
			}
			if (name !== 'first-send') {
				// A possible late independent load, NOT the cause of #984: its hosted
				// tags response was already in. Keep the real response.
				// Intercept the page's real fetch, including a service-worker response.
				await page.addInitScript(() => {
					if (sessionStorage.getItem('__journeyLateTagsDone')) return;
					const hold = window.__journeyLateTags = { startedAt: null, releasedAt: null };
					const original = window.fetch.bind(window);
					window.fetch = async (input, init) => {
						const url = new URL(typeof input === 'string' ? input : input.url, location.href);
						if ((init?.method || input?.method || 'GET').toUpperCase() !== 'GET'
							|| url.pathname !== '/api/v1/tags' || hold.startedAt !== null) return original(input, init);
						hold.startedAt = performance.now();
						const response = await original(input, init);
						await new Promise(resolve => {
							const release = () => {
								clearTimeout(safety);
								sessionStorage.setItem('__journeyLateTagsDone', 'true');
								hold.releasedAt = performance.now();
								resolve();
							};
							const safety = setTimeout(release, 20_000);
							hold.release = () => setTimeout(release, 3000);
						});
						return response;
					};
				});
			}
			await holdBootLoad(page, BOOT_HOLD.path, BOOT_HOLD.ms, { releaseOnSignal: true });
			ctx.closed = [];
			page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
				try {
					const frame = JSON.parse(String(payload));
					if (frame.type === 'close_session') ctx.closed.push(frame);
				} catch { /* non-JSON transport frame */ }
			}));
			await page.reload({ waitUntil: 'domcontentloaded' });
			// Only wait for the composer to exist: never wait for boot to settle.
			await composer(page).waitFor({ timeout: 60_000 });
			ctx.initGeneration = await page.evaluate(() => Number(sessionStorage.getItem('__aihub_initGeneration')));
			if (name === 'bootstrap-reset-reload-before-reconciliation') {
				// Establish the intended loading -> New Chat reset before recording
				// the send/reload journey. Its setup motion is not a send reversal.
				await page.evaluate(() => window.__journeyBootHold.release());
				await page.waitForFunction(() => performance.getEntriesByName('shuffle:boot.init.end').length > 0
					&& typeof window.__journeyLateTags?.release === 'function', null, { timeout: BOOT_HOLD.ms });
				await waitChat(page, { surface: 'new', session: 'none', loading: 'false' });
				await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
			}
		},
		async act(ctx) {
			const { page, mark, variant, log } = ctx;
			const token = `tok-${uniq()}`;
			const afterReset = name === 'new-chat-after-init-reload-before-reconciliation' || name === 'bootstrap-reset-reload-before-reconciliation';
			if (afterReset) {
				if (name === 'new-chat-after-init-reload-before-reconciliation') {
					await page.evaluate(() => window.__journeyBootHold.release());
					await page.waitForFunction(() => performance.getEntriesByName('shuffle:boot.init.end').length > 0
						&& typeof window.__journeyLateTags?.release === 'function', null, { timeout: BOOT_HOLD.ms });
					await mark('explicit New chat after init while tags remain held');
					await showSidebar(page);
					await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New chat', exact: true }).click();
				}
				await waitChat(page, { surface: 'new', session: 'none', loading: 'false' });
				const reset = await page.evaluate(() => ({
					generation: Number(sessionStorage.getItem('__aihub_initGeneration')),
					reconciled: performance.getEntriesByName('shuffle:boot.reconciled').length > 0,
					tagsReleased: window.__journeyLateTags.releasedAt
				}));
				expect(reset.generation > ctx.initGeneration && !reset.reconciled && reset.tagsReleased === null,
					`New Chat reset advanced the generation before reconciliation: ${JSON.stringify(reset)}`);
				ctx.closedAtSend = ctx.closed.length;
				log(`reset before send: ${JSON.stringify({ ...reset, initGeneration: ctx.initGeneration, staleSession: ctx.staleSession })}`);
			}
			await mark('send immediately during init');
			await composer(page).fill(`during init ${token} ${fakeAiTag(variant)}`.trim());
			await sendButton(page).click();
			const hold = await page.evaluate(() => window.__journeyBootHold);
			expect(hold?.startedAt != null && (afterReset ? hold.releasedAt !== null : hold.releasedAt === null),
				`send occurs in the intended preference/init window: ${JSON.stringify(hold)}`);
			await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2, hold: 0 });
			const beforeRelease = await page.evaluate(() => window.__journeyBootHold);
			if (afterReset) expect(await page.evaluate(() => window.__journeyLateTags.releasedAt === null), 'the reset turn settled while tags still held reconciliation');
			else expect(beforeRelease.releasedAt === null, 'the first turn finished before preference hydration (not just isStreaming protection)');
			if (name === 'first-send') {
				// #984/#1063: a settled first send must already persist its created
				// chat, independent of how long the remaining boot reads take.
				await waitForCreatedChat(page);
			}
			if (!afterReset) {
				await mark('preference hydration finishes');
				await page.evaluate(() => window.__journeyBootHold.release());
			}
			if (name.endsWith('reload-before-reconciliation')) {
				// #1063: reload after init but BEFORE the independent boot loads
				// reconcile. Keep the real tags response held in this document only.
				await page.waitForFunction(() => performance.getEntriesByName('shuffle:boot.init.end').length > 0
					&& typeof window.__journeyLateTags?.release === 'function', null, { timeout: BOOT_HOLD.ms });
				const beforeReload = await page.evaluate(() => ({
					view: JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView,
					session: JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId,
					reconciled: performance.getEntriesByName('shuffle:boot.reconciled').length > 0,
					tagsReleased: window.__journeyLateTags.releasedAt
				}));
				expect(!beforeReload.reconciled && beforeReload.tagsReleased === null, 'reload occurs inside the held reconciliation window');
				log(`settled first send before reconciliation: ${JSON.stringify(beforeReload)}`);
				const session = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
				await privateScreenshot(page, variant, afterReset ? `${name}-settled-before-reconciliation` : 'settled-before-reconciliation');
				// Do not hold tags in the next document; it must fully reconcile so an
				// empty New Chat cannot be mistaken for a merely unfinished restore.
				await page.evaluate(() => sessionStorage.setItem('__journeyLateTagsDone', 'true'));
				await mark('reload the created chat before reconciliation');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
				await mark('page reloaded');
				await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function', null, { timeout: 30_000 });
				await page.evaluate(() => window.__journeyBootHold.release());
				await waitForBootReconciled(page, { timeout: BOOT_HOLD.ms });
				// Boot selection and transcript hydration have different milestones.
				// Empty New Chat also settles loading=false, so the pre-fix failure
				// still reaches the exact identity/count assertions rather than hiding.
				await waitChat(page, { loading: 'false', streaming: 'false' });
				const reloaded = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
				const count = await countUserRows(page, token);
				log(`reconciled reload: signature=${reloaded}, first-message rows=${count}`);
				await privateScreenshot(page, variant, afterReset ? `${name}-reloaded-before-reconciliation` : 'reloaded-before-reconciliation');
				expect(count === 1, `reload before reconciliation reopens the first message exactly once, got ${count}`);
				expect(reloaded === session, `reload before reconciliation reopens the created chat: ${reloaded}`);
				expect(ctx.closed.length === (ctx.closedAtSend ?? 0), 'reload must not close the boot-send-owned created chat');
				return;
			}
			if (name === 'first-send-late-tags') {
				await page.waitForFunction(() => typeof window.__journeyLateTags?.release === 'function');
				await page.evaluate(() => window.__journeyLateTags.release());
			}
			await page.waitForFunction(() => window.__journeyBootHold?.releasedAt != null, null, { timeout: BOOT_HOLD.ms });
			if (name === 'first-send-late-tags') {
				await page.waitForFunction(() => window.__journeyLateTags?.releasedAt != null, null, { timeout: 5000 });
			}
			// Boot can still await init after the HTTP responses have arrived.
			// Observe reconciliation itself; its hosted delay is not yet attributed.
			await waitForCreatedChat(page);
			expect(ctx.closed.length === 0, `boot must not send close_session: ${JSON.stringify(ctx.closed)}`);
			await waitChat(page, { surface: 'active', session: /^s:/, streaming: 'false', messagesAtLeast: 2 }, { timeout: 5000 });
			expect(await countUserRows(page, token) === 1, 'the first message stays in the transcript exactly once');
			log(`first send and settled reply completed while preferences were held; released explicitly, no close_session`);
			const session = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
			await mark('reload the created chat');
			await page.reload({ waitUntil: 'domcontentloaded' });
			// A reload is an intentional document replacement, not a boot reversal.
			await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
			await mark('page reloaded');
			// Only the first send is under test here; release the reload's hold
			// as soon as its preference request starts, without spending a timer.
			await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function', null, { timeout: 30_000 });
			await page.evaluate(() => window.__journeyBootHold.release());
			await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2 });
			expect(await countUserRows(page, token) === 1, 'reloading reopens this chat with its first message');
			const reloaded = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
			expect(reloaded === session, `reload reopened the created chat, not an old one: ${reloaded}`);
			expect(ctx.closed.length === 0, 'reload must not close the created chat either');
		},
		verify: ({ recording }) => expectStopFollowsStreaming(recording, { from: markTime(recording, 'send immediately during init') })
	})), {
		name: 'pick-project',
		async setup(ctx) {
			const { page } = ctx;
			// A fresh browser: no saved layout view, so boot's landing reconcile
			// decides what the new-session screen becomes.
			await holdBootLoad(page, BOOT_HOLD.path, BOOT_HOLD.ms, { releaseOnSignal: true });
			ctx.queries = [];
			page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
				try {
					const frame = JSON.parse(String(payload));
					if (frame.type === 'query') ctx.queries.push(frame);
				} catch { /* non-JSON transport frame */ }
			}));
			await openNewChat(ctx, { project: PROJECT });
		},
		async act(ctx) {
			const { page, mark, variant, log } = ctx;
			const hold = await page.evaluate(() => window.__journeyBootHold);
			expect(hold?.startedAt != null && hold.releasedAt === null, `the project was picked while init still awaited preferences: ${JSON.stringify(hold)}`);
			await mark('preference hydration finishes');
			await page.evaluate(() => window.__journeyBootHold.release());
			// The landing reconcile ends by saving the layout's view: wait for that.
			await page.waitForFunction(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView != null,
				null, { timeout: BOOT_HOLD.ms + 30_000 });
			const picked = page.getByRole('button', { name: `Workspace folders: ${PROJECT}` });
			// In place on a wide screen, in Chat controls on a phone.
			expect(await withChatSetup(page, () => picked.isVisible()), 'the picked project is still on the new-session screen after boot reconciled');
			await mark('send');
			await typeAndSend(page, `picked during init tok-${uniq()} ${fakeAiTag(variant)}`.trim());
			await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2, hold: 0 });
			const projects = ctx.queries.map(frame => frame.project ?? null);
			log(`query project(s): ${JSON.stringify(projects)}`);
			expect(projects.length === 1 && projects[0] === 'dev-sandbox', `the first send carries the picked project: ${JSON.stringify(projects)}`);
		}
	}],
	allow: [...DEV_INSTANCE_NOISE]
});
