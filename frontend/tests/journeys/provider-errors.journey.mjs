// Journey: the model provider fails. The fake AI answers HTTP 500, HTTP 429
// (rate limited) or drops the stream mid-reply. The user must see a clear
// error (an alert saying what happened, which they can dismiss), the chat must
// not be stuck (the turn ends, Stop goes away, the composer takes a message),
// and the retry (sending again) must work and clear the old error.
//
// Cases:
//   error-500        [script:error-500]: every attempt fails with HTTP 500
//   rate-limit-429   [script:rate-limit-429]: every attempt is rate limited
//   cut              [script:cut]: the stream dies mid-reply (client retries, then gives up)
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, expect, openNewChat, sendButton, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const SCENARIOS = {
	'error-500': /HTTP 500/,
	'rate-limit-429': /HTTP 429|rate limit/i,
	cut: /interrupted/i
};

const alert = (page) => page.getByRole('log', { name: 'Conversation messages' }).getByRole('alert');

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
			await typeAndSend(page, `provoke ${script} tok-${uniq()} ${fakeAiTag(variant, script)}`);
			// The client retries first; give it time, then the turn must end.
			await alert(page).filter({ hasText: SCENARIOS[script] }).first().waitFor({ timeout: 120_000 });
			await waitChat(page, { surface: 'active', streaming: 'false' }, { timeout: 30_000 });
			log(`error shown ${Date.now() - t0}ms after send`);
			await page.getByRole('button', { name: 'Dismiss error' }).first().waitFor();
			await page.waitForTimeout(1000);
			expect((await page.getByRole('button', { name: 'Stop generating' }).count()) === 0, 'no Stop button once the turn failed');

			await mark('retry');
			const retry = `tok-${uniq()}`;
			await composer(page).click();
			await composer(page).fill(`retry ${retry} ${fakeAiTag(variant)}`.trim());
			expect(await sendButton(page).isEnabled(), 'the composer can send after the error');
			await sendButton(page).click();
			await waitTurnSettled(page, { text: `You said: “retry ${retry}`, messagesAtLeast: 3 });
			expect((await alert(page).count()) === 0, 'the old error is gone once a retry succeeded');
		},
		verify({ recording }) {
			expectStopFollowsStreaming(recording, { from: markTime(recording, 'send') });
		}
	};
}

const dismissCase = {
	name: 'dismiss',
	async setup(ctx) {
		await openNewChat(ctx);
		await typeAndSend(ctx.page, `provoke tok-${uniq()} [script:error-500]`);
		await alert(ctx.page).first().waitFor({ timeout: 120_000 });
		await waitChat(ctx.page, { streaming: 'false' });
		await ctx.page.waitForTimeout(500);
	},
	async act({ page, mark }) {
		await mark('dismiss the error');
		await page.getByRole('button', { name: 'Dismiss error' }).first().click();
		await alert(page).first().waitFor({ state: 'detached', timeout: 5000 });
		await page.waitForTimeout(1000);
		expect((await alert(page).count()) === 0, 'the dismissed error stays dismissed');
	}
};

await runJourney({
	name: 'provider-errors',
	recorder: { selectors: [STOP_SELECTOR] },
	cases: [errorCase('error-500'), errorCase('rate-limit-429'), errorCase('cut'), dismissCase],
	allow: [...DEV_INSTANCE_NOISE]
});
