// The rail's notification history opens inside the Sidebar, and the empty-chat
// composer does not hold an empty Stop seat at its right edge.
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

await runJourney({ name: 'rail-popover-composer-inset', allow: DEV_INSTANCE_NOISE, cases: [1440, 1000].map(width => ({
	name: `notifications-inside-sidebar-${width}`,
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
		await page.getByRole('button', { name: /^Notifications( \(\d+ unread\))?$/ }).first().click();
		const panel = page.getByRole('dialog', { name: 'Notifications', exact: true });
		await panel.waitFor();
		const geometry = await panel.evaluate(el => {
			const aside = document.querySelector('aside.sidebar').getBoundingClientRect();
			const rail = document.querySelector('aside.sidebar .panel-plane').getBoundingClientRect();
			const r = el.getBoundingClientRect();
			return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, asideLeft: aside.left, asideRight: aside.right, listLeft: rail.left, innerWidth };
		});
		log(JSON.stringify({ width, ...geometry }));
		expect(geometry.right <= geometry.asideRight, 'the history panel ends inside the Sidebar, not over the chat');
		expect(geometry.left >= geometry.listLeft, 'the history panel leaves the rail itself uncovered');
		expect(geometry.top >= 0 && geometry.bottom <= 900, 'the history panel stays in the window');
		await mark('Escape closes it');
		await page.keyboard.press('Escape');
		await panel.waitFor({ state: 'hidden' });
	}
})).concat([{
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
