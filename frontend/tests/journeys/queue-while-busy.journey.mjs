// Journey: send a second message while the first reply is still streaming.
// The second message must show as queued/pending right away (the user can see
// it was taken), be delivered exactly once after the first turn, and get its
// own reply. At no painted frame may it show twice (queued bubble and
// delivered row together, or two rows), and nothing may remount.
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	SLOW_REPLY,
	countUserRows,
	expect,
	openNewChat,
	scriptTag,
	scrollTranscriptTo,
	transcriptScroll,
	typeAndSend,
	uniq,
	waitChat,
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
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
