// Sending must not move the turn rail, including when the composer gives height
// back: its box sets the transcript's height, and a rail placed from that box
// followed it (#1094). chat-position-stability's own send is one short line and
// never changes the composer's height, so these sends live here. On a phone the
// rail stands at the transcript's top whatever shares the composer's slot, and
// only its bottom gives way to a keyboard or a tall draft.
import { runJourney } from './runner.mjs';
import { composer, expect, gotoApp, PROJECT, pickAccess, pickProject, seededChatId, sendButton, stopButton, typeAndSend, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { mobileShell, openStopChat, queueDuringASlowTurn } from './lib/stop-queue.mjs';
import { movements, transcriptMovements, unmeasuredFrames } from './lib/frame-movement.mjs';
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
		const rows = [...log.querySelectorAll('[data-transcript-key]')];
		// A transcript a few pixels tall can show only its bottom padding; the last row still reports.
		const row = rows.find((el) => el.getBoundingClientRect().bottom > logTop + 1) ?? rows[rows.length - 1];
		const watch = (window.__railFrames = { running: true, next: 0, frames: [] });
		const sample = () => {
			if (!watch.running) return;
			const elements = { rail: document.querySelector('nav.turn-nav'), composer: document.querySelector('.input-slot'), row };
			const seq = watch.next++;
			const observer = new IntersectionObserver((entries) => {
				observer.disconnect();
				const top = (el) => entries.find((entry) => entry.target === el)?.boundingClientRect.top ?? null;
				watch.frames.push({ seq, rail: top(elements.rail), composer: top(elements.composer), row: top(elements.row), content: log.scrollHeight, gap: log.scrollHeight - log.clientHeight - log.scrollTop });
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

/** Only the frames where something moved, so a failure can be read from its log. */
const changes = (frames) => frames
	.filter((frame, i) => i === 0 || ['row', 'content', 'composer', 'gap'].some((key) => frame[key] !== frames[i - 1][key]))
	.map(({ seq, row, content, composer, gap }) => [seq, row, content, composer, gap].map((value) => (Number.isFinite(value) ? Math.round(value) : value)));

const present = (frames, key) => frames.map((frame) => frame[key]).filter(Number.isFinite);

const sendCase = ({ name, setup, prepare, ...options }) => ({
	name, setup, ...options,
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
		// Desktop only: the composer gives back 8.5px and the row follows it for
		// one frame before the bottom-follow code takes it back. Phones do not.
		const knownCorrection = ctx.variant.viewport === 'desktop' ? 8.5 : null;
		const transcriptMoves = transcriptMovements(frames, { knownCorrection });
		const unmeasured = unmeasuredFrames(frames);
		log(`send frames ${JSON.stringify({ frames: frames.length, unmeasured, rail: travel, composerMoves, transcriptMoves })}`);
		log(`row path ${JSON.stringify(changes(frames))}`);
		expect(frames.length >= 20 && unmeasured === 0, `the row and composer were measured on every frame of the send (${unmeasured} of ${frames.length} missing)`);
		expect(travel <= 2, `sending keeps the turn rail still (${travel.toFixed(1)}px)`);
		expect(composerMoves <= 1, `clearing the draft moves the composer at most once (${composerMoves} movements)`);
		expect(transcriptMoves <= 1, `the composer shrinking moves the transcript at most once (${transcriptMoves} movements)`);
	}
});

/** Resolves once the composer has held one place and size for 20 frames. */
function composerAtRest(page) {
	return page.waitForFunction(() => {
		const box = document.querySelector('.input-slot').getBoundingClientRect();
		const at = `${box.top}/${box.height}`;
		window.__composerRest = at === window.__composerRest?.at ? { at, frames: window.__composerRest.frames + 1 } : { at, frames: 0 };
		return window.__composerRest.frames >= 20;
	}, undefined, { polling: 'raf', timeout: 15_000 });
}

/** A chat with two turns and one child chat still running, so the line naming
 * that child sits in the composer's slot above the draft. */
async function openChatWithARunningChild(ctx) {
	const { page } = ctx;
	await openStopChat(ctx);
	await pickProject(page, page.getByRole('region', { name: 'New chat' }), PROJECT);
	await pickAccess(page, 'Bypass');
	await typeAndSend(page, 'start the helper [script:tool-start-running-child]');
	await waitTurnSettled(page, { text: 'Started the child.', messagesAtLeast: 2 });
	await page.locator('.input-slot').getByText('Running child job').waitFor({ timeout: 30_000 });
	await typeAndSend(page, 'one more turn [script:default]');
	await waitTurnSettled(page, { messagesAtLeast: 4 });
}

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
	return { railTop: rail.top, railBottom: rail.bottom, logTop: log.top, composerTop: composer.top, composerHeight: composer.height };
}, LOG);

// Enough for a few ticks; a shorter rail scrolls, and a shorter transcript fills it.
const MIN_RAIL = 40;

/** The rail's top is the transcript's top; only a transcript with no height left may lose the rail. */
function expectRailUsable(name, at) {
	const detail = JSON.stringify(at);
	expect(Math.abs(at.railTop - at.logTop) <= 1, `${name}: the rail stands at the transcript's top (${detail})`);
	const room = at.composerTop - at.logTop;
	if (room < 1) return;
	expect(at.railTop >= at.logTop - 1 && at.railBottom <= at.composerTop + 1, `${name}: the rail stays inside the transcript (${detail})`);
	expect(at.railBottom - at.railTop >= Math.min(room, MIN_RAIL) - 1, `${name}: the rail stays usable while the transcript has room (${detail})`);
}

const DRAFT = 'please read the deployment notes again and then list every question that is still open for me ';

/** The touch scrub's preview rides above the finger, and over the first tick
 * there is no room above it. Real pointer handlers, synthetic touch. */
async function firstTickPreview(page) {
	const at = await page.evaluate(async () => {
		const nav = document.querySelector('nav.turn-nav');
		const rail = nav.querySelector('.turn-rail');
		const [first, second] = rail.querySelectorAll('.tick');
		const y = (el) => { const box = el.getBoundingClientRect(); return box.top + box.height / 2; };
		const send = (target, type, clientY) => target.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: true, pointerId: 41, clientY }));
		send(second, 'pointerdown', y(second));
		send(rail, 'pointermove', y(first));
		await new Promise((done) => setTimeout(done, 300));
		const box = nav.querySelector('.preview').getBoundingClientRect();
		const result = { scrubbing: nav.classList.contains('is-scrubbing'), top: box.top, bottom: box.bottom, tick: y(first) };
		send(rail, 'pointercancel', y(first));
		return result;
	});
	return at;
}

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
					expectRailUsable(name, at);
				}
				await page.setViewportSize({ width: 390, height: 844 });
				await showVisibleHeight(page, 544);
				const before = await railGeometry(page);
				await composer(page).fill('please read the deployment notes again and then list every question that is still open for me');
				await page.waitForFunction((top) => document.querySelector('.input-slot').getBoundingClientRect().top < top - 10, before.composerTop);
				const after = await railGeometry(page);
				expect(Math.abs(after.railTop - before.railTop) <= 1, `the draft's height does not move the rail (${before.railTop} to ${after.railTop})`);
				const preview = await firstTickPreview(page);
				log(`first tick preview ${JSON.stringify(preview)}`);
				expect(preview.scrubbing && preview.top >= before.logTop && preview.bottom > preview.top, `the first tick's preview stays below the header (${JSON.stringify(preview)})`);
				let tallest = 0;
				for (const visible of [340, 436, 544, 640]) {
					await showVisibleHeight(page, visible);
					await composer(page).fill('');
					await page.waitForTimeout(200);
					const rest = await railGeometry(page);
					for (const chars of [60, 150, 230]) {
						await composer(page).fill(DRAFT.repeat(3).slice(0, chars));
						await page.waitForTimeout(200);
						const at = await railGeometry(page);
						log(`ordinary draft ${visible}/${chars} ${JSON.stringify(at)}`);
						tallest = Math.max(tallest, at.composerHeight);
						expect(Math.abs(at.railTop - rest.railTop) <= 1, `a ${Math.round(at.composerHeight)}px composer at visible ${visible} does not move the rail (${rest.railTop} to ${at.railTop})`);
					}
				}
				expect(tallest > 260, `the drafts reached a tall composer (${tallest}px)`);
				await showVisibleHeight(page, 544);
				// Grow the draft until the transcript has less room than the rail's usual
				// offset. /mobile/ caps its composer at 300px, so it needs less height to get there.
				if (mobileShell) await showVisibleHeight(page, 440);
				let tightest = Infinity;
				for (const repeats of [4, 5, 7, 10, 14]) {
					await composer(page).fill(DRAFT.repeat(repeats));
					await page.waitForTimeout(200);
					const tall = await railGeometry(page);
					log(`tall draft ${repeats} under a keyboard ${JSON.stringify(tall)}`);
					expectRailUsable(`tall draft (${repeats}) under a keyboard`, tall);
					tightest = Math.min(tightest, tall.composerTop - tall.logTop);
				}
				expect(tightest > 0 && tightest < 130, `a draft left the transcript too little room for the usual offset (${tightest}px left)`);
				await composer(page).fill('');
				// The rail and the app's shell read one height, so the rule holds with that
				// height missing (both use 100dvh) or out of date.
				await page.setViewportSize({ width: 360, height: 340 });
				await showVisibleHeight(page, null);
				for (const height of [null, '544px']) {
					await page.evaluate((value) => {
						if (value) document.documentElement.style.setProperty('--app-viewport-height', value);
						else document.documentElement.style.removeProperty('--app-viewport-height');
					}, height);
					await composer(page).fill('');
					await page.waitForTimeout(200);
					const rest = await railGeometry(page);
					await composer(page).fill(DRAFT);
					await page.waitForTimeout(200);
					const at = await railGeometry(page);
					log(`app height ${height ?? 'unset'} ${JSON.stringify({ rest, at })}`);
					expect(at.composerHeight > rest.composerHeight + 20, `the draft wrapped with the app height ${height ?? 'unset'}`);
					// A shell taller than the window scrolls the page to the composer, so read the rail against the transcript.
					const offset = ({ railTop, logTop }) => railTop - logTop;
					expect(Math.abs(offset(at) - offset(rest)) <= 1, `with the app height ${height ?? 'unset'} a wrapped draft does not move the rail (${offset(rest)} to ${offset(at)} below the header)`);
				}
				await composer(page).fill('');
			},
			async act() {}
		},
		sendCase({
			name: 'wrapped-send', setup: openHistory,
			prepare: ({ page }) => composer(page).fill('please read the deployment notes again and then list every question that is still open for me [script:default]')
		}),
		// With a keyboard up the rail stands at the top of a short transcript, and a
		// wrapped draft must not change that.
		sendCase({
			// Prepared in setup, which is not recorded: the journey shrinks the screen
			// itself, and on a slow phone that lands outside "recent input".
			name: 'wrapped-send-keyboard', viewports: ['phone'],
			async setup(ctx) {
				const { page } = ctx;
				await openHistory(ctx);
				await page.setViewportSize({ width: 360, height: 640 });
				await showVisibleHeight(page, 340);
				await composer(page).fill('please read the deployment notes again and then list every question that is still open for me [script:default]');
				await composerAtRest(page);
			},
			prepare: async () => {}
		}),
		// Whatever else sits in the composer's slot leaves with its draft height, so
		// it must not be able to move the rail either. Prepared in setup, which is
		// not recorded: the journey grows the draft itself, without typing.
		sendCase({
			name: 'six-line-send-with-running-child', viewports: ['phone'], throttles: ['fast'],
			async setup(ctx) {
				const { page, log } = ctx;
				await openChatWithARunningChild(ctx);
				await showVisibleHeight(page, 544);
				await composer(page).fill(`${DRAFT.repeat(3).slice(0, 230)} [script:default]`);
				await page.waitForTimeout(300);
				const at = await railGeometry(page);
				log(`six lines with a running child ${JSON.stringify(at)}`);
				expect(at.composerHeight >= 300, `the draft and the running child's line filled the slot (${at.composerHeight}px)`);
				// Send from a settled chat, like the other cases: the last turn's room below the reader has been handed back.
				await page.waitForFunction((selector) => {
					const log = document.querySelector(selector);
					return log.scrollHeight - log.clientHeight - log.scrollTop <= 16;
				}, LOG, { timeout: 15_000 });
			},
			prepare: async () => {}
		}),
		// The words of a "Not sent" row join a typed draft, and both go at once.
		sendCase({ name: 'restored-row-send', viewports: ['phone'], setup: openStopChat, prepare: restoreNotSentRowIntoADraft })
	],
	allow: [...DEV_INSTANCE_NOISE]
});
