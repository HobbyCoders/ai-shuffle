// Journey: a reply that runs tools. The fake AI calls Bash (once, or three
// times over two rounds), then answers. The work log and its tool cards must
// build up forward: the work log never disappears once shown, no row leaves
// the page and comes back (a fold undone, a row remounted), nothing remounts, the final text lands, the Stop
// button follows the turn, and the folded log names what ran. Expanding the
// log shows one card per call, each Complete, with its command.
//
// Cases:
//   bash    [script:tool-bash]: one Bash call (echo), then the answer
//   multi   [script:tool-multi]: two parallel calls, one more, then the answer
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, countUserRows, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled, watchRowFlicker } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { STOP_SELECTOR, expectNeverDecreases, expectStopFollowsStreaming, markTime } from './lib/timeline.mjs';

const SCENARIOS = {
	bash: {
		script: 'tool-bash',
		final: 'All done.',
		summary: /Ran 1 command/,
		cards: ['Bash: echo hello from fake ai. Complete']
	},
	multi: {
		script: 'tool-multi',
		final: 'I ran three commands',
		summary: /Ran 3 commands/,
		cards: ['Bash: pwd. Complete', 'Bash: ls -la. Complete', 'Bash: git status --short || true. Complete']
	}
};

const worklogToggle = (page) => page.getByRole('region', { name: 'Work log' }).last().getByRole('button', { name: /work details/ }).first();

function toolCase(name) {
	const scenario = SCENARIOS[name];
	return {
		name,
		async setup(ctx) {
			// A project chat, so the tools have a checkout to run in. Bypass, so
			// the cards are judged and not the approval flow: under Ask the call
			// prompts or not depending on the host's sandbox (see pickAccess).
			await openNewChat(ctx, { project: PROJECT });
			await pickAccess(ctx.page, 'Bypass');
			await ctx.page.waitForTimeout(800);
		},
		async act({ page, mark, variant, log }) {
			const token = `tok-${uniq()}`;
			await mark('send');
			const t0 = Date.now();
			const flicker = await watchRowFlicker(page);
			await typeAndSend(page, `use tools ${token} ${fakeAiTag(variant, scenario.script)}`);
			await waitTurnSettled(page, { text: scenario.final, messagesAtLeast: 2 });
			log(`turn settled ${Date.now() - t0}ms after send`);
			const back = await flicker();
			expect(back.length === 0, `rows left the page and came back while the turn ran: ${JSON.stringify(back)}`);
			expect((await countUserRows(page, token)) === 1, 'the prompt shows once');
			const toggle = worklogToggle(page);
			const label = (await toggle.getAttribute('aria-label')) || (await toggle.innerText());
			expect(scenario.summary.test(label), `the folded work log says what ran (${label})`);

			await mark('expand the work log');
			await toggle.click();
			for (const card of scenario.cards) {
				await page.getByRole('button', { name: card, exact: true }).waitFor({ timeout: 10_000 });
			}
			expect(
				(await page.getByRole('button', { name: /^Bash: / }).count()) === scenario.cards.length,
				`one card per call (${scenario.cards.length})`
			);
			await page.waitForTimeout(800);

			await mark('collapse the work log');
			await worklogToggle(page).click();
			await page.getByRole('button', { name: scenario.cards[0], exact: true }).waitFor({ state: 'hidden', timeout: 10_000 });
			await page.getByText(scenario.final, { exact: false }).last().waitFor();
			await page.waitForTimeout(800);
		},
		verify({ recording }) {
			const sent = markTime(recording, 'send');
			expectStopFollowsStreaming(recording, { from: sent });
			// Between the send and the expand click the work log, once shown, stays.
			const expandAt = markTime(recording, 'expand the work log');
			const upToExpand = { ...recording, timeline: recording.timeline.filter((e) => e.t < expandAt) };
			expectNeverDecreases(upToExpand, 'worklog', { from: sent });
		}
	};
}

await runJourney({
	name: 'tool-cards',
	recorder: {
		selectors: [STOP_SELECTOR, 'worklog=[role="region"][aria-label="Work log"]']
	},
	cases: [toolCase('bash'), toolCase('multi')],
	allow: [...DEV_INSTANCE_NOISE]
});
