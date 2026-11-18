// Selected desktop details must gain a usable Back step when resized into a stack.
import { runJourney } from './runner.mjs';
import { SEEDED, chatState, expect, gotoApp, holdBootLoad, openFromSidebar, reloadForHistory, sidebarRow, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');
/** The chat the reload case's fresh load lands on, by its surface tag. */
let landedOn = null;

await runJourney({
	name: 'view-shell-back',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'project-drafts-do-not-cross-identity',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
			await page.getByRole('button', { name: 'Open Dev sandbox', exact: true }).click();
			await page.getByPlaceholder('Project name').waitFor();
		},
		async act({ page, mark }) {
			await mark('dirty project A without saving');
			await page.getByPlaceholder('Project name').fill('Unsaved project A');
			await page.getByPlaceholder('Project path').fill('/unsaved-project-a');
			await mark('switch to project B');
			await page.getByRole('button', { name: 'Open Dev other', exact: true }).click();
			expect(await page.getByPlaceholder('Project name').inputValue() === 'Dev other', 'B never inherits A’s unsaved name');
			expect(!(await page.getByPlaceholder('Project path').inputValue()).includes('unsaved-project-a'), 'B never inherits A’s unsaved path');
		}
	}, {
		name: 'back-across-retained-destinations',
		viewports: ['phone'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await page.getByRole('button', { name: /^Chats(?: \(|$)/ }).first().click();
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Notes', exact: true }).click();
			await page.getByRole('button', { name: 'New note', exact: true }).first().click();
			await page.locator('[data-view-shell="Notes"][data-pane="detail"]').waitFor();
		},
		async act({ page, mark }) {
			await mark('Back reaches the Notes list and its global menu, then Files');
			await page.locator('.topbar').getByRole('button', { name: 'Back to Notes', exact: true }).click();
			await page.locator('[data-view-shell="Notes"][data-pane="panel"]').waitFor();
			await page.getByRole('button', { name: /^Chats(?: \(|$)/ }).first().click();
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Files', exact: true }).click();
			await page.getByRole('button', { name: 'Workspace', exact: true }).click();
			await page.locator('[data-view-shell="Files"][data-pane="detail"]').waitFor();
			await mark('Back closes Files detail');
			await page.locator('.topbar').getByRole('button', { name: 'Back to Files', exact: true }).click();
			await page.locator('[data-view-shell="Files"][data-pane="panel"]').waitFor();
			await page.goForward();
			await page.locator('[data-view-shell="Files"][data-pane="detail"]').waitFor();
		}
	}, {
		name: 'selected-desktop-detail-resized-to-phone',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await page.setViewportSize({ width: 1440, height: 1000 });
			await gotoApp(page, status);
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Agents', exact: true }).click();
			await page.locator('.page-panel [data-settings-item]').first().click();
			await page.locator('[data-view-shell="Agents"] .st-dwrap').waitFor();
		},
		async act({ page, mark, log }) {
			await mark('resize the selected desktop detail through the medium desktop frame into stacked');
			await page.setViewportSize({ width: 1000, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-docked]:not([data-stacked])').waitFor();
			await page.setViewportSize({ width: 390, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-stacked][data-pane="detail"]').waitFor();
			await page.waitForTimeout(300);
			await mark('widen and re-enter the same selected detail without another selection');
			await page.setViewportSize({ width: 1440, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-docked]').waitFor();
			await page.setViewportSize({ width: 1000, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-docked]:not([data-stacked])').waitFor();
			await page.setViewportSize({ width: 390, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-stacked][data-pane="detail"]').waitFor();
			await page.waitForTimeout(300);
			log(`before Back: ${JSON.stringify(await page.evaluate(() => ({ state: history.state, length: history.length })))}`);
			await mark('Back returns the selected detail to the list');
			await page.getByRole('button', { name: 'Back to Agents', exact: true }).click();
			await page.locator('[data-view-shell="Agents"][data-pane="panel"]').waitFor({ timeout: 5000 });
			expect(await page.getByPlaceholder('Search agents').isVisible(), 'the list is visible after Back');
		}
	}, {
		name: `${mobileShell ? 'native' : 'responsive'}-detail-browser-back`,
		viewports: ['phone'],
		async setup({ page, status }) {
			if (mobileShell) {
				await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
				expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
				await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
			} else {
				await gotoApp(page, status);
				await page.getByRole('button', { name: /^Chats(?: \(|$)/ }).first().click();
			}
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Agents', exact: true }).click();
			await page.getByPlaceholder('Search agents').waitFor();
		},
		async act({ page, mark }) {
			await mark(mobileShell ? 'native create action is a reachable 44px top-bar control' : 'responsive create action stays inside the phone viewport');
			const create = page.getByRole('button', { name: 'New agent', exact: true });
			const rect = await create.boundingBox();
			expect(rect && rect.x >= 0 && rect.x + rect.width <= 390, 'create stays inside the phone viewport');
			expect(rect.width >= 44 && rect.height >= 44 && rect.y < 56, 'K1: both shells lend their 44px create target to the top bar');
			const search = await page.getByPlaceholder('Search agents').locator('..').boundingBox();
			expect(search && search.y === 68, 'K1: list search starts 12px below the 56px top bar');
			await create.click();
			const dialog = page.getByRole('dialog', { name: 'New agent', exact: true });
			await dialog.waitFor();
			await dialog.getByRole('button', { name: 'Close', exact: true }).click();
			await page.getByPlaceholder('Search agents').waitFor();
			for (let cycle = 1; cycle <= 2; cycle++) {
				await mark(`open a ${mobileShell ? 'native' : 'responsive'} detail then return with browser Back (cycle ${cycle})`);
				await page.locator('[data-view-shell="Agents"] [data-settings-item]').first().click();
				await page.locator('[data-view-shell="Agents"][data-pane="detail"]').waitFor();
				const back = page.locator('.topbar').getByRole('button', { name: 'Back to Agents', exact: true });
				await back.waitFor();
				expect(await back.isVisible(), 'detail has an on-screen Back control');
				const backRect = await back.boundingBox();
				expect(backRect && backRect.y < 56 && backRect.height >= 44, 'K2: parent Back is a 44px control in the single phone bar');
				expect(await page.getByRole('button', { name: 'New agent', exact: true }).count() === 0, 'K2: no create action on a detail');
				const tabs = await page.getByRole('tablist').boundingBox();
				expect(tabs && tabs.y + tabs.height <= 240, 'K2: agent tabs finish by y=240');
				const strip = page.getByRole('tablist');
				// Council pass 3 (F5, replacing pass 2's fixed 28px chevron slot): the
				// end fade starts right after the last whole tab, the chevron sits at
				// that edge no further than the normal tab gap, and at rest a tab is
				// either whole or hidden past the edge.
				await page.waitForTimeout(300);
				const fade = await strip.locator('..').evaluate(el => {
					const after = getComputedStyle(el, '::after');
					const box = el.getBoundingClientRect();
					const edge = el.querySelector('[role=tablist]').getBoundingClientRect().right;
					const shown = [...el.querySelectorAll('[role=tab]')].filter(t => getComputedStyle(t).opacity !== '0').map(t => ({ t, r: t.getBoundingClientRect() }));
					const cut = shown.filter(({ r }) => r.left < edge - 1 && r.right > edge + 1).map(({ t }) => t.textContent.trim());
					const lastWhole = Math.max(...shown.filter(({ r }) => r.right <= edge + 1).map(({ r }) => r.right));
					const chevron = el.querySelector('.tabs-more svg')?.getBoundingClientRect();
					return {
						fadeStart: Math.round(box.left + parseFloat(after.left)), lastWhole: Math.round(lastWhole),
						chevronGap: chevron ? Math.round(chevron.left - lastWhole) : null,
						tabGap: parseFloat(getComputedStyle(el.querySelector('[role=tablist]')).columnGap), cut
					};
				});
				expect(Math.abs(fade.fadeStart - fade.lastWhole) <= 1 && fade.chevronGap !== null && fade.chevronGap >= 0 && fade.chevronGap <= fade.tabGap && fade.cut.length === 0,
					`K4: fade from the last whole tab, chevron within one tab gap of it, no tab cut at rest (${JSON.stringify(fade)})`);
				const folder = page.locator('.st-row').filter({ has: page.getByRole('combobox', { name: 'Folder', exact: true }) });
				const selector = await folder.getByRole('combobox').boundingBox();
				const hint = await folder.locator('.st-rh').boundingBox();
				expect(selector && hint && selector.y >= hint.y + hint.height, 'K5: Folder selector sits below the entire helper');
				if (cycle === 1) await back.click();
				else await page.goBack();
				await page.locator('[data-view-shell="Agents"][data-pane="panel"]').waitFor({ timeout: 5000 });
				expect(await page.getByPlaceholder('Search agents').isVisible(), 'native Back returns to the list');
			}
		}
	}, {
		// A fresh load's chat is written to its entry once the user steps away,
		// even while boot still loads, so a reload there keeps Back to it (#1477).
		name: 'reload-after-stepping-away-keeps-back',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
			landedOn = (await chatState(page)).session;
			await holdBootLoad(page, '/api/v1/tags', 15_000, { releaseOnSignal: true });
		},
		async act({ page, status, mark }) {
			const notes = page.locator('[data-view-shell="Notes"]');
			const releaseBoot = async () => {
				await page.waitForFunction(() => window.__journeyBootHold?.startedAt);
				await page.evaluate(() => window.__journeyBootHold.release());
			};
			await mark('a fresh load shows the chat while boot still loads');
			await reloadForHistory(page, mark, new URL('/', status.urls.ui).toString());
			await waitChat(page, { surface: 'active', session: landedOn }, { timeout: 30_000 });
			expect(await page.evaluate(() => !window.__journeyBootHold?.releasedAt), 'boot is still loading');
			await mark('step to Notes before boot settles');
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Notes', exact: true }).click();
			await notes.waitFor();
			await releaseBoot();
			await mark('reload on Notes');
			await reloadForHistory(page, mark);
			await notes.waitFor({ timeout: 30_000 });
			await releaseBoot();
			await mark('Back returns to the chat the load landed on');
			await page.goBack();
			await waitChat(page, { surface: 'active', session: landedOn }, { timeout: 15_000 });
			await mark('Forward returns to Notes');
			await page.goForward();
			await notes.waitFor({ timeout: 5000 });
		}
	}, ...(mobileShell ? [{
		// A chat picked in the drawer waits for the drawer's own entry to be
		// popped before it pushes its own. The drawer's marker must never be
		// copied onto a chat entry, or Back would land on a chat that still
		// claims the drawer.
		name: 'native-drawer-chat-switch-back',
		viewports: ['phone'],
		async setup({ page, status }) {
			await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
			expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
			await menu(page).waitFor();
		},
		async act({ page, mark }) {
			const open = (key) => waitOpen(page, SEEDED[key]);
			const pickInDrawer = async (key) => {
				await menu(page).click();
				const sessions = page.getByRole('complementary', { name: 'Chats' });
				const row = sidebarRow(sessions, SEEDED[key]);
				await row.scrollIntoViewIfNeeded();
				await row.click();
				await open(key);
				await drawerClosed(page);
			};
			await mark('switch chats through the drawer three times');
			await pickInDrawer('no_project');
			await pickInDrawer('worktree');
			await pickInDrawer('other_project');
			expect(!(await entryState(page)).includes('mobileDrawer'), 'the chat entry carries no drawer marker');

			await mark('reopen the drawer: Back closes it and keeps the chat');
			await menu(page).click();
			await page.waitForFunction(() => String(history.state?.['sveltekit:metadata']?.state ?? '').includes('mobileDrawer'));
			await page.goBack();
			await drawerClosed(page);
			await open('other_project');

			await mark('Back opens the chat under it with the drawer closed, twice');
			await page.goBack();
			await open('worktree');
			expect(!(await entryState(page)).includes('mobileDrawer'), 'the entry under the chat carries no drawer marker');
			await drawerClosed(page);
			await page.goBack();
			await open('no_project');
			await drawerClosed(page);

			await mark('Forward returns to the later chat');
			await page.goForward();
			await open('worktree');
			await drawerClosed(page);
		}
	}, {
		// Shell history is the web UI's (#1477); the phone app keeps main's history.
		// Every expectation below is what main's /mobile/ build does, step for step,
		// so a change in the phone's Back or Forward fails here (main-vs-branch parity).
		name: 'native-history-matches-main-drawer-notes',
		viewports: ['phone'],
		async setup({ page, status }) { await openNative(page, status); },
		async act({ page, mark }) {
			await mark('chat A, then B, from the drawer'); await pickInDrawer(page, 'no_project'); await pickInDrawer(page, 'worktree');
			const length = await page.evaluate(() => history.length);
			await mark('Notes from the drawer'); await drawerTo(page, 'Notes'); await notesShown(page);
			expect(await page.evaluate(() => history.length) === length + 1, 'Notes adds one entry');
			await mark('Back lands on A, as on main'); await page.goBack(); await chatShown(page, 'no_project');
			await mark('Forward reopens B, as on main'); await page.goForward(); await chatShown(page, 'worktree');
			await mark('Back again lands on A'); await page.goBack(); await chatShown(page, 'no_project');
			expect(await page.evaluate(() => history.length) === length + 1, 'Back and Forward add no entry');
		}
	}, {
		name: 'native-history-matches-main-settings-detail',
		viewports: ['phone'],
		async setup({ page, status }) { await openNative(page, status); },
		async act({ page, mark }) {
			await mark('chat A, then B, from the drawer'); await pickInDrawer(page, 'no_project'); await pickInDrawer(page, 'worktree');
			await mark('Settings from the drawer'); await drawerTo(page, 'Settings'); await settingsShown(page, 'index');
			await mark('Providers, then the Claude detail');
			await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Providers', exact: true }).click();
			await settingsShown(page, 'page');
			await page.locator('[data-settings-item="provider:claude"]').click(); await settingsShown(page, 'detail');
			const length = await page.evaluate(() => history.length);
			await mark('Back: detail, page, index, then chat A'); await page.goBack(); await settingsShown(page, 'page');
			await page.goBack(); await settingsShown(page, 'index');
			await page.goBack(); await chatShown(page, 'no_project');
			await mark('Forward reopens B, as on main'); await page.goForward(); await chatShown(page, 'worktree');
			expect(await page.evaluate(() => history.length) === length, 'Back and Forward add no entry');
		}
	}] : [])]
});

async function openNative(page, status) {
	await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
	expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
	await menu(page).waitFor();
}
async function pickInDrawer(page, key) {
	await menu(page).click();
	const row = sidebarRow(page.getByRole('complementary', { name: 'Chats' }), SEEDED[key]);
	await row.scrollIntoViewIfNeeded();
	await row.click();
	await chatShown(page, key);
}
async function drawerTo(page, label) {
	await menu(page).click();
	const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
	const direct = nav.getByRole('button', { name: label, exact: true });
	if (await direct.count()) await direct.click();
	else { await nav.getByRole('button', { name: 'More', exact: true }).click(); await page.getByRole('menuitem', { name: label, exact: true }).click(); }
}
/** One seeded chat on screen, with the drawer closed. */
async function chatShown(page, key) {
	await waitOpen(page, SEEDED[key]);
	await drawerClosed(page);
	const others = Object.values(SEEDED).filter((title) => title !== SEEDED[key]);
	const bar = await page.locator('.topbar').textContent();
	expect(!others.some((title) => bar.includes(title)), `only ${SEEDED[key]} is open`);
}
async function notesShown(page) {
	await page.getByPlaceholder('Search notes', { exact: true }).waitFor();
	await drawerClosed(page);
}
async function settingsShown(page, view) {
	await page.locator(`div.settings-shell[data-view="${view}"]`).waitFor({ timeout: 10_000 });
}

function menu(page) {
	return page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ });
}
function entryState(page) {
	return page.evaluate(() => String(history.state?.['sveltekit:metadata']?.state ?? ''));
}
async function drawerClosed(page) {
	await page.waitForFunction(() => document.querySelector('button[aria-label^="Open menu"]')?.getAttribute('aria-expanded') === 'false', null, { timeout: 5000 });
	// A marker copied onto the entry would reopen nothing, but hold the drawer's history step: give it a moment to show.
	await page.waitForTimeout(300);
	expect(await menu(page).getAttribute('aria-expanded') === 'false', 'the drawer is closed');
}
/** The open chat, by the title the phone's one bar shows. */
async function waitOpen(page, title) {
	await page.waitForFunction((want) => document.querySelector('.topbar')?.textContent?.includes(want), title, { timeout: 15_000 });
}
