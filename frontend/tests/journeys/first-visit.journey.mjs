// Journey: a fresh browser's first visit loads the app exactly once.
//
// Guards #908: in a secure context (CI's loopback) the first load installs
// /sw.js, which claims the page and posts SW_UPDATED. That first claim is not
// an update: on the build the page already runs it must not reload the page
// or show the "App updated" banner. Every journey case starts in a fresh
// browser context, so this reload used to abort boot requests and restart
// initialization under other journeys too.
//
// Cases:
//   no-reload   load the app, let the service worker install and take
//               control, and check the page loaded one document, with no banner
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/** After the claim: long enough for the version check and any reload it would start. */
const AFTER_CLAIM_MS = 3000;

await runJourney({
	name: 'first-visit',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'no-reload',
		viewports: ['desktop'],
		throttles: ['slow'],
		async setup(ctx) {
			const { page, status } = ctx;
			// Count documents, not `framenavigated` (the router's same-document
			// history updates fire it too): every new top document runs this.
			await page.addInitScript(() => {
				if (window !== window.top) return;
				const n = Number(sessionStorage.getItem('journey-first-visit-documents') || 0) + 1;
				sessionStorage.setItem('journey-first-visit-documents', String(n));
			});
			await gotoApp(page, status);
			// The claim lands here, unrecorded: the boot's own early remount
			// (#811's subject, not this journey's) stays out of the recording,
			// and the document count still sees any reload the claim causes.
			const waitForWorker = () => page.evaluate(async () => {
				if (!window.isSecureContext || !('serviceWorker' in navigator)) return 'no-worker';
				const deadline = performance.now() + 20_000;
				while (!navigator.serviceWorker.controller && performance.now() < deadline) {
					await new Promise((resolve) => setTimeout(resolve, 50));
				}
				return navigator.serviceWorker.controller ? 'controlled' : 'uncontrolled';
			});
			// A reload mid-wait destroys the evaluation's context: ask the new document.
			ctx.worker = await waitForWorker().catch(() => waitForWorker());
			if (ctx.worker === 'controlled') await page.waitForTimeout(1000);
		},
		async act(ctx) {
			const { page, mark, log } = ctx;
			if (ctx.worker === 'no-worker') {
				log('no service worker here (not a secure context): nothing to check');
				return;
			}
			expect(ctx.worker === 'controlled', 'the service worker took control of the first visit');
			await mark('the service worker controls the page; nothing reloads after its claim');
			await page.waitForTimeout(AFTER_CLAIM_MS);
			await mark('settled');
		},
		async verify(ctx) {
			const { page, log } = ctx;
			if (ctx.worker === 'no-worker') return;
			const documents = await page.evaluate(() => Number(sessionStorage.getItem('journey-first-visit-documents')));
			expect(documents === 1, `the first visit loaded once, not ${documents} times (#908)`);
			const banner = await page.getByText(/App updated/).count();
			expect(banner === 0, 'no "App updated" banner on the build the page already runs');
			log('one navigation, no update banner');
		}
	}]
});
