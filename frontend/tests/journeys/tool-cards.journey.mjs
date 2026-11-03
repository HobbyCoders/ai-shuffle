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
import { PROJECT, api, chatIdByToken, countUserRows, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled, watchRowFlicker } from './lib/app.mjs';
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

const workerFollowup = {
	name: 'worker-followup',
	allow: [{
		kind: 'failed_requests', status: 409, url: /\/git\/fetch(\?|$)/,
		reason: 'background fetch is refused while the scripted worker/follow-up uses the Git target (require_idle_git_target)'
	}],
	async setup(ctx) {
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(ctx.page, 'Bypass');
	},
	async act({ page, mark, variant, log }) {
		const fetchRefusals = [];
		page.on('response', (response) => {
			if (response.status() === 409 && response.url().includes('/git/fetch'))
				fetchRefusals.push(response.json());
		});
		const token = `followup-${uniq()}`;
		await mark('start an untouched worker then send its follow-up');
		await typeAndSend(page, `${token} ${fakeAiTag(variant, 'tool-worker-followup')}`);
		await waitTurnSettled(page, { text: 'Follow-up dispatched.', messagesAtLeast: 2 });
		const id = await chatIdByToken(page, token);
		const session = await api(page, 'GET', `/sessions/${id}`);
		const messages = JSON.stringify(session.messages);
		expect(messages.includes('Resume queued for script-tool-bash'), `the real SendMessage admits a continuation: ${messages.slice(-6000)}`);
		expect(!messages.includes('Could not resume'), 'the follow-up was not refused');
		const result = session.messages.find((m) => m.type === 'subagent' && m.content);
		expect(!!result, 'the real Task has a persisted worker receipt');
		const worker = JSON.parse(result.content);
		await page.waitForFunction(async (id) => {
			const response = await fetch(`/api/v1/sessions/${id}`);
			const child = await response.json();
			return child.messages.filter((m) => m.tool_name === 'Bash' && m.toolStatus === 'completed').length >= 2;
		}, worker.worker_session_id, { timeout: 30_000, polling: 500 });
		log('worker executed Bash on its first turn and its resumed turn');
		for (const refusal of await Promise.all(fetchRefusals))
			expect(refusal.detail === 'A session using this worktree or branch is active. Stop it or wait before changing Git state.', 'only the active Git target safety refusal is expected');
	}
};

const nativeModel = {
	name: 'native-model',
	async setup(ctx) {
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(ctx.page, 'Bypass');
	},
	async act({ page, mark, variant, log }) {
		const token = `native-model-${uniq()}`;
		await mark('refuse native version pins and accept the native family name');
		await typeAndSend(page, `${token} Use opus for the new chat. ${fakeAiTag(variant, 'tool-native-model-override')}`);
		await waitTurnSettled(page, { text: 'Native model checks complete.', messagesAtLeast: 2 });
		const parent = await api(page, 'GET', `/sessions/${await chatIdByToken(page, token)}`);
		const results = parent.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult);
		expect(results.length === 4, `three pin refusals and one family start: ${JSON.stringify(results)}`);
		for (const refused of results.slice(0, 3)) {
			expect(refused.toolStatus === 'error' && /family name/.test(refused.toolResult)
				&& /Choose one of:/.test(refused.toolResult), `pin refused with real choices: ${JSON.stringify(refused)}`);
		}
		const result = results[3];
		expect(result?.toolStatus !== 'error', `family StartSession succeeded: ${JSON.stringify(result)}`);
		const created = JSON.parse(result.toolResult);
		const child = await api(page, 'GET', `/sessions/${created.session_id}`);
		expect(child.model === 'opus' && !child.provider_id, `native routing retained: ${JSON.stringify({ model: child.model, provider: child.provider_id })}`);
		log(`native override resolved to ${child.model}`);
	}
};

await runJourney({
	name: 'tool-cards',
	recorder: {
		selectors: [STOP_SELECTOR, 'worklog=[role="region"][aria-label="Work log"]']
	},
	cases: [toolCase('bash'), toolCase('multi'), workerFollowup, nativeModel],
	allow: [...DEV_INSTANCE_NOISE]
});
