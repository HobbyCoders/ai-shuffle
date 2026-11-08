// Sending must not move the turn rail, including when the composer gives height
// back: its box sets the transcript's height, and a rail placed from that box
// followed it (#1094). chat-position-stability's own send is one short line and
// never changes the composer's height, so these two sends live here. The rail's
// position comes from the visible height, so a keyboard or a landscape phone must
// keep it inside the transcript too.
import { runJourney } from './runner.mjs';
import { composer, expect, gotoApp, seededChatId, sendButton, stopButton, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { mobileShell, openStopChat, queueDuringASlowTurn } from './lib/stop-queue.mjs';
import { movements, transcriptMovements } from './lib/frame-movement.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const LOG = '[role="log"][aria-label="Conversation messages"]';

const openChat = (key) => async ({ page, status, variant }) => {
	const shell = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	await gotoApp(page, status, `${shell}?session=${seededChatId(status, key)}`);
	await waitChatOpen(page, { messagesAtLeast: 2 });
};
const openHistory = openChat('no_project');

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

/** Rail, composer and the row being read on every painted frame. The
 * IntersectionObserver entry is the geometry the browser settled on for that
 * frame, after the app's own work. */
async function watchFrames(page) {
	await page.evaluate((selector) => {
		const log = document.querySelector(selector);
		const logTop = log.getBoundingClientRect().top;
		const row = [...log.querySelectorAll('[data-transcript-key]')].find((el) => el.getBoundingClientRect().bottom > logTop + 1);
		const watch = (window.__railFrames = { running: true, next: 0, frames: [] });
		const sample = () => {
			if (!watch.running) return;
			const elements = { rail: document.querySelector('nav.turn-nav'), composer: document.querySelector('.input-slot'), row };
			const seq = watch.next++;
			const observer = new IntersectionObserver((entries) => {
				observer.disconnect();
				const top = (el) => entries.find((entry) => entry.target === el)?.boundingClientRect.top ?? null;
				watch.frames.push({ seq, rail: top(elements.rail), composer: top(elements.composer), row: top(elements.row), content: log.scrollHeight });
			});
			for (const el of Object.values(elements)) if (el?.isConnected) observer.observe(el);
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	}, LOG);
	// Include the resting frame before the action, not just its aftermath.
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
	return () => page.evaluate(() => {
		window.__railFrames.running = false;
		// Slow frames can deliver their observers out of order.
		return window.__railFrames.frames.sort((a, b) => a.seq - b.seq);
	});
}

const present = (frames, key) => frames.map((frame) => frame[key]).filter(Number.isFinite);

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
		const travel = rail.length ? Math.max(...rail) - Math.min(...rail) : Infinity;
		const composerMoves = movements(present(frames, 'composer'));
		const transcriptMoves = transcriptMovements(frames);
		expect(present(frames, 'row').length >= 2, 'the transcript row was measured through the send');
		log(`send frames ${JSON.stringify({ frames: frames.length, rail: travel, composerMoves, transcriptMoves })}`);
		expect(travel <= 2, `sending keeps the turn rail still (${travel.toFixed(1)}px)`);
		expect(composerMoves <= 1, `clearing the draft moves the composer at most once (${composerMoves} movements)`);
		expect(transcriptMoves <= 1, `the composer shrinking moves the transcript at most once (${transcriptMoves} movements)`);
	}
});

/** What a phone keyboard does: only the visual viewport shrinks, and the app's
 * own listener resizes the shell from it. Headless Chromium has no keyboard. */
async function showVisibleHeight(page, height) {
	await page.evaluate((visible) => {
		Object.defineProperty(window.visualViewport, 'height', { configurable: true, get: () => visible ?? window.innerHeight });
		window.visualViewport.dispatchEvent(new Event('resize'));
	}, height);
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
}

const railGeometry = (page) => page.evaluate((selector) => {
	const box = (query) => document.querySelector(query).getBoundingClientRect();
	const [rail, log, composer] = [box('.turn-rail'), box(selector), box('.input-slot')];
	return { railTop: rail.top, railBottom: rail.bottom, logTop: log.top, composerTop: composer.top };
}, LOG);

const short = [
	{ name: 'portrait keyboard', width: 390, height: 844, visible: 544 },
	{ name: 'small phone keyboard', width: 360, height: 640, visible: 340 },
	{ name: 'very short keyboard', width: 390, height: 390, visible: 250 },
	{ name: 'landscape', width: 844, height: 390, visible: null },
	{ name: 'landscape keyboard', width: 844, height: 390, visible: 200 }
];

await runJourney({
	name: 'send-turn-rail',
	cases: [
		{
			// Everything happens in setup, which is not recorded: the journey shrinks
			// the screen itself, so any layout shift here would be its own doing.
			name: 'short-visible-height', viewports: ['phone'], throttles: ['fast'],
			async setup(ctx) {
				const { page, log } = ctx;
				await openChat('long')(ctx);
				for (const { name, width, height, visible } of short) {
					await page.setViewportSize({ width, height });
					await showVisibleHeight(page, visible);
					const at = await railGeometry(page);
					log(`${name} ${JSON.stringify(at)}`);
					// A transcript with no height left has no rail to show: it collapses.
					const inside = at.railTop >= at.logTop - 1 && at.railBottom <= at.composerTop + 1;
					expect(inside || at.railBottom - at.railTop < 1, `${name}: the rail stays inside the transcript (${JSON.stringify(at)})`);
				}
				await page.setViewportSize({ width: 390, height: 844 });
				await showVisibleHeight(page, 544);
				const before = await railGeometry(page);
				await composer(page).fill('please read the deployment notes again and then list every question that is still open for me');
				await page.waitForFunction((top) => document.querySelector('.input-slot').getBoundingClientRect().top < top - 10, before.composerTop);
				const after = await railGeometry(page);
				expect(Math.abs(after.railTop - before.railTop) <= 1, `the draft's height does not move the rail (${before.railTop} to ${after.railTop})`);
			},
			async act() {}
		},
		sendCase({
			name: 'wrapped-send', setup: openHistory,
			prepare: ({ page }) => composer(page).fill('please read the deployment notes again and then list every question that is still open for me [script:default]')
		}),
		// The words of a "Not sent" row join a typed draft, and both go at once.
		sendCase({ name: 'restored-row-send', setup: openStopChat, prepare: restoreNotSentRowIntoADraft })
	],
	allow: [...DEV_INSTANCE_NOISE]
});
