// The rail's notification history floats beside the rail (council pass 4: a
// 300px popover over the list column, bottom-aligned with the bell; where it
// still lands on page content, the modal scrim dims the page so no sliced words read),
// and the empty-chat composer does not hold an empty Stop seat at its right edge.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, holdBootLoad, openNewChat, seededChatId, waitChat, SEEDED, settleServiceWorker } from './lib/app.mjs';
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

// With the list panel open the 300px popover still overruns it by ~45px. Over a
// new chat at 1440 that strip is blank page (the composer is centred), so the
// page stays lit; in a chat at 1000 and 800 the transcript and the composer's
// "+" start under it, so the dialog scrim dims the page (G9). (A chat at 1440
// is dimmed too: its turn navigator's ticks sit in that strip.)
await runJourney({ name: 'rail-popover-composer-inset', allow: DEV_INSTANCE_NOISE, cases: [1440, 1000, 800].map(width => ({
	name: `notifications-beside-rail-${width}`,
	viewports: ['desktop'],
	async setup({ page, status }) {
		await page.setViewportSize({ width, height: 900 });
		// The bell is already interactive while bootstrap awaits the agent list.
		// Hold that read until the panel opens so late reconciliation always
		// overlaps it, rather than depending on a loaded CI runner to hit the race.
		await holdBootLoad(page, '/api/v1/agents', 30_000, { releaseOnSignal: true });
		await gotoApp(page, status, width === 1440 ? '/' : `/?session=${seededChatId(status, 'long')}`);
		const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
		if (!await page.getByRole('complementary', { name: 'Chats', exact: true }).locator('.panel-plane').isVisible()) {
			await page.getByRole('button', { name: 'Show sidebar', exact: true }).click();
		}
		await nav.waitFor();
	},
	async act({ page, mark, log }) {
		await mark('open the rail bell');
		const panel = await openRailBell(page);
		await mark('bootstrap finishes with the notification panel open');
		await page.evaluate(() => window.__journeyBootHold.release());
		await page.waitForFunction(() => performance.getEntriesByName('shuffle:boot.reconciled').length > 0);
		expect(await panel.isVisible(), 'initial landing reconciliation keeps the notification panel open');
		// Measure where it settles, not partway through its 120ms entrance.
		await panel.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
		const geometry = await panel.evaluate(el => {
			const rail = document.querySelector('aside.sidebar nav.rail').getBoundingClientRect();
			const bell = document.querySelector('aside.sidebar .notif-center.rail .notif-btn').getBoundingClientRect();
			const r = el.getBoundingClientRect();
			const scrimEl = document.querySelector('[data-testid="notif-scrim"]');
			const probe = document.createElement('i');
			probe.style.background = 'var(--modal-overlay)';
			document.body.append(probe);
			const modalOverlay = getComputedStyle(probe).backgroundColor;
			probe.remove();
			const list = document.querySelector('aside.sidebar').getBoundingClientRect();
			const composer = document.querySelector('.chat-island').getBoundingClientRect();
			return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, railRight: rail.right, bellBottom: bell.bottom, innerWidth,
				listRight: list.right, composerLeft: composer.left, scrim: !!scrimEl, scrimBg: scrimEl && getComputedStyle(scrimEl).backgroundColor, modalOverlay };
		});
		log(JSON.stringify({ width, ...geometry }));
		expect(geometry.left >= geometry.railRight && geometry.left <= geometry.railRight + 12, 'the history panel starts just right of the rail, leaving the rail uncovered');
		expect(Math.abs(geometry.width - 300) <= 1, `the history panel is 300px wide (${geometry.width}px)`);
		expect(Math.abs(geometry.bottom - geometry.bellBottom) <= 2, 'the history panel is bottom-aligned with the bell');
		expect(geometry.top >= 0 && geometry.bottom <= 900, 'the history panel stays in the window');
		if (geometry.right <= geometry.composerLeft) {
			expect(width === 1440, `only the wide window keeps the chat clear of the popover (${JSON.stringify(geometry)})`);
			expect(!geometry.scrim, `with the list panel open the popover ends before the new chat's content, so the page is not dimmed (${JSON.stringify(geometry)})`);
			await mark('Escape closes it');
			await page.keyboard.press('Escape');
			await panel.waitFor({ state: 'hidden' });
		} else {
			expect(width !== 1440, `the popover covers the chat at ${width}px: ${JSON.stringify(geometry)}`);
			expect(geometry.right > geometry.listRight, 'the popover overruns the open list panel onto the chat');
			expect(geometry.scrim, `G9: a popover over the transcript and composer dims the page (${JSON.stringify(geometry)})`);
			expect(geometry.scrimBg === geometry.modalOverlay, `G3: the scrim is the modal overlay at full strength (${geometry.scrimBg} vs ${geometry.modalOverlay})`);
			await mark('a click on the scrim closes it');
			await page.mouse.click(width - 40, 450);
			await panel.waitFor({ state: 'hidden' });
		}
	}
})).concat([{
	// The narrowest Sidebar no longer narrows the 300px panel; an error row's
	// action buttons must still not squeeze its message to nothing.
	name: 'notifications-min-sidebar-actionable-row',
	viewports: ['desktop'],
	async setup({ page, status }) {
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.addInitScript(() => localStorage.setItem('sidebar-width', '280'));
		// The built app's SW forwards API GETs from the worker. page.route can
		// miss those requests once it claims the page; intercept at the caller,
		// leaving the real SW and every other read/write enabled.
		await page.addInitScript(() => {
			const original = window.fetch.bind(window);
			window.__journeyNotificationReads = 0;
			window.fetch = async (input, init) => {
				const method = (init?.method || (input instanceof Request && input.method) || 'GET').toUpperCase();
				const url = new URL(input instanceof Request ? input.url : String(input), location.href);
				if (method !== 'GET' || url.origin !== location.origin || url.pathname !== '/api/v1/notifications') return original(input, init);
				window.__journeyNotificationReads++;
				return Response.json({ notifications: [{
					id: 'journey-api-error', severity: 'error', surface: 'notification', category: 'api_error',
					title: 'Request failed', message: 'The provider returned an error while starting this chat. Try again or send a report.',
					read: false, dismissed: false, created_at: Date.now(), session_id: null,
					action: { kind: 'api_error', payload: { status_code: 500 } }
				}] });
			};
		});
		await gotoApp(page, status);
		// Exercise both the initial visit and a controlled document, so a mock
		// that only wins the SW-installation race cannot pass this case.
		await settleServiceWorker(page, status);
		await page.getByRole('navigation', { name: 'Workspace', exact: true }).waitFor();
		expect(await page.evaluate(() => window.__journeyNotificationReads > 0), 'the page consumed the notification fixture');
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
		expect(row.actions === 2, 'the API error row rebuilds both history actions');
		expect(row.actionsBelow, 'in the rail popover the row actions sit under the message');
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
