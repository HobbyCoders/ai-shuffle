// Journey: move between chats. Opening the seeded 280-row transcript for the
// first time (its history not loaded yet: the open-unloaded-chat path), then
// another chat, then back. Each open only moves forward (loading → active,
// composer moving at most once), nothing remounts, no row shows twice, and the
// reading position is sane: a chat read at the bottom comes back at its last
// rows, a chat scrolled up comes back where the reader left it.
//
// Cases:
//   away-and-back   another chat → long (first open) → the other (cached) → long
//   scrolled-up     long scrolled up 40% → the other (cached) → long: same rows on screen
import { runJourney } from './runner.mjs';
import {
	SEEDED,
	expect,
	gotoApp,
	openFromSidebar,
	scrollTranscriptTo,
	transcriptScroll,
	waitChat,
	waitChatOpen
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

// The long chat's last answer (scripts/devloop/seed.py: LONG_TURNS = 70 turns, 280 rows).
const LAST_WORDS = 'Step 70 is done.';

/** The last top-level transcript row in the page. */
const lastRowInPage = (page) =>
	page.evaluate(() => {
		const rows = [...document.querySelectorAll('[role="log"] [data-ui-row]')].filter((el) => !el.parentElement?.closest('[data-ui-row]'));
		return rows[rows.length - 1]?.getAttribute('data-ui-row') ?? null;
	});

async function expectAtTail(page, what) {
	const scroll = await transcriptScroll(page);
	expect(scroll?.atBottom, `${what}: the transcript is at its bottom (${JSON.stringify(scroll)})`);
	expect(scroll.lastRow === (await lastRowInPage(page)), `${what}: the last row is on screen (${JSON.stringify(scroll)})`);
	await page.getByText(LAST_WORDS, { exact: false }).last().waitFor({ timeout: 5000 });
}

/**
 * Unrecorded: open the other chat once, in-page (a page load would drop the
 * tab's transcript cache), so the recorded switch to it is a cached open. The
 * cases judge the long chat across a switch away and back; a first open of a
 * short chat from another chat flashes the loading placeholder (#806), which no
 * marker may hide, and which open-unloaded-chat gains a case for once fixed.
 */
async function warmOtherChat(page, status) {
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
	await page.waitForTimeout(1000);
	await openFromSidebar(page, SEEDED.no_project);
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
}

await runJourney({
	name: 'switch-chats',
	cases: [
		{
			name: 'away-and-back',
			// Starts from the other chat, already opened once (warmOtherChat).
			async setup({ page, status }) {
				await warmOtherChat(page, status);
			},
			async act({ page, mark }) {
				await mark('open the long chat (first time)');
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				await expectAtTail(page, 'first open');

				await mark('open another chat');
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2 });
				// In the transcript: the turn navigator shows the same question as its title.
				await page.getByLabel('Conversation messages').getByText('What is 2 + 2?').waitFor();

				await mark('back to the long chat');
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				await expectAtTail(page, 'coming back');
			}
		},
		{
			name: 'scrolled-up',
			knownBug: {
				issue: 805,
				error: /^Error: expectation failed: came back scrolled up, not at the bottom/,
				reason: 'a switch back always scrolls to the bottom, though the leaving position is saved for it'
			},
			async setup(ctx) {
				const { page, status } = ctx;
				await warmOtherChat(page, status);
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50, hold: 800 });
				await scrollTranscriptTo(page, 0.4);
				ctx.left = await transcriptScroll(page);
				expect(!ctx.left.atBottom && ctx.left.firstRow, `scrolled up before leaving (${JSON.stringify(ctx.left)})`);
				await page.waitForTimeout(800);
			},
			async act({ page, mark, left, log }) {
				await mark('open another chat');
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2 });

				await mark('back to the long chat');
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const back = await transcriptScroll(page);
				log(`left at ${JSON.stringify(left)}, back at ${JSON.stringify(back)}`);
				expect(!back.atBottom, `came back scrolled up, not at the bottom (${JSON.stringify(back)})`);
				expect(
					back.firstRow === left.firstRow || Math.abs(back.top - left.top) <= 150,
					`came back where the reader left it (left ${JSON.stringify(left)}, back ${JSON.stringify(back)})`
				);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
