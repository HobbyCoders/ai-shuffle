// Journey: stop a reply while it streams. The fake AI streams slowly
// ([script:slow tps=3]); the user presses Stop part way through. The turn must
// end cleanly: streaming goes false and stays false (no "working" state that
// never clears, no restart), the Stop button goes away, the part already
// streamed stays on screen without the rest arriving, and the composer takes
// the next message, which gets its reply.
import { runJourney } from './runner.mjs';
import {
	SLOW_REPLY,
	countUserRows,
	expect,
	openNewChat,
	scriptTag,
	stopButton,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime, spans } from './lib/timeline.mjs';

await runJourney({
	name: 'stop-midstream',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [
		{
			name: 'stop',
			async setup(ctx) {
				await openNewChat(ctx);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send');
				await typeAndSend(page, `stream slowly tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
				await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
				await waitChat(page, { streaming: 'true' });
				await page.waitForTimeout(700);

				await mark('stop');
				const t0 = Date.now();
				await stopButton(page).click();
				await waitChat(page, { streaming: 'false' }, { timeout: 15_000 });
				log(`streaming ended ${Date.now() - t0}ms after Stop`);
				await stopButton(page).waitFor({ state: 'detached', timeout: 5000 });
				// Hold: a stopped turn must stay stopped (no late restart, no rest of the reply).
				await page.waitForTimeout(3000);
				const state = await page.evaluate(() => document.querySelector('[data-ui-state="chat"]').getAttribute('data-ui-streaming'));
				expect(state === 'false', 'still not streaming 3s after Stop');
				expect(await page.getByText(SLOW_REPLY.start, { exact: false }).first().isVisible(), 'the part streamed before Stop is still shown');
				expect((await page.getByText(SLOW_REPLY.end, { exact: false }).count()) === 0, 'the rest of the reply never arrived after Stop');

				await mark('send after stop');
				const next = `tok-${uniq()}`;
				await typeAndSend(page, `after stopping ${next}`);
				await waitTurnSettled(page, { text: `You said: “after stopping ${next}`, messagesAtLeast: 3 });
				expect((await countUserRows(page, next)) === 1, 'the next message shows once');
			},
			verify({ recording }) {
				expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') });
				const stoppedAt = markTime(recording, 'stop');
				const nextAt = markTime(recording, 'send after stop');
				// After the stop settled, streaming may only come back with the next send.
				const relapses = spans(recording, (s) => s.streaming === 'true', { from: stoppedAt }).filter(
					(span) => span.start > stoppedAt + 50 && span.start < nextAt
				);
				expect(relapses.length === 0, `streaming came back after Stop: ${JSON.stringify(relapses)}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
