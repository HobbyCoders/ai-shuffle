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
import { runJourney } from './runner.mjs';
import { PROJECT, SEEDED, openFromSidebar, openNewChat, pickProject, settleServiceWorker, showSidebar, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const hero = (page) => page.getByRole('region', { name: 'New session' });
const projectWord = (page, project) => hero(page).getByRole('button', { name: `Workspace folders: ${project}` });
const noProjectWord = (page) => hero(page).getByRole('button', { name: /^No project/ });

/** Press the sidebar's New session (opening the drawer first on a phone). */
async function pressNewSession(page) {
	await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New session', exact: true }).click();
	await waitChat(page, { surface: 'new' });
}

/** The screen shows `project` (or no project for null), and still does a moment later. */
async function expectProject(page, project) {
	const want = project ? projectWord(page, project) : noProjectWord(page);
	const other = project ? noProjectWord(page) : hero(page).getByRole('button', { name: /^Workspace folders: / });
	await want.waitFor({ timeout: 30_000 });
	// Late boot loads and the catalog answering must not take it back.
	await page.waitForTimeout(1500);
	if (!(await want.isVisible()) || (await other.isVisible())) {
		throw new Error(`expected the new-session screen to show ${project ? `project "${project}"` : 'no project'}`);
	}
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
			async act({ page, mark }) {
				await mark('New session on the new-session screen');
				await pressNewSession(page);
				await expectProject(page, PROJECT);

				await mark('open a chat in another project');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChatOpen(page);
				await mark('New session from that chat');
				await pressNewSession(page);
				await expectProject(page, PROJECT);

				await reload(page, mark);
				await expectProject(page, PROJECT);
			}
		},
		{
			name: 'keeps-cleared',
			async setup(ctx) {
				await settleServiceWorker(ctx.page, ctx.status);
				const heroRegion = await openNewChat(ctx, { project: PROJECT });
				const { page } = ctx;
				const folders = page.getByRole('dialog', { name: 'Workspace folders' });
				await projectWord(page, PROJECT).click();
				await folders.waitFor();
				await folders.getByRole('button', { name: `Remove ${PROJECT}: start without a project` }).click();
				await heroRegion.getByRole('button', { name: /^No project/ }).waitFor();
			},
			async act({ page, mark }) {
				await mark('New session after clearing the project');
				await pressNewSession(page);
				await expectProject(page, null);

				await reload(page, mark);
				await expectProject(page, null);

				await mark('pick it again: the next New session has it');
				await pickProject(page, hero(page));
				await pressNewSession(page);
				await expectProject(page, PROJECT);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
