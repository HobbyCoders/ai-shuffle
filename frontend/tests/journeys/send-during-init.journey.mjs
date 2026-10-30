// #821: a first send while init awaits preferences owns the chat, not the
// saved New Chat view. Hold preferences, not the later tags/catalog loads.
// #930: a project picked on a fresh browser's new-session screen while init
// awaits preferences is still the chat's project after boot reconciles.
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	PROJECT, composer, countUserRows, expect, holdBootLoad, openNewChat, sendButton,
	settleServiceWorker, typeAndSend, uniq, waitChat, waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const BOOT_HOLD = { path: '/api/v1/preferences/all', ms: 12_000 };

await runJourney({
	name: 'send-during-init',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [{
		name: 'first-send',
		async setup(ctx) {
			const { page, status } = ctx;
			await openNewChat(ctx);
			await settleServiceWorker(page, status);
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
		},
		async act(ctx) {
			const { page, mark, variant, log } = ctx;
			const token = `tok-${uniq()}`;
			await mark('send immediately during init');
			await composer(page).fill(`during init ${token} ${fakeAiTag(variant)}`.trim());
			await sendButton(page).click();
			const hold = await page.evaluate(() => window.__journeyBootHold);
			expect(hold?.startedAt != null && hold.releasedAt === null, `init is still awaiting preferences at Send: ${JSON.stringify(hold)}`);
			await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2, hold: 0 });
			const beforeRelease = await page.evaluate(() => window.__journeyBootHold);
			expect(beforeRelease.releasedAt === null, 'the first turn finished before preference hydration (not just isStreaming protection)');
			await mark('preference hydration finishes');
			await page.evaluate(() => window.__journeyBootHold.release());
			await page.waitForFunction(() => window.__journeyBootHold?.releasedAt != null, null, { timeout: BOOT_HOLD.ms + 30_000 });
			await page.waitForTimeout(2000);
			expect(ctx.closed.length === 0, `boot must not send close_session: ${JSON.stringify(ctx.closed)}`);
			await waitChat(page, { surface: 'active', session: /^s:/, streaming: 'false', messagesAtLeast: 2 }, { timeout: 5000 });
			expect(await countUserRows(page, token) === 1, 'the first message stays in the transcript exactly once');
			const view = await page.evaluate(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView);
			expect(view?.type === 'session' && typeof view.sessionId === 'string', `layout follows the created chat: ${JSON.stringify(view)}`);
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
	}, {
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
			expect(await picked.isVisible(), 'the picked project is still on the new-session screen after boot reconciled');
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
