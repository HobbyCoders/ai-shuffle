/** Actual sidebar clicks (including the real phone drawer), or cached dispatch. */
import { arm, snapshot, waitUsable } from './chat-journeys.mjs';

export async function switchJourney(p, id, { sidebar = true } = {}) {
	if (sidebar) {
		if (p.platform === 'mobile') {
			const menu = p.page.getByRole('button', { name: /Open menu/i }).first();
			if (await menu.count() && await menu.getAttribute('aria-expanded') !== 'true') await menu.click();
		}
		const row = p.page.locator(`[data-session-card="${id}"] .scard`);
		await row.waitFor({ state: 'visible', timeout: 10000 });
		await arm(p, 'switch');
		await row.click();
	} else {
		await arm(p, 'return');
		await p.page.evaluate((id) => window.dispatchEvent(new CustomEvent('open-session', {
			detail: { sessionId: id }
		})), id);
	}
	await waitUsable(p);
	return snapshot(p);
}
