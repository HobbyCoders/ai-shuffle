// Journey: every new session starts with no project (owner decision
// 2026-11-13, reversing 2026-10-31), and Cmd/Ctrl+P picks one from the
// keyboard. A new-chat draft with typed text keeps the project it was typed
// in; an untyped one left for another chat starts over.
//
// Cases:
//   starts-without-project
//                   pick a project, press New session (on the screen itself,
//                   and again after opening a chat in ANOTHER project), then
//                   reload: every time the screen shows no project.
//   keeps-typed-draft
//                   type a draft in a project, open another chat, press New
//                   session: the draft and its project are back.
//   late-effort-capabilities-keep-controls-still
//                   capability refreshes preserve the open phone sheet geometry.
//   keyboard-picker (desktop) Cmd/Ctrl+P from the message box opens the
//                   project search: type + Enter picks, a digit picks a listed
//                   row, Escape and every pick put the cursor back in the
//                   message box, and the same keys switch or clear a picked
//                   project. Inside a saved chat the keys are left alone.
import { runJourney } from './runner.mjs';
import { PROJECT, SEEDED, composer, expect, openChatSetup, openFromSidebar, openNewChat, settleServiceWorker, showSidebar, uniq, waitChat, waitChatOpen, withChatSetup } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const hero = (page) => page.getByRole('region', { name: 'New chat' });
const projectWord = (page, project) => hero(page).getByRole('button', { name: `Workspace folders: ${project}` });
const noProjectWord = (page) => hero(page).getByRole('button', { name: /^No project/ });

/** Press the sidebar's New session (opening the drawer first on a phone). */
async function pressNewSession(page) {
	await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New chat', exact: true }).click();
	await waitChat(page, { surface: 'new' });
}

/** The screen shows `project` (or no project for null), and still does a moment later. */
async function expectProject(page, project, log) {
	const want = project ? projectWord(page, project) : noProjectWord(page);
	const other = project ? noProjectWord(page) : hero(page).getByRole('button', { name: /^Workspace folders: / });
	await withChatSetup(page, async () => {
		await want.waitFor({ timeout: 30_000 });
		// Keep the same observation window, but retain the model/capability and
		// group geometry when a late load moves the phone sheet (#1193). A final
		// screenshot cannot show which row appeared or disappeared mid-window.
		const changes = await page.evaluate(() => new Promise(resolve => {
			const samples = [];
			const start = performance.now();
			let last = '', frame;
			const sample = () => {
				const sheet = document.querySelector('.mcp.shown');
				const state = sheet ? {
					model: sheet.querySelector('button[aria-label^="Model: "]')?.getAttribute('aria-label'),
					effort: sheet.querySelector('[data-row="effort"]')?.textContent,
					groups: [...sheet.querySelectorAll('.mcp-group')].map(node => {
						const rect = node.getBoundingClientRect();
						return { top: Math.round((rect.top - sheet.getBoundingClientRect().top) * 10) / 10, height: Math.round(rect.height * 10) / 10 };
					})
				} : null;
				const signature = JSON.stringify(state);
				if (signature !== last && samples.length < 40) {
					samples.push({ at: Math.round(performance.now() - start), ...state });
					last = signature;
				}
				frame = requestAnimationFrame(sample);
			};
			sample();
			setTimeout(() => { cancelAnimationFrame(frame); resolve(samples); }, 1500);
		}));
		log?.(`controls-geometry ${JSON.stringify(changes)}`);
		if (!(await want.isVisible()) || (await other.isVisible())) {
			throw new Error(`expected the new-session screen to show ${project ? `project "${project}"` : 'no project'}`);
		}
	});
}

/** Reload; a new step once the old page is gone, so the reload itself is not judged as going back. */
async function reload(page, mark) {
	await mark('reload');
	await page.reload({ waitUntil: 'domcontentloaded' });
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
	await mark('page reloaded');
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
}

await runJourney({
	name: 'new-chat-project',
	cases: [
		{
			name: 'starts-without-project',
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				await openNewChat(ctx, { project: PROJECT });
			},
			async act({ page, mark, log }) {
				await mark('New session on the new-session screen');
				await pressNewSession(page);
				await expectProject(page, null, log);

				await mark('open a chat in another project');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChatOpen(page);
				await mark('New session from that chat');
				await pressNewSession(page);
				await expectProject(page, null, log);

				await reload(page, mark);
				await expectProject(page, null, log);
			}
		},
		{
			name: 'keeps-typed-draft',
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				await openNewChat(ctx, { project: PROJECT });
			},
			async act({ page, mark, log }) {
				const draft = `draft ${uniq()}`;
				await mark('type a draft in the project');
				await composer(page).click();
				await page.keyboard.type(draft);
				await mark('open a chat in another project');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChatOpen(page);
				await mark('New session from that chat');
				await pressNewSession(page);
				await expectProject(page, PROJECT, log);
				expect((await composer(page).innerText()).includes(draft), 'the typed draft comes back with its project');
			}
		},
		{
			name: 'late-effort-capabilities-keep-controls-still',
			viewports: ['phone'],
			async setup(ctx) {
				const { page, status } = ctx;
				await settleServiceWorker(page, status);
				await openNewChat(ctx, { project: PROJECT });
				// Pick through the real model picker; no component/store is mounted
				// or mutated by the test. The model identity stays constant below.
				await page.getByRole('button', { name: /^Model: / }).first().click();
				await page.getByRole('option', { name: /^Sonnet\b/ }).click();
				await page.getByRole('button', { name: 'Model: Sonnet', exact: true }).first().waitFor();
				await page.locator('.mcp-scrim').waitFor({ state: 'detached' });
				await openChatSetup(page);
				await page.evaluate(() => {
					const original = window.fetch.bind(window);
					const catalog = window.__journeyEffortCatalog = { mode: 'known', applied: null };
					window.fetch = async (input, init) => {
						const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
						const mode = catalog.mode;
						const response = await original(input, init);
						if (path !== '/api/v1/models' || !response.ok) return response;
						const payload = await response.clone().json();
						const model = payload.models.find(model => model.value === 'sonnet');
						if (!model || !Array.isArray(model.effort_levels) || model.effort_levels.length === 0) {
							throw new Error('the real Sonnet catalog must declare adjustable effort levels');
						}
						// Exercise the API-input capability transition deterministically:
						// unknown/explicitly fixed metadata, then the untouched real read.
						// This does NOT claim to reproduce #1193's unproven CI producer.
						if (mode !== 'known') model.effort_levels = mode === 'unknown' ? null : [];
						catalog.applied = mode;
						return new Response(JSON.stringify(payload), { status: response.status, headers: response.headers });
					};
				});
				await page.waitForTimeout(600);
			},
			async act({ page, mark, log }) {
				const sheet = page.getByRole('dialog', { name: 'Chat controls', exact: true });
				const groups = () => page.locator('.mcp.shown .mcp-group').evaluateAll(nodes => nodes.map(node => {
					const rect = node.getBoundingClientRect();
					return { top: rect.top, height: rect.height };
				}));
				const before = await groups();
				for (const mode of ['unknown', 'known', 'fixed', 'known']) {
					await mark(`effort capabilities become ${mode} with Chat controls open`);
					await page.evaluate(mode => {
						window.__journeyEffortCatalog.mode = mode;
						window.__journeyEffortCatalog.applied = null;
						// The app's real live-sync online handler refreshes the catalog.
						window.dispatchEvent(new Event('online'));
					}, mode);
					await page.waitForFunction(mode => window.__journeyEffortCatalog.applied === mode, mode);
					await page.waitForFunction(mode => {
						const choices = document.querySelector('.mcp.shown [data-row="effort"] [role="radiogroup"]');
						return Boolean(choices) === (mode === 'known');
					}, mode);
					await page.waitForTimeout(600);
					const after = await groups();
					log(`effort-capabilities ${JSON.stringify({ mode, before, after })}`);
					expect(before.length > 1 && before.length === after.length &&
						before.every((group, i) => Math.abs(group.top - after[i].top) <= 1 && Math.abs(group.height - after[i].height) <= 1),
						`${mode} effort metadata must not resize or move the open controls groups`);
					expect(await sheet.getByRole('button', { name: 'Model: Sonnet', exact: true }).isVisible(), 'capability refresh must keep the selected model');
					if (mode !== 'known') {
						const note = mode === 'unknown' ? /Reasoning effort capabilities are not known yet/ : /Adjustable reasoning effort is not available/;
						expect(await sheet.getByText(note).isVisible(), `${mode} capabilities need a truthful explanation, not stale choices`);
					}
				}
			}
		},
		{
			name: 'keyboard-picker',
			viewports: ['desktop'],
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				await openNewChat(ctx);
			},
			async act({ page, mark, log }) {
				const search = page.getByRole('textbox', { name: 'Search projects' });
				const inEditor = () => page.evaluate(() => !!document.activeElement?.closest('[data-message-editor]'));
				const openPicker = async () => {
					await page.keyboard.press('ControlOrMeta+p');
					await search.waitFor({ timeout: 10_000 });
					expect(await search.evaluate(node => node === document.activeElement), 'the search field has the cursor');
				};

				await mark('Cmd/Ctrl+P from the message box');
				await composer(page).click();
				await openPicker();
				const rows = await page.locator('[data-project-picker] .panel-scroll .menu-item .item-name').allInnerTexts();
				log(`picker-rows ${JSON.stringify(rows)}`);
				expect(rows.includes(PROJECT), 'the picker lists the projects');

				await mark('Escape goes back to the message box');
				await page.keyboard.press('Escape');
				await search.waitFor({ state: 'detached' });
				expect(await inEditor(), 'Escape puts the cursor back in the message box');
				await expectProject(page, null, log);

				await mark('type part of a name and press Enter');
				await openPicker();
				await page.keyboard.type(PROJECT.slice(0, 5).toLowerCase());
				await page.keyboard.press('Enter');
				await expectProject(page, PROJECT, log);
				expect(await inEditor(), 'a pick puts the cursor back in the message box');

				await mark('the same keys switch: 0 is No project');
				await openPicker();
				expect(await page.getByRole('dialog', { name: 'Switch project' }).isVisible(), 'a picked project opens the switcher');
				await page.keyboard.press('0');
				await expectProject(page, null, log);

				await mark('a digit picks that row');
				await openPicker();
				const first = await page.locator('[data-project-picker] .panel-scroll .menu-item .item-name').first().innerText();
				await page.keyboard.press('1');
				await expectProject(page, first, log);
				expect(await inEditor(), 'a digit pick puts the cursor back in the message box');

				await mark('inside a saved chat the keys are left to the browser');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChatOpen(page);
				const prevented = await page.evaluate(() => {
					const target = document.querySelector('[data-message-editor]') ?? document.body;
					const event = new KeyboardEvent('keydown', { key: 'p', ctrlKey: true, metaKey: /Mac/.test(navigator.platform), bubbles: true, cancelable: true });
					target.dispatchEvent(event);
					return event.defaultPrevented;
				});
				expect(!prevented, 'Cmd/Ctrl+P in a saved chat must not be taken');
				expect(!(await search.isVisible()), 'no picker in a saved chat');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
