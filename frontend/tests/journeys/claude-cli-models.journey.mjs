// Journey: a Claude subscription's model list comes from Claude Code. The dev
// instance's fake `claude` lists its models in the `initialize` reply the way
// Claude Code 2.1.325 does (scripts/devloop/fake_claude/cli.py, FAKE_CLI_MODELS).
//
// Cases:
//   listed-in-settings  Settings > Providers > Claude > Models: one list of every
//                       model Claude Code offers (aliases, versions, Mythos), no
//                       checkboxes, and the pickers offer all of them
//   pick-and-run        pick Mythos 5.1 in a chat, run a turn: the fake CLI
//                       was started with exactly that model, not an alias
import { readFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import {
	PROJECT,
	api,
	chatIdByToken,
	expect,
	gotoApp,
	isPhone,
	openPopup,
	pick,
	pickAccess,
	pickProject,
	scriptTag,
	showSidebar,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CLAUDE_AGENT = 'Dev (Claude Code)';
const VERSION = 'claude-opus-4-6';
const VERSION_LABEL = 'Opus 4.6';
const MYTHOS = 'claude-mythos-5-1';
const MYTHOS_LABEL = 'Mythos 5.1';

/** The fake CLI's log: one JSON line per event, `start` lines carry argv. */
const cliLog = (status) => readFileSync(status.claude_cli.log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);

async function openClaudeModels(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
	await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
	// The subscription sign-in's own page (a saved sign-in has an id suffix).
	await page.getByRole('button', { name: /^Open Claude .*subscription$/ }).first().click();
	await page.getByRole('tab', { name: 'Models', exact: true }).click();
	await page.getByTestId('claude-cli-models').waitFor({ timeout: 30_000 });
}

/** Fresh Claude Code chat in the Dev sandbox, Bypass. */
async function openClaudeChat({ page, status }) {
	if (!status.seed?.claude_cli?.agent_id) {
		throw new Error(`the dev instance has no Claude Code agent (seed: ${JSON.stringify(status.seed?.claude_cli)})`);
	}
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New chat' });
	await hero.waitFor();
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: CLAUDE_AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${CLAUDE_AGENT}` })
	);
	await pickProject(page, hero, PROJECT);
	await pickAccess(page, 'Bypass');
}

await runJourney({
	name: 'claude-cli-models',
	// What is listed and which model runs do not depend on CPU speed.
	throttles: ['fast'],
	cases: [
		{
			name: 'listed-in-settings',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await openClaudeModels(page, variant);
			},
			async act({ page, mark, log }) {
				await mark('read the Models tab');
				const list = page.getByTestId('claude-cli-models');
				const text = (await list.innerText()).replace(/\s+/g, ' ');
				log(`models: ${text}`);
				for (const wanted of ['Opus 5.5', VERSION_LABEL, VERSION, MYTHOS_LABEL, MYTHOS]) {
					expect(text.includes(wanted), `the Models tab lists ${wanted}: ${text}`);
				}
				expect((await page.getByRole('checkbox').count()) === 0, 'no opt-in checkboxes');
				expect((await page.getByText(/Listed by Claude Code/).count()) === 0, 'no "Listed by Claude Code" section');
				const registry = await api(page, 'GET', '/models');
				const values = registry.models.map((m) => m.value);
				expect(values.includes(VERSION) && values.includes(MYTHOS), `the pickers offer every version: ${values}`);
				expect(registry.models.find((m) => m.value === MYTHOS)?.family === 'mythos', 'Mythos is its own family');
			}
		},
		{
			name: 'pick-and-run',
			async setup({ page, status }) {
				await openClaudeChat({ page, status });
			},
			async act({ page, status, mark, variant, log }) {
				const label = new RegExp(MYTHOS_LABEL.replace('.', '\\.'));
				const logged = cliLog(status).length;
				await mark(`pick ${MYTHOS_LABEL}`);
				const models = page.getByRole('dialog', { name: 'Choose model' });
				await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), models);
				await models.getByRole('textbox', { name: 'Search models' }).fill(MYTHOS_LABEL);
				await models.getByRole('option', { name: new RegExp(`^${label.source}`) }).first().click();
				await page.keyboard.press('Escape');
				await models.waitFor({ state: 'hidden', timeout: 5000 });
				await page.getByRole('button', { name: new RegExp(`^Model: .*${label.source}`) }).first().waitFor();

				await mark('run a turn');
				const token = `cli models ${uniq()}`;
				await typeAndSend(page, `${token} ${scriptTag(variant, 'default')}`);
				await waitTurnSettled(page, { messagesAtLeast: 2 });

				const id = await chatIdByToken(page, token);
				const chat = await api(page, 'GET', `/sessions/${id}`);
				log(`chat ${id} model: ${chat.model}`);
				expect(chat.model === MYTHOS, `the chat saved ${MYTHOS}, not an alias (got ${chat.model})`);
				// What the CLI itself was started with: `--model` of the chat's process.
				const started = cliLog(status).slice(logged).filter((e) => e.kind === 'start' && e.argv?.includes('--model'));
				const ran = started.map((e) => e.argv[e.argv.indexOf('--model') + 1]);
				log(`fake CLI started with --model ${ran.join(', ')}`);
				expect(ran.length > 0 && ran.every((value) => value === MYTHOS), `the CLI ran ${MYTHOS} (got ${ran})`);
				await page.getByRole('button', { name: new RegExp(`^Model: .*${label.source}`) }).first().waitFor();
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
