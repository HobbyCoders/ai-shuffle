// Real Settings navigation and Devices panel against the dev instance. No seeded machine
// status, credential copying, installer runs or production worker mutations.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { expect, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
	name: 'worker-machine-settings',
	viewports: ['desktop', 'fold-cover'],
	throttles: ['slow'],
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'status-and-all-worker-confirmation',
		async setup({ page, status }) {
			await page.goto(status.urls.ui);
			// A fresh context starts with no layout view. Boot reconciliation
			// writes the new-chat view after preference hydration; it may close
			// the phone drawer first. Read that real producer's completion, not
			// network-idle (the app intentionally keeps an event stream open).
			await page.waitForFunction(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView?.type === 'session');
			// Reconciliation can finish before Svelte has rendered the desktop
			// sidebar. Wait for either visible shell state before checking it.
			await page.getByRole('complementary', { name: 'Chats' })
				.or(page.getByRole('button', { name: /^(Show sidebar|Chats)\b/ }))
				.filter({ visible: true }).first().waitFor();
			await showSidebar(page);
			await page.getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('button', { name: 'Devices', exact: true }).click();
			const worker = await page.request.get(new URL('/api/v1/settings/worker-settings', status.urls.ui).href);
			expect(worker.ok(), 'owner can read machine settings');
			const local = (await worker.json()).workers.find((w) => w.is_self);
			expect(local?.outcome === 'ok', 'local worker has real status');
			// The row may read "Local" rather than a raw machine id, so find it by its slot.
			await page.locator('[data-settings-item="self"]').click();
			const panel = page.getByRole('region', { name: 'Worker machine settings', exact: true });
			await panel.getByText('Claude CLI', { exact: true }).waitFor();
			await panel.getByRole('button', { name: 'Refresh worker status' }).scrollIntoViewIfNeeded();
		},
		async act({ page, mark }) {
			const panel = page.getByRole('region', { name: 'Worker machine settings', exact: true });
			await mark('refresh machine status without replacing the panel');
			await panel.getByRole('button', { name: 'Refresh worker status' }).click();
			await mark('preview an all-worker operation, then cancel without dispatch');
			await panel.getByRole('button', { name: 'Apply…', exact: true }).click();
			await panel.getByRole('button', { name: 'Apply to all workers', exact: true }).waitFor();
			await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
			expect(await panel.getByRole('button', { name: 'Apply to all workers', exact: true }).count() === 0, 'cancel clears the confirmation');
		},
		async verify({ page, variant }) {
			const panel = page.getByRole('region', { name: 'Worker machine settings', exact: true });
			const sizes = await panel.evaluate((el) => ({ width: el.clientWidth, scroll: el.scrollWidth, viewport: innerWidth, page: document.documentElement.scrollWidth }));
			expect(sizes.scroll <= sizes.width && sizes.page <= sizes.viewport, 'panel fits without horizontal scrolling');
			expect(await panel.getByRole('button', { name: /Sign in|Sign out/i }).count() === 0, 'sign-in is status-only');
			if (process.env.JOURNEY_SCREENSHOTS) {
				mkdirSync(process.env.JOURNEY_SCREENSHOTS, { recursive: true });
				await panel.getByText('Machine settings', { exact: true }).scrollIntoViewIfNeeded();
				await page.screenshot({ path: join(process.env.JOURNEY_SCREENSHOTS, `worker-settings-${variant.viewport}.png`) });
			}
		}
	}, {
		name: 'claude-deep-link-and-back',
		viewports: ['desktop', 'phone'],
		async setup({ page, status }) {
			await page.goto(status.urls.ui);
			await page.locator('[data-ui-state="chat"]').waitFor();
			await page.getByRole('button', { name: /^Model: / }).first().waitFor();
			await showSidebar(page);
			await page.getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('combobox', { name: 'Search settings' }).waitFor();
			// Load the real collection first; this case judges history, not lazy-page layout.
			await page.getByRole('button', { name: 'Providers', exact: true }).click();
			await page.locator('[data-settings-item="provider:claude"]').click();
			await page.getByText('Keep Claude Code installed', { exact: true }).waitFor();
			if (page.viewportSize().width <= 600) {
				await page.goBack();
				await page.waitForFunction(() => document.querySelector('div.settings-shell[data-view]')?.getAttribute('data-view') === 'page');
				await page.goBack();
				await page.waitForFunction(() => document.querySelector('div.settings-shell[data-view]')?.getAttribute('data-view') === 'index');
			}
			// Keep Settings mounted off screen: a cold lazy import can hide the race.
			await showSidebar(page);
			const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
			await nav.getByRole('button', { name: 'More', exact: true }).click();
			await page.getByRole('menuitem', { name: 'Automations', exact: true }).click();
			await page.getByPlaceholder('Search automations').waitFor();
			await page.getByRole('button', { name: 'New automation', exact: true }).first().click();
			await page.getByRole('button', { name: 'Open Claude settings', exact: true }).waitFor();
		},
		async act({ page, mark, variant }) {
			await mark('follow the Claude mode link from the automation detail');
			await page.getByRole('button', { name: 'Open Claude settings', exact: true }).click();
			const target = page.locator('[data-setting-anchor="providers.claudeMode"]');
			await target.waitFor({ state: 'attached' });
			// The outgoing ViewShell pop is asynchronous; it must not undo the target.
			await page.waitForTimeout(500);
			expect(await target.isVisible(), 'Claude mode stays visible after the outgoing history entry retires');
			if (variant.viewport === 'desktop') return;
			await mark('Back restores the Providers category');
			await page.goBack();
			await page.waitForFunction(() => document.querySelector('div.settings-shell[data-view]')?.getAttribute('data-view') === 'page');
			expect(await page.getByRole('heading', { name: 'Providers', exact: true }).isVisible(), 'Back shows the category');
			await mark('Back restores the Settings index');
			await page.goBack();
			await page.waitForFunction(() => document.querySelector('div.settings-shell[data-view]')?.getAttribute('data-view') === 'index');
			expect(await page.getByRole('combobox', { name: 'Search settings' }).isVisible(), 'Back shows the index');
		},
		async verify({ page, variant }) {
			if (variant.viewport !== 'phone') return;
			// Leaving the document is checked after the in-app recording stops.
			await page.goBack();
			await page.waitForURL((url) => url.href === 'about:blank');
		}
	}]
});
