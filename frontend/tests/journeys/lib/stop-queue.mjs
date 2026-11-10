// What the journeys about Stop with queued messages share.
import { SLOW_REPLY, expect, gotoApp, openNewChat, pickAgent, scriptTag, typeAndSend, uniq, waitChat } from './app.mjs';

// Hosted journeys build shared desktop UI only. Explicitly opt into the native
// phone shell locally, matching transcript-reader's --mobile-shell convention.
export const mobileShell = process.argv.includes('--mobile-shell');

export async function openStopChat(ctx) {
	if (!mobileShell || ctx.variant.viewport !== 'phone') return openNewChat(ctx);
	const { page, status } = ctx;
	await gotoApp(page, status, '/mobile/');
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New chat' });
	await hero.waitFor();
	await pickAgent(page, hero);
}

/**
 * Every painted frame from now until the returned function runs: where the row
 * being read sits and, per queued message, how many copies show and where. Rows
 * are read off the DOM, not the recorder's score, so a move under its threshold
 * still shows. Positions come from an IntersectionObserver made inside the frame:
 * its entry holds the geometry the browser computed last before painting, after
 * the app's own scroll for that frame. A read inside the frame comes too early and
 * a read after it can catch a fold that has not painted yet.
 */
export async function watchStopFrames(page, texts) {
	await page.evaluate((texts) => {
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const logTop = log.getBoundingClientRect().top;
		const rows = () => [...log.querySelectorAll('.chat-column > .msg-virtualized')];
		const key = rows().find((el) => el.getBoundingClientRect().bottom > logTop + 1)?.dataset.transcriptKey;
		// The first line on screen inside that row. A row taller than the screen
		// can fold or grow above the viewport while its top stays put; this is what
		// the person actually sees. Found again by its text if it is re-rendered.
		const LINES = 'p, li, pre, h1, h2, h3, h4, blockquote, tr';
		const lineIn = (row) => row && [...row.querySelectorAll(LINES)].find((el) => el.getBoundingClientRect().top >= logTop);
		const firstLine = lineIn(rows().find((el) => el.dataset.transcriptKey === key));
		const lineText = firstLine?.textContent ?? null;
		const visibleLine = () => {
			if (!lineText) return null;
			const row = rows().find((el) => el.dataset.transcriptKey === key);
			return row ? [...row.querySelectorAll(LINES)].find((el) => el.textContent === lineText) ?? null : null;
		};
		const watch = (window.__stopFrames = { frames: [], next: 0, running: true, startedAt: performance.now() });
		const slot = document.querySelector('.input-slot');
		const bubblesAndRows = () => [
			...document.querySelectorAll('.pending-messages [role="article"], [data-not-sent="true"]')
		];
		const frame = () => {
			if (!watch.running) return;
			const order = watch.next++;
			const seen = new IntersectionObserver((entries) => {
				seen.disconnect();
				const painted = new Map(entries.map((entry) => [entry.target, entry.boundingClientRect.top]));
				const topOf = (el) =>
					Math.round((painted.get(el) ?? el.getBoundingClientRect().top) * 10) / 10;
				const anchor = rows().find((el) => el.dataset.transcriptKey === key);
				const line = visibleLine();
				watch.frames.push({
					order,
					at: Math.round(performance.now() - watch.startedAt),
					scrollTop: Math.round(log.scrollTop),
					scrollHeight: log.scrollHeight,
					// The running-task row at the history tail, if any (#1147).
					taskRow: log.querySelector('.chat-column > .inline')?.getBoundingClientRect().height ?? null,
					anchorTop: anchor ? topOf(anchor) : null,
					lineTop: line ? topOf(line) : null,
					replyHeight: rows().at(-1)?.getBoundingClientRect().height ?? null,
					composerTop: slot ? topOf(slot) : 0,
					messages: texts.map((text) => {
						const has = (el) => (el.textContent || '').includes(text);
						const bubbles = [...document.querySelectorAll('.pending-messages [role="article"]')].filter(has);
						const notSent = [...document.querySelectorAll('[data-not-sent="true"]')].filter(has);
						return {
							bubbles: bubbles.length,
							notSent: notSent.length,
							labelledNotSent: bubbles.filter((el) => el.dataset.queueStatus === 'stopped').length,
							top: topOf(bubbles[0] ?? notSent[0] ?? log)
						};
					})
				});
			});
			for (const el of [log, slot, visibleLine(), ...rows(), ...bubblesAndRows()].filter(Boolean)) seen.observe(el);
			requestAnimationFrame(frame);
		};
		requestAnimationFrame(frame);
	}, texts);
	return () =>
		page.evaluate(() => {
			const watch = window.__stopFrames;
			watch.running = false;
			// After a slow frame the browser delivers several of these together, in no set order.
			return watch.frames.sort((a, b) => a.order - b.order);
		});
}

/**
 * The stopped turn settles 1.2s after it ends (settle-hold.ts). The reader once
 * moved at that moment, so watch well past it.
 */
export const PAST_SETTLEMENT_MS = 1200 + 3000;

/** Send a slow turn and queue messages the server has confirmed, so only Stop can change them from here. */
export async function queueDuringASlowTurn(page, mark, variant, queued, opening = `stream tok-${uniq()}`) {
	await mark('send');
	// A second turn in this chat: its reply, not the last one with the same words.
	const replies = page.getByText(SLOW_REPLY.start, { exact: false });
	const repliesBefore = await replies.count();
	await typeAndSend(page, `${opening} ${scriptTag(variant, 'slow', { tps: 2 })}`);
	await replies.nth(repliesBefore).waitFor({ timeout: 60_000 });
	await waitChat(page, { streaming: 'true' });

	await mark('queue');
	const texts = Array.from({ length: queued }, () => `tok-${uniq()}`);
	for (const text of texts) {
		await typeAndSend(page, `queued ${text}`);
		await page.locator('.pending-messages [role="article"]').filter({ hasText: text }).waitFor({ timeout: 5000 });
	}
	await page.waitForFunction(
		(n) => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === n,
		texts.length,
		{ timeout: 30_000 }
	);
	return texts;
}

export function expectEachOnceInEveryFrame(frames) {
	frames.forEach((frame, i) => {
		frame.messages.forEach((message, m) => {
			expect(message.bubbles + message.notSent === 1, `queued message ${m} shows exactly once in frame ${i}: ${JSON.stringify(message)}`);
		});
	});
}
