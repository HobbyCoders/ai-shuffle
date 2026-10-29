// Real Devices panel, read-only against the dev instance. No seeded machine
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
			await page.getByRole('complementary', { name: 'Sessions' })
				.or(page.getByRole('button', { name: 'Show sidebar' }))
				.filter({ visible: true }).first().waitFor();
			await showSidebar(page);
			await page.getByRole('button', { name: 'Settings', exact: true }).click();
			await page.getByRole('button', { name: 'Devices', exact: true }).click();
			const worker = await page.request.get(new URL('/api/v1/settings/worker-settings', status.urls.ui).href);
			expect(worker.ok(), 'owner can read machine settings');
			const local = (await worker.json()).workers.find((w) => w.is_self);
			expect(local?.outcome === 'ok', 'local worker has real status');
			await page.getByRole('button').filter({ hasText: local.device_name }).first().click();
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
	}]
});
