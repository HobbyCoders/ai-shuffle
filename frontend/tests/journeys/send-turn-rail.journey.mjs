// Sending must not move the turn rail, including when the composer gives height
// back: its box sets the transcript's height, and a rail placed from that box
// followed it (#1094). chat-position-stability's own send is one short line and
// never changes the composer's height, so these two sends live here.
import { runJourney } from './runner.mjs';
import { composer, expect, gotoApp, seededChatId, sendButton, stopButton, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { mobileShell, openStopChat, queueDuringASlowTurn } from './lib/stop-queue.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

async function openHistory({ page, status, variant }) {
	const shell = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	await gotoApp(page, status, `${shell}?session=${seededChatId(status, 'no_project')}`);
	await waitChatOpen(page, { messagesAtLeast: 2 });
}

async function restoreNotSentRowIntoADraft({ page, mark, variant }) {
	const [restored] = await queueDuringASlowTurn(page, mark, variant, 2);
	await mark('stop');
	await stopButton(page).click();
	await waitChat(page, { streaming: 'false' });
	const row = page.locator('[data-not-sent="true"]').filter({ hasText: restored });
	await row.waitFor({ timeout: 5000 });
	await composer(page).click();
	await page.keyboard.type('and this too [script:default]');
	await mark('restore into the draft');
	await row.getByRole('button', { name: 'Restore to composer' }).click();
	await row.waitFor({ state: 'detached', timeout: 10_000 });
	await page.waitForFunction((t) => document.querySelector('[contenteditable="true"]')?.innerText.includes(t), `queued ${restored}`, { timeout: 10_000 });
}

/** Rail and composer on every painted frame. The IntersectionObserver entry is
 * the geometry the browser settled on for that frame, after the app's own work. */
async function watchFrames(page) {
	await page.evaluate(() => {
		const watch = (window.__railFrames = { running: true, frames: [] });
		const sample = () => {
			if (!watch.running) return;
			const elements = { rail: document.querySelector('nav.turn-nav'), composer: document.querySelector('.input-slot') };
			const observer = new IntersectionObserver((entries) => {
				observer.disconnect();
				const top = (el) => entries.find((entry) => entry.target === el)?.boundingClientRect.top ?? null;
				watch.frames.push({ rail: top(elements.rail), composer: top(elements.composer) });
			});
			for (const el of Object.values(elements)) if (el) observer.observe(el);
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	});
	// Include the resting frame before the action, not just its aftermath.
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
	return () => page.evaluate(() => {
		window.__railFrames.running = false;
		return window.__railFrames.frames;
	});
}

const present = (frames, key) => frames.map((frame) => frame[key]).filter(Number.isFinite);
const reversals = (tops) => tops.slice(1).reduce((state, top, i) => {
	const direction = Math.sign(Math.round(top - tops[i]));
	if (direction && state.direction && direction !== state.direction) state.count++;
	if (direction) state.direction = direction;
	return state;
}, { direction: 0, count: 0 }).count;

const sendCase = ({ name, setup, prepare }) => ({
	name, setup,
	async act(ctx) {
		const { page, mark, log } = ctx;
		await prepare(ctx);
		await page.waitForTimeout(500);
		await mark('send');
		const stop = await watchFrames(page);
		const messagesBefore = Number(await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
		await sendButton(page).click();
		await waitTurnSettled(page, { messagesAtLeast: messagesBefore + 2, hold: 3000 });
		const frames = await stop();
		const rail = present(frames, 'rail');
		const tops = present(frames, 'composer');
		const travel = rail.length ? Math.max(...rail) - Math.min(...rail) : Infinity;
		log(`send frames ${JSON.stringify({ frames: frames.length, rail: travel, composer: Math.max(...tops) - Math.min(...tops), reversals: reversals(tops) })}`);
		expect(travel <= 2, `sending keeps the turn rail still (${travel.toFixed(1)}px)`);
		expect(reversals(tops) === 0, `clearing the draft moves the composer at most once (${reversals(tops)} reversals)`);
	}
});

await runJourney({
	name: 'send-turn-rail',
	cases: [
		sendCase({
			name: 'wrapped-send', setup: openHistory,
			prepare: ({ page }) => composer(page).fill('please read the deployment notes again and then list every question that is still open for me [script:default]')
		}),
		// The words of a "Not sent" row join a typed draft, and both go at once.
		sendCase({ name: 'restored-row-send', setup: openStopChat, prepare: restoreNotSentRowIntoADraft })
	],
	allow: [...DEV_INSTANCE_NOISE]
});
