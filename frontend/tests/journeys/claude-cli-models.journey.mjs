// Journey: a Claude subscription's model list comes from Claude Code. The dev
// instance's fake `claude` lists its models in the `initialize` reply the way
// Claude Code 2.1.325 does (scripts/devloop/fake_claude/cli.py, FAKE_CLI_MODELS).
//
// Cases:
//   listed-in-settings  Settings > Providers > Claude > Models: curated checklist
//                       of every model Claude Code offers (aliases, versions,
//                       Mythos, unknown family), filterable, and unchecking a
//                       model removes it from /api/v1/models while keeping it
//                       checkable in Settings
//   pick-and-run        pick Mythos 5.1 in a chat, run a turn (CLI started with
//                       claude-mythos-5-1), then uncheck Mythos in Settings:
//                       the picker hides Mythos while the existing Mythos chat
//                       still displays "Mythos 5.1"
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
/** The fake CLI's entry from a family this release does not know. */
const UNKNOWN = 'claude-zephyr-1';
const UNKNOWN_LABEL = 'Zephyr 1';

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

async function backToChatsFromSettings(page, variant) {
	if (isPhone(variant)) {
		const backToChats = page.getByRole('button', { name: 'Back to chats', exact: true });
		for (let i = 0; i < 4 && !(await backToChats.isVisible()); i++) {
			const backBtn = page.getByRole('button', { name: /^Back to / }).first();
			if (await backBtn.isVisible()) await backBtn.click();
			else break;
		}
	}
	await page.getByRole('button', { name: 'Back to chats', exact: true }).click();
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
				const panel = page.getByRole('tabpanel', { name: 'Models' });
				const list = panel.getByTestId('claude-cli-models');
				const text = (await list.innerText()).replace(/\s+/g, ' ');
				log(`models: ${text}`);
				for (const wanted of ['Opus 5.5', VERSION_LABEL, VERSION, MYTHOS_LABEL, MYTHOS, UNKNOWN_LABEL, UNKNOWN]) {
					expect(text.includes(wanted), `the Models tab lists ${wanted}: ${text}`);
				}
				const rows = list.locator('.pv-mrow');
				const total = await rows.count();
				expect(await rows.filter({ has: page.locator('[data-provider-logo="claude"]:visible') }).count() === total,
					'every model in the checklist shows the Claude mark');
				const checkboxes = list.getByRole('checkbox');
				expect(await checkboxes.count() === total, 'every subscription model has a checkbox');
				expect((await page.getByText(/Listed by Claude Code/).count()) === 0, 'no "Listed by Claude Code" section');

				const registry = await api(page, 'GET', '/models');
				const values = registry.models.map((m) => m.value);
				expect(values.includes(VERSION) && values.includes(MYTHOS), `the pickers offer every version: ${values}`);
				expect(registry.models.find((m) => m.value === MYTHOS)?.family === 'mythos', 'Mythos is its own family');
				const unknown = registry.models.find((m) => m.value === UNKNOWN);
				expect(unknown && unknown.family === null, `an unknown family stays selectable: ${JSON.stringify(unknown)}`);

				await mark('filter models and uncheck then re-check one version');
				const filterInput = panel.getByRole('searchbox', { name: 'Filter models', exact: true });
				await filterInput.fill(VERSION);
				expect(await rows.count() === 1, 'filter isolates the matching model');
				const rowCheckbox = list.getByRole('checkbox').first();
				expect(await rowCheckbox.isChecked(), 'model starts checked');

				const saveDisable = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().endsWith('/api/v1/models/claude/subscription/models'));
				await rowCheckbox.click();
				expect((await saveDisable).ok(), 'disabling a Claude subscription model must succeed');
				expect(!(await rowCheckbox.isChecked()), 'disabled model stays visible and unchecked');

				const afterDisable = await api(page, 'GET', '/models');
				expect(!afterDisable.models.some((m) => m.value === VERSION),
					'disabled subscription model is removed from /api/v1/models');
				expect(afterDisable.hidden_models?.some((m) => m.value === VERSION && m.label === VERSION_LABEL),
					'disabled subscription model keeps its live metadata in hidden_models');

				const saveEnable = page.waitForResponse((res) =>
					res.request().method() === 'PUT' && res.url().endsWith('/api/v1/models/claude/subscription/models'));
				await rowCheckbox.click();
				expect((await saveEnable).ok(), 're-enabling a Claude subscription model must succeed');
				expect(await rowCheckbox.isChecked(), 're-enabled model is checked again');

				const afterEnable = await api(page, 'GET', '/models');
				expect(afterEnable.models.some((m) => m.value === VERSION),
					're-enabled subscription model returns to /api/v1/models');

				await filterInput.fill('');
				expect(await rows.count() === total, 'clearing filter shows all models again');
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

				// S2 end-to-end: unchecking Mythos hides it from the picker while this
				// existing Mythos chat continues to display "Mythos 5.1" (never raw id).
				await mark('hide Mythos and verify existing chat keeps Mythos 5.1 label');
				try {
					await openClaudeModels(page, variant);
					const panel = page.getByRole('tabpanel', { name: 'Models' });
					const filter = panel.getByRole('searchbox', { name: 'Filter models', exact: true });
					await filter.fill(MYTHOS);
					const mythosBox = panel.getByTestId('claude-cli-models').getByRole('checkbox').first();
					const hideSave = page.waitForResponse((res) =>
						res.request().method() === 'PUT' && res.url().endsWith('/api/v1/models/claude/subscription/models'));
					await mythosBox.click();
					expect((await hideSave).ok(), 'unchecking Mythos must succeed');

					await mark('return to existing Mythos chat and verify label + picker');
					await backToChatsFromSettings(page, variant);
					await waitChat(page, { surface: 'active' }, { timeout: 30_000 });
					await page.getByRole('button', { name: new RegExp(`^Model: .*${label.source}`) }).first().waitFor({ timeout: 10_000 });

					const picker = page.getByRole('dialog', { name: 'Choose model' });
					await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), picker);
					await picker.getByRole('textbox', { name: 'Search models' }).fill(MYTHOS_LABEL);
					expect(await picker.getByRole('option', { name: new RegExp(`^${label.source}`) }).count() === 0,
						'hidden Mythos 5.1 must not appear in the model picker');
					await page.keyboard.press('Escape');
					if (await picker.isVisible()) await page.keyboard.press('Escape');
					await picker.waitFor({ state: 'hidden', timeout: 5000 });
				} finally {
					// Restore Mythos 5.1 so the dev instance stays clean for subsequent variants/runs.
					await api(page, 'PUT', '/models/claude/subscription/models', { model: MYTHOS, enabled: true }).catch(() => {});
				}
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
