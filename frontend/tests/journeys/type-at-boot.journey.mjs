// #811: text typed on a fresh page just before boot finishes its first loads
// must survive boot's New Chat reconciliation (which can swap the composer)
// and go out on Send. Preferences are held so the typing lands before boot
// reconciles; the hold is let go right after typing, inside the composer's
// 100 ms editor-to-store draft debounce, where the text was lost.
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	composer, countUserRows, expect, gotoApp, holdBootLoad, sendButton, settleServiceWorker, uniq, waitChat, waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { waitForBootReconciled } from './lib/boot.mjs';

const BOOT_HOLD = { path: '/api/v1/preferences/all', ms: 20_000 };

await runJourney({
	name: 'type-at-boot',
	cases: [
		{ name: 'type-then-boot-finishes' },
		// Draft survival is not a boot-speed assertion. Keep the original
		// debounce overlap above and also cover reconciliation slower than 2 s.
		{ name: 'type-then-slow-boot-finishes', initDelayMs: 3000, viewports: ['phone'], throttles: ['slow'] }
	].map(({ name, initDelayMs = 0, ...variants }) => ({
		name,
		...variants,
		async setup({ page, status }) {
			// The service worker's first-visit claim reload is #908, not this bug.
			await gotoApp(page, status);
			await settleServiceWorker(page, status);
			if (initDelayMs) {
				// Install before the hold: its release calls this fetch wrapper,
				// so preferences remain pending after release, as on a slow host.
				await page.addInitScript(({ path, ms }) => {
					const original = window.fetch.bind(window);
					let delayed = false;
					window.fetch = async (input, init) => {
						const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
						const url = new URL(typeof input === 'string' ? input : input.url, location.href);
						if (!delayed && method === 'GET' && url.pathname === path) {
							delayed = true;
							await new Promise(resolve => setTimeout(resolve, ms));
						}
						return original(input, init);
					};
				}, { path: BOOT_HOLD.path, ms: initDelayMs });
			}
			await holdBootLoad(page, BOOT_HOLD.path, BOOT_HOLD.ms, { releaseOnSignal: true });
			// A first visit has no saved layout: the page opens on the default
			// landing, and boot then opens New Chat explicitly.
			await page.addInitScript(() => localStorage.removeItem('ai-shuffle-layout'));
			await page.goto(new URL('/', status.urls.ui).toString());
			// Only wait for the composer and the held load: never for boot to settle.
			await composer(page).waitFor({ timeout: 90_000 });
			await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function', null, { timeout: 60_000 });
		},
		async act(ctx) {
			const { page, mark, variant, log } = ctx;
			const token = `tok-${uniq()}`;
			const text = `typed at boot ${token} ${fakeAiTag(variant)}`.trim();
			const hold = await page.evaluate(() => window.__journeyBootHold);
			expect(hold?.releasedAt === null, `boot is still awaiting preferences when typing starts: ${JSON.stringify(hold)}`);
			await mark('type while boot still loads');
			await composer(page).fill(text);
			// Release inside the draft debounce so fast reconciliation can overlap
			// it; the delayed case separately covers survival through a slow init.
			await page.evaluate(() => window.__journeyBootHold.release());
			await waitForBootReconciled(page, { timeout: BOOT_HOLD.ms });
			if (initDelayMs) {
				const timing = await page.evaluate(() => ({
					releasedAt: window.__journeyBootHold?.releasedAt,
					reconciledAt: performance.getEntriesByName('shuffle:boot.reconciled')[0]?.startTime
				}));
				const elapsed = timing.reconciledAt - timing.releasedAt;
				log(`slow-boot timing: ${JSON.stringify({ ...timing, elapsed })}`);
				expect(Number.isFinite(timing.releasedAt) && Number.isFinite(timing.reconciledAt) && elapsed > 2000,
					`invalid slow-boot run: reconciliation must exceed 2000 ms after release: ${JSON.stringify({ ...timing, elapsed })}`);
			}
			const kept = ((await composer(page).innerText()) || '').trim();
			expect(kept.includes(token), `the typed text survives boot finishing: composer shows ${JSON.stringify(kept)}`);
			await mark('send');
			await sendButton(page).click();
			await waitTurnSettled(page, { text: 'Fake AI here', messagesAtLeast: 2 });
			await waitChat(page, { surface: 'active', session: /^s:/, streaming: 'false', messagesAtLeast: 2 }, { timeout: 5000 });
			expect(await countUserRows(page, token) === 1, 'the typed message is sent exactly once');
			log('text typed before boot finished survived it and was sent');
		}
	})),
	allow: [...DEV_INSTANCE_NOISE]
});
