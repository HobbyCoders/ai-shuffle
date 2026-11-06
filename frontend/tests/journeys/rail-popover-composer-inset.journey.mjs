// The rail's notification history floats beside the rail (council pass 4: a
// 300px popover over the list column, bottom-aligned with the bell, that ends
// before the page content while the list panel is open, so it slices no words),
// and the empty-chat composer does not hold an empty Stop seat at its right edge.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, openNewChat, seededChatId, waitChat, SEEDED } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

// Distance from the composer box's inner edges to its first and last controls.
const composerInsets = page => page.evaluate(() => {
	const cluster = [...document.querySelectorAll('.send-cluster')].find(el => el.getClientRects().length);
	let box = cluster?.parentElement;
	while (box && getComputedStyle(box).borderRightWidth === '0px') box = box.parentElement;
	const attach = box?.querySelector('.composer-attach');
	if (!cluster || !box || !attach) return null;
	const shown = [...cluster.querySelectorAll('button')].filter(b => b.getClientRects().length);
	const last = Math.max(...shown.map(b => b.getBoundingClientRect().right));
	const outer = box.getBoundingClientRect();
	return {
		left: Math.round(attach.getBoundingClientRect().left - outer.left),
		right: Math.round(outer.right - last),
		stopSeats: cluster.querySelectorAll('.stop-slot').length
	};
});

// The rail mounts before the shell finishes loading preferences, so a click in
// the first moments can land before the bell is live; click until it opens.
async function openRailBell(page) {
	const bell = page.getByRole('button', { name: /^Notifications( \(\d+ unread\))?$/ }).first();
	const panel = page.getByRole('dialog', { name: 'Notifications', exact: true });
	for (let i = 0; i < 5; i++) {
		await bell.click();
		try { await panel.waitFor({ timeout: 3000 }); return panel; } catch {}
	}
	await panel.waitFor({ timeout: 3000 });
	return panel;
}

await runJourney({ name: 'rail-popover-composer-inset', allow: DEV_INSTANCE_NOISE, cases: [1440, 1000].map(width => ({
	name: `notifications-beside-rail-${width}`,
	viewports: ['desktop'],
	async setup({ page, status }) {
		await page.setViewportSize({ width, height: 900 });
		await gotoApp(page, status);
		const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
		if (!await page.getByRole('complementary', { name: 'Chats', exact: true }).locator('.panel-plane').isVisible()) {
			await page.getByRole('button', { name: 'Show sidebar', exact: true }).click();
		}
		await nav.waitFor();
	},
	async act({ page, mark, log }) {
		await mark('open the rail bell');
		const panel = await openRailBell(page);
		// Measure where it settles, not partway through its 120ms entrance.
		await panel.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
		const geometry = await panel.evaluate(el => {
			const rail = document.querySelector('aside.sidebar nav.rail').getBoundingClientRect();
			const bell = document.querySelector('aside.sidebar .notif-center.rail .notif-btn').getBoundingClientRect();
			const r = el.getBoundingClientRect();
			const scrim = !!document.querySelector('[data-testid="notif-scrim"]');
			return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, railRight: rail.right, bellBottom: bell.bottom, innerWidth, scrim };
		});
		log(JSON.stringify({ width, ...geometry }));
		expect(geometry.left >= geometry.railRight && geometry.left <= geometry.railRight + 12, 'the history panel starts just right of the rail, leaving the rail uncovered');
		expect(Math.abs(geometry.width - 300) <= 1, `the history panel is 300px wide (${geometry.width}px)`);
		expect(!geometry.scrim, 'with the list panel open the popover ends before the content, so the page is not dimmed');
		expect(Math.abs(geometry.bottom - geometry.bellBottom) <= 2, 'the history panel is bottom-aligned with the bell');
		expect(geometry.top >= 0 && geometry.bottom <= 900, 'the history panel stays in the window');
		await mark('Escape closes it');
		await page.keyboard.press('Escape');
		await panel.waitFor({ state: 'hidden' });
	}
})).concat([{
	// The narrowest Sidebar no longer narrows the 300px panel; an error row's
	// action buttons must still not squeeze its message to nothing.
	name: 'notifications-min-sidebar-actionable-row',
	viewports: ['desktop'],
	async setup({ page, status }) {
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.addInitScript(() => localStorage.setItem('sidebar-width', '280'));
		await page.route(/\/api\/v1\/notifications(?:\?|$)/, (route) => route.request().method() !== 'GET' ? route.fallback() : route.fulfill({
			contentType: 'application/json',
			body: JSON.stringify({ notifications: [{
				id: 'journey-api-error', severity: 'error', surface: 'notification', category: 'api_error',
				title: 'Request failed', message: 'The provider returned an error while starting this chat. Try again or send a report.',
				read: false, dismissed: false, created_at: Date.now(), session_id: null,
				action: { kind: 'api_error', payload: { status_code: 500 } }
			}] })
		}));
		await gotoApp(page, status);
		await page.getByRole('navigation', { name: 'Workspace', exact: true }).waitFor();
	},
	async act({ page, mark, log }) {
		await mark('open the rail bell at the narrowest Sidebar');
		const panel = await openRailBell(page);
		await panel.getByText('Request failed', { exact: true }).waitFor();
		const row = await panel.evaluate(el => {
			const railRight = document.querySelector('aside.sidebar nav.rail').getBoundingClientRect().right;
			const item = el.querySelector('.notif-item');
			const message = item.querySelector('.item-message').getBoundingClientRect();
			const actions = [...item.querySelectorAll('.item-action')].map(b => b.getBoundingClientRect());
			return {
				panel: Math.round(el.getBoundingClientRect().width), panelLeft: el.getBoundingClientRect().left, railRight,
				message: Math.round(message.width), actions: actions.length,
				actionsBelow: actions.every(a => a.top >= message.bottom - 1)
			};
		});
		log(JSON.stringify(row));
		expect(row.panelLeft >= row.railRight, 'the panel leaves the rail uncovered');
		// Only the icon, the dismiss button, gaps and padding (about 80px) share the row with the message.
		expect(row.message >= row.panel - 90, `the message keeps the row's width (${row.message}px of ${row.panel}px)`);
		if (row.actions) expect(row.actionsBelow, 'in the rail popover the row actions sit under the message');
	}
}, {
	name: 'empty-chat-composer-has-no-stop-seat',
	viewports: ['desktop'],
	async setup(ctx) {
		await openNewChat(ctx);
	},
	async act({ page, mark, log, status }) {
		await mark('empty chat: circles sit flush with the box edge');
		const hero = await composerInsets(page);
		log(JSON.stringify({ hero }));
		expect(hero, 'the composer box and its controls are measurable');
		expect(hero.stopSeats === 0, 'no empty Stop seat on the empty-chat composer');
		expect(Math.abs(hero.right - hero.left) <= 6, `right inset (${hero.right}px) matches the left inset (${hero.left}px)`);
		await mark('a chat keeps the Stop seat so Stop arriving never moves Send');
		await gotoApp(page, status, `/?session=${seededChatId(status, 'long')}`);
		await waitChat(page, { surface: 'active' }, { timeout: 90_000 });
		const chat = await composerInsets(page);
		log(JSON.stringify({ chat, seeded: SEEDED.long }));
		expect(chat?.stopSeats === 1, 'the chat composer still reserves the Stop seat');
	}
}]) });
