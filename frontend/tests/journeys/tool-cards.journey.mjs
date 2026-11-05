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
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
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

// Observe the producing checkout path, not the fake CLI's printed Bash result.
function observeTaskCheckouts(status) {
	const root = join(status.seed.projects['dev-sandbox'], '.fluid', 'worktrees');
	const seen = new Map();
	const sample = () => {
		for (const [path, entry] of seen) if (!existsSync(join(path, '.git'))) entry.present = false;
		for (const name of existsSync(root) ? readdirSync(root) : []) {
			if (!name.startsWith('chore-task-')) continue;
			const path = join(root, name);
			if (!existsSync(join(path, '.git')) || seen.get(path)?.present) continue;
			const branch = spawnSync('git', ['branch', '--show-current'], { cwd: path, encoding: 'utf8', timeout: 1000 });
			const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: path, encoding: 'utf8', timeout: 1000 });
			if (branch.status !== 0 || head.status !== 0) continue; // Git is still materializing it.
			const entry = seen.get(path) || { generations: [] };
			entry.generations.push({ branch: branch.stdout.trim(), head: head.stdout.trim() });
			entry.present = true;
			seen.set(path, entry);
		}
	};
	const timer = setInterval(sample, 25);
	return { root, seen, sample, stop: () => clearInterval(timer) };
}

async function waitForCheckout(predicate, message) {
	const deadline = Date.now() + 30_000;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
	throw new Error(`checkout observation timed out: ${message}`);
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
		const agents = await api(ctx.page, 'GET', '/agents');
		expect(agents.some((agent) => agent.id === 'dev-claude-code'), 'the seeded fake CLI helper is available');
	},
	async act({ page, mark, variant, log, status }) {
		const fetchRefusals = [];
		page.on('response', (response) => {
			if (response.status() === 409 && response.url().includes('/git/fetch'))
				fetchRefusals.push(response.json());
		});
		const observation = observeTaskCheckouts(status);
		try {
			const token = `followup-${uniq()}`;
			await mark('start an untouched fake CLI Task worker');
			await typeAndSend(page, `${token}\nUse Sonnet for the helper.\n${fakeAiTag(variant, 'tool-worker-followup')}`);
			await waitTurnSettled(page, { text: 'Worker task complete.', messagesAtLeast: 2 });
			const id = await chatIdByToken(page, token);
			const session = await api(page, 'GET', `/sessions/${id}`);
			const result = session.messages.find((m) => m.type === 'subagent' && m.content);
			expect(!!result, 'the real Task has a persisted worker receipt');
			const worker = JSON.parse(result.content);
			const branch = `chore/task-${worker.worker_session_id}`;
			const path = join(observation.root, branch.replaceAll('/', '-'));
			await mark('observe real Git reclamation before sending the continuation');
			await waitForCheckout(() => !existsSync(path), 'first checkout was reclaimed');
			observation.sample();
			expect(observation.seen.get(path)?.generations.length === 1, 'real filesystem and Git witnessed initial Task checkout');
			expect(spawnSync('git', ['show-ref', '--verify', '--quiet', `refs/heads/${branch}`], { cwd: status.seed.projects['dev-sandbox'] }).status === 1, 'untouched worker branch was reclaimed');
			await typeAndSend(page, fakeAiTag(variant, 'tool-worker-resume'));
			await waitTurnSettled(page, { text: 'Follow-up dispatched.', messagesAtLeast: 4 });
			const resumed = await api(page, 'GET', `/sessions/${id}`);
			const messages = JSON.stringify(resumed.messages);
			expect(messages.includes('Resume queued for script-tool-bash'), `the real SendMessage admits a continuation: ${messages.slice(-6000)}`);
			expect(!messages.includes('Could not resume'), 'the follow-up was not refused');
			await page.waitForFunction(async (id) => {
				const response = await fetch(`/api/v1/sessions/${id}`);
				const child = await response.json();
				return child.messages.filter((m) => m.tool_name === 'Bash' && m.toolStatus === 'completed').length >= 2;
			}, worker.worker_session_id, { timeout: 30_000, polling: 500 });
			observation.sample();
			const generations = observation.seen.get(path)?.generations || [];
			expect(generations.length === 2 && generations.every((g) => g.branch === branch && g.head === generations[0].head), 'real Git restored the same Task branch and commit');
			// UI tool projections can already contain two completed Bash rows from
			// the first request. Wait for the actual resumed process request too.
			const readTrace = () => readFileSync(join(status.scratch_dir, 'fake-claude.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
			await waitForCheckout(() => {
				const rows = readTrace();
				const workerPids = new Set(rows.filter((row) => row.kind === 'start' && row.cwd === path && row.argv.includes('-p')).map((row) => row.pid));
				return rows.filter((row) => row.kind === 'turn_cwd' && workerPids.has(row.pid)).length >= 2;
			}, 'actual resumed CLI incoming request');
			const trace = readTrace();
			const pids = new Set(trace.filter((row) => row.kind === 'start' && row.cwd === path && row.argv.includes('-p')).map((row) => row.pid));
			const requests = trace.filter((row) => row.kind === 'turn_cwd' && pids.has(row.pid));
			expect(requests.length >= 2 && requests.every((row) => row.cwd === path && row.cwd_error === null), `actual cwd for every worker request: ${JSON.stringify(requests)}`);
			log(`real Git creation/reclamation/restoration and valid per-request CLI cwd: ${branch}; Bash output is scripted, not shell execution`);
			for (const refusal of await Promise.all(fetchRefusals))
				expect(refusal.detail === 'A session using this worktree or branch is active. Stop it or wait before changing Git state.', 'only the active Git target safety refusal is expected');
		} finally {
			observation.stop();
		}
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
		await typeAndSend(page, `${token}\nUse opus for this as it needs to be buyllet proof no gpt for this.\n${fakeAiTag(variant, 'tool-native-model-override')}`);
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

const nativePinRetry = {
	name: 'native-pin-retry',
	viewports: ['desktop'],
	async setup(ctx) {
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(ctx.page, 'Bypass');
	},
	async act({ page, mark, variant, log }) {
		const token = `native-pin-retry-${uniq()}`;
		await mark('request a specific native version, then retry its family');
		await typeAndSend(page, `${token}\nCan you use Opus version 4?\n${fakeAiTag(variant, 'tool-native-model-override')}`);
		await page.getByText('Reply yes or no in the message box.', { exact: false }).waitFor({ timeout: 20_000 });
		await mark('decline the current-family substitution');
		await typeAndSend(page, 'no');
		await waitTurnSettled(page, { text: 'Native model checks complete.', messagesAtLeast: 2 });
		const parent = await api(page, 'GET', `/sessions/${await chatIdByToken(page, token)}`);
		const results = parent.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult);
		expect(results.length === 4, `three pin refusals and one denied family retry: ${JSON.stringify(results)}`);
		expect(results.every((r) => r.toolStatus === 'error'), 'no start bypassed the owner prompt');
		expect(/owner declined/.test(results[3].toolResult), 'the family retry required the owner agreement');
		expect(/current version/.test(results[0].toolResult) && /agreement/.test(results[0].toolResult), 'pin refusal explains current-family substitution');
		log('a version-pinned owner request did not silently authorize the current family');
	}
};

async function waitNativeCheck(page, id, calls, answers) {
	await page.waitForFunction(async ({ id, calls, answers }) => {
		const response = await fetch(`/api/v1/sessions/${id}`);
		const session = await response.json();
		return session.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult).length === calls
			&& session.messages.filter((m) => m.type === 'text' && m.content?.includes('Newer native request checks complete.')).length === answers;
	}, { id, calls, answers }, { timeout: 30_000 });
}

const nativePrecedence = {
	name: 'native-precedence',
	viewports: ['desktop'],
	async setup(ctx) {
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(ctx.page, 'Bypass');
	},
	async act({ page, mark, variant, log }) {
		const token = `native-precedence-${uniq()}`;
		await typeAndSend(page, `${token}\nUse opus.\n${fakeAiTag(variant, 'tool-native-model-override')}`);
		await waitTurnSettled(page, { text: 'Native model checks complete.', messagesAtLeast: 2 });
		const id = await chatIdByToken(page, token);
		await mark('a newer version request supersedes the saved plain-family naming');
		await typeAndSend(page, `Can you use Opus version 4?\n${fakeAiTag(variant, 'tool-native-model-precedence')}`);
		await page.getByText('Reply yes or no in the message box.', { exact: false }).waitFor({ timeout: 20_000 });
		await typeAndSend(page, 'no');
		await waitNativeCheck(page, id, 6, 1);
		await waitTurnSettled(page, { text: 'Newer native request checks complete.', messagesAtLeast: 4 });
		const parent = await api(page, 'GET', `/sessions/${await chatIdByToken(page, token)}`);
		const results = parent.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult);
		expect(results.length === 6, `initial family start and newer refusal/retry: ${JSON.stringify(results)}`);
		expect(results[3].toolStatus !== 'error', 'earlier plain-family consent started its chat');
		expect(results[4].toolStatus === 'error' && results[5].toolStatus === 'error', 'newer pin and family substitution did not start');
		expect(/owner declined/.test(results[5].toolResult), 'saved naming did not skip the new owner agreement');
		await mark('a command word in a different clause cannot restore consent');
		await typeAndSend(page, `the opus agent stopped, try again
${fakeAiTag(variant, 'tool-native-model-precedence')}`);
		await page.getByText('Reply yes or no in the message box.', { exact: false }).waitFor({ timeout: 20_000 });
		await typeAndSend(page, 'no');
		await waitNativeCheck(page, id, 8, 2);
		await waitTurnSettled(page, { text: 'Newer native request checks complete.', messagesAtLeast: 6 });
		const narrative = await api(page, 'GET', `/sessions/${await chatIdByToken(page, token)}`);
		const narrativeResults = narrative.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult);
		expect(narrativeResults.length === 8 && narrativeResults.slice(-2).every((r) => r.toolStatus === 'error'), 'narrative did not override the version request');
		await mark('a short family correction restores plain consent');
		await typeAndSend(page, `go with opus
${fakeAiTag(variant, 'tool-native-model-precedence')}`);
		await waitNativeCheck(page, id, 10, 3);
		await waitTurnSettled(page, { text: 'Newer native request checks complete.', messagesAtLeast: 8 });
		const corrected = await api(page, 'GET', `/sessions/${await chatIdByToken(page, token)}`);
		const correctedResults = corrected.messages.filter((m) => m.tool_name === 'StartSession' && m.toolResult);
		expect(correctedResults.length === 10 && correctedResults.at(-1).toolStatus !== 'error', `short correction started without another prompt: ${JSON.stringify(correctedResults.at(-1))}`);
		log('newer version request overrides older naming; narrative cannot restore it, a short instruction can');
	}
};

await runJourney({
	name: 'tool-cards',
	recorder: {
		selectors: [STOP_SELECTOR, 'worklog=[role="region"][aria-label="Work log"]']
	},
	cases: [toolCase('bash'), toolCase('multi'), workerFollowup, nativeModel, nativePinRetry, nativePrecedence],
	allow: [...DEV_INSTANCE_NOISE]
});
