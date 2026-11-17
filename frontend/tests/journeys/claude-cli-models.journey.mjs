// Journey: a Claude subscription's model list comes from Claude Code. The dev
// instance's fake `claude` lists its models in the `initialize` reply the way
// Claude Code 2.1.325 does (scripts/devloop/fake_claude/cli.py, FAKE_CLI_MODELS).
//
// Cases:
//   enable-in-settings  Settings > Providers > Claude > Models: a version is off
//                       by default; checking it saves and the composer offers it
//   pick-and-run        pick that version in a chat, run a turn: the fake CLI
//                       was started with exactly that model, not the alias
//
// The throwaway instance's preview cookie may turn a model on or off
// (app/api/auth.py, _preview_may_change_dev_data); a real install's may not.
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

/** The fake CLI's log: one JSON line per event, `start` lines carry argv. */
const cliLog = (status) => readFileSync(status.claude_cli.log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);

const setEnabled = (page, enabled) => api(page, 'PUT', '/models/claude/enabled', { model: VERSION, enabled });

async function offered(page) {
	const registry = await api(page, 'GET', '/models');
	return registry.models.some((m) => m.value === VERSION);
}

async function openClaudeModels(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
	await page.locator('[data-settings-item="provider:claude"]:visible').first().click();
	// The subscription sign-in's own page (a saved sign-in has an id suffix).
	await page.getByRole('button', { name: /^Open Claude .*subscription$/ }).first().click();
	await page.getByRole('tab', { name: 'Models', exact: true }).click();
	await page.getByTestId('claude-versioned-models').waitFor({ timeout: 30_000 });
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

const idleTabs = new WeakMap();
const effortButton = (page) => page.getByRole('button', { name: /^Effort:/ }).first();
const chatControls = (page) => page.getByRole('button', { name: /^All chat controls/ });
const effortRadio = (page, label) => page.getByRole('radio', { name: label, exact: true });

async function pickXHigh(page) {
	if (await chatControls(page).isVisible()) {
		await openPopup(chatControls(page), effortRadio(page, 'XHigh'));
		await effortRadio(page, 'XHigh').click();
		await page.getByRole('button', { name: 'Close chat controls' }).click();
	} else {
		const slider = page.getByRole('slider', { name: 'Reasoning effort' });
		await openPopup(effortButton(page), slider);
		await slider.press('End');
		await slider.press('ArrowLeft');
		await page.keyboard.press('Escape');
	}
	await checkEffort(page, 'XHigh');
}

async function checkEffort(page, label) {
	if (await chatControls(page).isVisible()) {
		await openPopup(chatControls(page), effortRadio(page, label));
		await page.getByRole('radio', { name: label, exact: true, checked: true }).waitFor({ timeout: 5000 });
		await page.getByRole('button', { name: 'Close chat controls' }).click();
	} else {
		await page.getByRole('button', { name: `Effort: ${label}`, exact: true }).first().waitFor({ timeout: 5000 });
	}
}

async function chooseModel(page, label) {
	const modelButton = page.getByRole('button', { name: /^Model:/ }).first();
	const models = page.getByRole('dialog', { name: 'Choose model' });
	await openPopup(modelButton, models);
	await models.getByRole('textbox', { name: 'Search models' }).fill(label);
	await models.getByRole('option', { name: new RegExp(`^${label.replace('.', '\\.')}\\b`) }).first().click();
	await models.waitFor({ state: 'hidden', timeout: 5000 });
	await page.getByRole('button', { name: new RegExp(`^Model: .*${label.replace('.', '\\.')}`) }).first().waitFor();
}

await runJourney({
	name: 'claude-cli-models',
	// What is listed and which model runs do not depend on CPU speed.
	throttles: ['fast'],
	cases: [
		{
			name: 'enable-in-settings',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await setEnabled(page, false);
				await openClaudeModels(page, variant);
			},
			async act({ page, mark, log }) {
				try {
					const aliases = (await page.getByTestId('claude-alias-models').innerText()).replace(/\s+/g, ' ');
					log(`aliases: ${aliases}`);
					expect(/Opus 5\.5/.test(aliases), `the aliases are labelled as Claude Code names them: ${aliases}`);
					const box = page.getByTestId('claude-versioned-models').getByRole('checkbox', { name: /\bOpus 4\.6\b/ });
					expect(!(await box.isChecked()), `${VERSION_LABEL} is off by default`);
					expect(!(await offered(page)), `${VERSION} is not in the pickers yet`);

					await mark(`turn ${VERSION_LABEL} on`);
					await box.click();
					await page.waitForFunction(
						async (value) => (await (await fetch('/api/v1/models', { credentials: 'include' })).json()).models.some((m) => m.value === value),
						VERSION,
						{ timeout: 10_000, polling: 500 }
					);
					expect(await box.isChecked(), `${VERSION_LABEL} reads on after saving`);
					expect((await page.locator('.pv-msg.err[role="alert"]').count()) === 0, 'no save error');

					await mark(`turn ${VERSION_LABEL} off again`);
					await box.click();
					await page.waitForFunction(
						async (value) => !(await (await fetch('/api/v1/models', { credentials: 'include' })).json()).models.some((m) => m.value === value),
						VERSION,
						{ timeout: 10_000, polling: 500 }
					);
					expect(!(await box.isChecked()), `${VERSION_LABEL} reads off after saving`);
				} finally {
					await setEnabled(page, false).catch(() => {});
				}
			}
		},
		{
			name: 'pick-and-run',
			async setup({ page, status }) {
				await gotoApp(page, status);
				await setEnabled(page, true);
				await openClaudeChat({ page, status });
			},
			async act({ page, status, mark, variant, log }) {
				try {
					const logged = cliLog(status).length;
					await mark(`pick ${VERSION_LABEL}`);
					const models = page.getByRole('dialog', { name: 'Choose model' });
					await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), models);
					await models.getByRole('textbox', { name: 'Search models' }).fill(VERSION_LABEL);
					await models.getByRole('option', { name: new RegExp(`^${VERSION_LABEL.replace('.', '\\.')}`) }).first().click();
					await page.keyboard.press('Escape');
					await models.waitFor({ state: 'hidden', timeout: 5000 });
					await page.getByRole('button', { name: new RegExp(`^Model: .*${VERSION_LABEL.replace('.', '\\.')}`) }).first().waitFor();

					await mark('run a turn');
					const token = `cli models ${uniq()}`;
					await typeAndSend(page, `${token} ${scriptTag(variant, 'default')}`);
					await waitTurnSettled(page, { messagesAtLeast: 2 });

					const id = await chatIdByToken(page, token);
					const chat = await api(page, 'GET', `/sessions/${id}`);
					log(`chat ${id} model: ${chat.model}`);
					expect(chat.model === VERSION, `the chat saved ${VERSION}, not an alias (got ${chat.model})`);
					// What the CLI itself was started with: `--model` of the chat's process.
					const started = cliLog(status).slice(logged).filter((e) => e.kind === 'start' && e.argv?.includes('--model'));
					const ran = started.map((e) => e.argv[e.argv.indexOf('--model') + 1]);
					log(`fake CLI started with --model ${ran.join(', ')}`);
					expect(ran.length > 0 && ran.every((value) => value === VERSION), `the CLI ran ${VERSION} (got ${ran})`);
					await page.getByRole('button', { name: new RegExp(`^Model: .*${VERSION_LABEL.replace('.', '\\.')}`) }).first().waitFor();
				} finally {
					await setEnabled(page, false).catch(() => {});
				}
			}
		},
		...[0, 7000].map((saveDelay) => ({
			name: saveDelay ? 'effort-fit-notice-delayed' : 'effort-fit-notice',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await setEnabled(page, true);
				await openClaudeChat({ page, status });
				await chooseModel(page, 'Opus 5.5');
				await pickXHigh(page);
				const token = `effort fit ${uniq()}`;
				await typeAndSend(page, `${token} ${scriptTag(variant, 'default')}`);
				await waitTurnSettled(page, { messagesAtLeast: 2 });
				await checkEffort(page, 'XHigh');

				const session = await chatIdByToken(page, token);
				const idle = await page.context().newPage();
				await gotoApp(idle, status, `/?session=${encodeURIComponent(session)}`);
				idleTabs.set(page, idle);
				await page.bringToFront();
			},
			async act({ page, mark, log }) {
				const idle = idleTabs.get(page);
				const anyNotice = (tab) => tab.getByRole('status').filter({ has: tab.getByText(/Reasoning effort changed/) });
				try {
					const isFit = (request) => request.method() === 'PUT' &&
						new URL(request.url()).pathname.endsWith('/run-settings') && request.postDataJSON()?.effort === 'high';
					if (saveDelay) {
						await page.route('**/run-settings', async (route) => {
							if (isFit(route.request())) await new Promise(resolve => setTimeout(resolve, saveDelay));
							await route.continue();
						});
					}
					await mark('switch Opus 5.5 XHigh to Opus 4.6');
					await chooseModel(page, VERSION_LABEL);
					if (saveDelay) {
						expect(await anyNotice(page).count() === 0, 'do not announce an unconfirmed fit');
						const saved = await page.waitForResponse(r => isFit(r.request()));
						expect(saved.ok(), `delayed run-settings returned ${saved.status()}`);
						log(`delayed ${saveDelay} ms; run-settings returned HTTP ${saved.status()}`);
					}
					const notice = anyNotice(page);
					await notice.waitFor({ timeout: 3000 });
					const text = await notice.getByText('Reasoning effort changed from XHigh to High.', { exact: true }).innerText();
					expect(text === 'Reasoning effort changed from XHigh to High.', `status names both levels (got ${text})`);
					log(`polite status: ${text}`);
					expect(await notice.count() === 1, 'exactly one status in the tab that picked');
					expect(await anyNotice(idle).count() === 0, 'no notice in the idle tab');
					const dismiss = notice.getByRole('button', { name: 'Dismiss' });
					await dismiss.focus();
					await dismiss.press('Enter');
					await notice.waitFor({ state: 'hidden' });
					await checkEffort(page, 'High');

					await mark('switch to Sonnet 5.5, which also offers High');
					await chooseModel(page, 'Sonnet 5.5');
					await checkEffort(page, 'High');
					expect(await anyNotice(page).count() === 0, 'no notice when High is unchanged');
					expect(await anyNotice(idle).count() === 0, 'still no notice in the idle tab');
					log('unchanged High and idle tab: no status');
				} finally {
					await idle.close();
					await setEnabled(page, false).catch(() => {});
				}
			}
		}))
	],
	allow: [...DEV_INSTANCE_NOISE]
});
