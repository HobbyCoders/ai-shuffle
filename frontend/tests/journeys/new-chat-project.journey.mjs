// Journey: the new-session screen opens on the owner's last project choice,
// "no project" included (owner decision 2026-10-31). Someone working in a
// project should not have to pick it again for every session; clearing it is
// a choice too and sticks until a project is picked again.
//
// The preview cookie cannot write account preferences, so on the dev
// instance the choice lives in this browser's storage; every case gets a
// fresh browser, so no case sees another's choice.
//
// Cases:
//   keeps-project   pick a project, press New session (on the screen itself,
//                   and again after opening a chat in ANOTHER project), then
//                   reload: every time the screen shows the picked project.
//   keeps-cleared   pick a project, clear it, press New session and reload:
//                   the screen stays without a project.
//   late-effort-capabilities-keep-controls-still
//                   capability refreshes preserve the open phone sheet geometry.
import { runJourney } from './runner.mjs';
import { PROJECT, SEEDED, expect, openChatSetup, openFromSidebar, openNewChat, pickProject, settleServiceWorker, showSidebar, waitChat, waitChatOpen, withChatSetup } from './lib/app.mjs';
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
			name: 'keeps-project',
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				await openNewChat(ctx, { project: PROJECT });
			},
			async act({ page, mark, log }) {
				await mark('New session on the new-session screen');
				await pressNewSession(page);
				await expectProject(page, PROJECT, log);

				await mark('open a chat in another project');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChatOpen(page);
				await mark('New session from that chat');
				await pressNewSession(page);
				await expectProject(page, PROJECT, log);

				await reload(page, mark);
				await expectProject(page, PROJECT, log);
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
			name: 'keeps-cleared',
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				const heroRegion = await openNewChat(ctx, { project: PROJECT });
				const { page } = ctx;
				const folders = page.getByRole('dialog', { name: 'Workspace folders' });
				await withChatSetup(page, async () => {
					await projectWord(page, PROJECT).click();
					await folders.waitFor();
					await folders.getByRole('button', { name: `Remove ${PROJECT}: start without a project` }).click();
					await heroRegion.getByRole('button', { name: /^No project/ }).waitFor();
				});
			},
			async act({ page, mark, log }) {
				await mark('New session after clearing the project');
				await pressNewSession(page);
				await expectProject(page, null, log);

				await reload(page, mark);
				await expectProject(page, null, log);

				await mark('pick the project again');
				await pickProject(page, hero(page));
				await mark('New session after picking it again');
				await pressNewSession(page);
				await expectProject(page, PROJECT, log);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
