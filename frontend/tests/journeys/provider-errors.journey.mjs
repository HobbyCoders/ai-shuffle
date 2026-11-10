// Journey: the model provider fails. The fake AI answers HTTP 500, HTTP 429
// (rate limited) or drops the stream mid-reply. The user must see a clear
// error (the failed turn's Error row in the transcript saying what happened,
// shown once: the banner steps aside while the row says the same, #1341), the
// chat must not be stuck (the turn ends, Stop goes away, the composer takes a
// message), and the retry (sending again) must work, leave no banner, and keep
// the failed turn's Error row as its record.
//
// Cases:
//   error-500        [script:error-500]: every attempt fails with HTTP 500
//   rate-limit-429   [script:rate-limit-429]: every attempt is rate limited
//   cut              [script:cut]: the stream dies mid-reply (client retries, then gives up)
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, countUserRows, errorBanner, errorRow, expect, openNewChat, sendButton, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const SCENARIOS = {
	'error-500': /HTTP 500/,
	'rate-limit-429': /HTTP 429|rate limit/i,
	cut: /interrupted/i
};

function errorCase(script) {
	return {
		name: script,
		async setup(ctx) {
			await openNewChat(ctx);
			await ctx.page.waitForTimeout(800);
		},
		async act({ page, mark, variant, log }) {
			await mark('send');
			const t0 = Date.now();
			const first = `tok-${uniq()}`;
			await typeAndSend(page, `provoke ${script} ${first} ${fakeAiTag(variant, script)}`);
			// The client retries first; give it time, then the turn must end.
			await errorRow(page, SCENARIOS[script]).first().waitFor({ timeout: 120_000 });
			await waitChat(page, { surface: 'active', streaming: 'false' }, { timeout: 30_000 });
			log(`error shown ${Date.now() - t0}ms after send`);
			// The row says it; the banner would only say it again (#1341).
			await page.waitForTimeout(2000);
			expect((await errorRow(page, SCENARIOS[script]).count()) === 1, 'the failed turn shows one Error row');
			const banners = await errorBanner(page).allInnerTexts();
			expect(banners.length === 0, `no banner repeats the Error row, not ${JSON.stringify(banners)}`);
			const session = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
			expect(await countUserRows(page, first) === 1, 'the first message survives the provider error');
			expect((await page.getByRole('button', { name: 'Stop generating' }).count()) === 0, 'no Stop button once the turn failed');

			await mark('retry');
			const retry = `tok-${uniq()}`;
			await composer(page).click();
			await composer(page).fill(`retry ${retry} ${fakeAiTag(variant)}`.trim());
			expect(await sendButton(page).isEnabled(), 'the composer can send after the error');
			await sendButton(page).click();
			await waitTurnSettled(page, { text: `You said: “retry ${retry}`, messagesAtLeast: 3 });
			expect((await errorBanner(page).count()) === 0, 'no banner once a retry succeeded');
			expect((await errorRow(page, SCENARIOS[script]).count()) === 1, 'the failed turn keeps its Error row as its record');
			expect(await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session') === session, 'the retry stays in the first send\'s chat');
			expect(await countUserRows(page, first) === 1, 'retrying does not lose the failed first message');
		},
		verify({ recording }) {
			expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') });
		}
	};
}

await runJourney({
	name: 'provider-errors',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [errorCase('error-500'), errorCase('rate-limit-429'), errorCase('cut')],
	allow: [...DEV_INSTANCE_NOISE]
});
