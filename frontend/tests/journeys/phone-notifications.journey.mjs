// Render the relay status in the real Settings page at desktop/phone sizes.
// The unlinked case uses the real worker status. The linked case supplies only
// an HTTP status fixture; it does not claim account sign-in or phone delivery.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, seededChatId, showSidebar, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

async function openGeneral({ page, status }) {
	await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`);
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
	const sessions = await showSidebar(page);
	await sessions.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Settings', exact: true }).click();
	const general = page.getByRole('button', { name: 'General', exact: true });
	await general.waitFor();
	await general.click();
	await page.getByRole('heading', { name: 'General', exact: true }).waitFor();
	await page.locator('[data-setting-anchor="general.notifications"]').filter({ hasText: /Phone notifications:|Sign in to your HobbyCoders/ }).waitFor();
	await page.locator('[data-setting-anchor="general.notifications"]').scrollIntoViewIfNeeded();
}

async function checkRow(page, text) {
	const row = page.locator('[data-setting-anchor="general.notifications"]');
	await row.getByText(text, { exact: true }).waitFor();
	await row.scrollIntoViewIfNeeded();
	expect(await row.locator('input, textarea').count() === 0, 'no sender-key field');
	const groups = await page.locator('.st-grp h2').allTextContents();
	expect(groups.at(-1)?.trim() === 'Notifications', `notifications follow the existing groups: ${groups}`);
	const bounds = await row.boundingBox();
	const viewport = page.viewportSize();
	expect(bounds.x >= 0 && bounds.x + bounds.width <= viewport.width, 'notification row fits the viewport');
}

await runJourney({
	name: 'phone-notifications',
	cases: [
		{
			name: 'not-linked',
			setup: openGeneral,
			async act({ page, mark }) {
				await mark('unlinked worker: no key setup, existing sign-in entry');
				await checkRow(page, 'Sign in to your HobbyCoders account to get phone notifications');
				await page.locator('[data-setting-anchor="general.notifications"]').getByRole('button', { name: 'Sign in', exact: true }).click();
				await page.getByRole('heading', { name: 'Shuffle account', exact: true }).waitFor();
				await page.getByRole('region', { name: 'Sign in', exact: true }).waitFor();
			},
			allow: [{
				kind: 'failed_requests', status: 403, url: /\/mobile\/hub\/devices\/confirmations$/,
				reason: 'preview cookies cannot act as worker approvers (mobile_hub.require_worker_approver); account navigation still works'
			}]
		},
		{
			name: 'linked-status-fixture',
			async setup(ctx) {
				await ctx.page.route('**/api/v1/settings/notifications', route => route.fulfill({
					json: { linked: true, devices: 2, enabled: true }
				}));
				await openGeneral(ctx);
			},
			async act({ page, mark }) {
				await mark('linked status fixture: phone notification count without a key editor');
				await checkRow(page, 'Phone notifications: on · 2 devices');
				expect(await page.locator('[data-setting-anchor="general.notifications"]').getByRole('button', { name: 'Sign in', exact: true }).count() === 0, 'no sign-in action on a linked worker');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
