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
			await mark('leave an open note for Files');
			await page.getByRole('button', { name: /^Chats(?: \(|$)/ }).first().click();
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Files', exact: true }).click();
			await page.getByRole('button', { name: 'Workspace', exact: true }).click();
			await page.locator('[data-view-shell="Files"][data-pane="detail"]').waitFor();
			await mark('Back closes Files detail');
			await page.getByRole('button', { name: 'Back to Files', exact: true }).click();
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
			await mark('resize the selected desktop detail through inline into stacked');
			await page.setViewportSize({ width: 1000, height: 1000 });
			await page.locator('[data-view-shell="Agents"]:not([data-docked]):not([data-stacked])').waitFor();
			await page.setViewportSize({ width: 390, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-stacked][data-pane="detail"]').waitFor();
			await page.waitForTimeout(300);
			await mark('widen and re-enter the same selected detail without another selection');
			await page.setViewportSize({ width: 1440, height: 1000 });
			await page.locator('[data-view-shell="Agents"][data-docked]').waitFor();
			await page.setViewportSize({ width: 1000, height: 1000 });
			await page.locator('[data-view-shell="Agents"]:not([data-docked]):not([data-stacked])').waitFor();
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
			if (mobileShell) expect(rect.width >= 44 && rect.height >= 44 && rect.y < 56, 'native create is a 44px top-bar target, not outside size containment');
			await create.click();
			const dialog = page.getByRole('dialog', { name: 'New agent', exact: true });
			await dialog.waitFor();
			await dialog.getByRole('button', { name: 'Close', exact: true }).click();
			await page.getByPlaceholder('Search agents').waitFor();
			for (let cycle = 1; cycle <= 2; cycle++) {
				await mark(`open a ${mobileShell ? 'native' : 'responsive'} detail then return with browser Back (cycle ${cycle})`);
				await page.locator('[data-view-shell="Agents"] [data-settings-item]').first().click();
				await page.locator('[data-view-shell="Agents"][data-pane="detail"]').waitFor();
				const back = page.getByRole('button', { name: 'Back to Agents', exact: true });
				expect(await back.isVisible(), 'native detail has an on-screen Back control');
				if (cycle === 1) await back.click();
				else await page.goBack();
				await page.locator('[data-view-shell="Agents"][data-pane="panel"]').waitFor({ timeout: 5000 });
				expect(await page.getByPlaceholder('Search agents').isVisible(), 'native Back returns to the list');
			}
		}
	}]
});
