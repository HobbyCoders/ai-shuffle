// Selected desktop details must gain a usable Back step when resized into a stack.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');

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
			expect(!(await page.evaluate(() => history.state?.['sveltekit:states']?.viewPanelDetail)), 'no hidden Notes detail marker remains after Files Back');
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
	}]
});
