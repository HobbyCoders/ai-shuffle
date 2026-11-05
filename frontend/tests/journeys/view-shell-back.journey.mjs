// Selected desktop details must gain a usable Back step when resized into a stack.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
	name: 'view-shell-back',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
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
		name: 'native-detail-browser-back',
		viewports: ['phone'],
		async setup({ page, status }) {
			await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
			await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
			await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Agents', exact: true }).click();
			await page.getByPlaceholder('Search agents').waitFor();
		},
		async act({ page, mark }) {
			for (let cycle = 1; cycle <= 2; cycle++) {
				await mark(`open a native detail then return with browser Back (cycle ${cycle})`);
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
