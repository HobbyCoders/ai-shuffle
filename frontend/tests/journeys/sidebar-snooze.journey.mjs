// Journey: snooze a chat from its sidebar row and bring it back.
//
// A snoozed chat leaves Active for its own Snoozed group, sorted by wake time,
// with the wake time where the row's age was ("until Fri 9:00 AM"). Unsnooze
// moves it straight back to Active. Guards: the row lands in the wrong group
// (Settled, or stays in Active), the group or its label is missing, or the row
// jumps between groups while the change is in flight.
//
// The chat is the case's own (created through the API, deleted by the runner
// afterwards), so no seeded chat is touched. A short wake (the server's loop
// and the page's local re-group at the due time) is covered by unit tests and
// checked by hand: waiting a minute per variant is too slow for the suite.
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, showSidebar, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CARD = (id) => `[data-session-card="${id}"]`;
const laneOf = (page, id) => page.evaluate((selector) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') ?? null, CARD(id));

async function waitLane(page, id, lane, timeout = 30_000) {
	const sessions = await showSidebar(page);
	if (lane !== 'active') {
		const section = sessions.getByRole('button', { name: new RegExp(`^${lane === 'snoozed' ? 'Snoozed' : 'Settled'}`) }).first();
		await section.waitFor({ timeout });
		if ((await section.getAttribute('aria-expanded')) === 'false') await section.click();
	}
	try {
		await page.waitForFunction(({ selector, lane }) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') === lane,
			{ selector: CARD(id), lane }, { timeout });
	} catch {
		throw new Error(`the row never reached the ${lane} group; it is in ${await laneOf(page, id)}`);
	}
}

/** What the server holds once the page's change has landed (the row moves first, optimistically). */
async function waitSaved(page, id, want, what) {
	let row = null;
	for (let attempt = 0; attempt < 40; attempt++) {
		row = await api(page, 'GET', `/sessions/${id}`);
		if (want(row)) return;
		await page.waitForTimeout(250);
	}
	throw new Error(`${what}; the server holds ${JSON.stringify({ is_active: row?.is_active, snoozed_until: row?.snoozed_until })}`);
}

/** The row's hover action, as sidebar-truth presses it (the buttons show on hover; a phone reaches them the same way). */
async function rowAction(page, id, name) {
	await showSidebar(page);
	await page.locator(CARD(id)).getByRole('button', { name, exact: true }).dispatchEvent('click');
}

await runJourney({
	name: 'sidebar-snooze',
	viewports: ['desktop', 'phone'],
	recorder: { selectors: ['snoozed=[data-group="snoozed"] [data-session-card]'] },
	cases: [
		{
			name: 'snooze-then-unsnooze',
			async setup({ page, status, log }) {
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				const { session_id: id } = await api(page, 'POST', '/sessions', {});
				await showSidebar(page); // a phone's drawer lists the rows only while open
				await page.locator(CARD(id)).waitFor({ state: 'attached', timeout: 30_000 });
				page.__snoozeChat = id;
				log(`chat ${id}`);
			},
			async act({ page, mark }) {
				const id = page.__snoozeChat;
				await waitLane(page, id, 'active');

				await mark('snooze with a quick choice');
				await rowAction(page, id, 'Snooze session');
				const picker = page.getByRole('dialog', { name: 'Snooze chat' });
				await picker.waitFor({ timeout: 10_000 });
				await picker.getByRole('heading', { name: 'Snooze until' }).waitFor();
				const choices = picker.getByRole('group', { name: 'Quick choices' }).getByRole('button');
				expect((await choices.count()) >= 1, 'the picker offers at least one quick choice');
				const picked = await choices.first().getAttribute('aria-label');
				await choices.first().click();
				await picker.waitFor({ state: 'hidden', timeout: 10_000 });
				await waitLane(page, id, 'snoozed');
				const label = (await page.locator(`${CARD(id)} .snooze-until`).innerText()).trim();
				expect(/^until /.test(label), `the row shows its wake time, got ${JSON.stringify(label)} (picked ${picked})`);
				expect((await page.locator(`[data-group="settled"] ${CARD(id)}`).count()) === 0, 'a snoozed chat is not listed under Settled');
				await waitSaved(page, id, (row) => !row.is_active && !!row.snoozed_until, 'the snooze never reached the server');

				await mark('unsnooze');
				await rowAction(page, id, 'Unsnooze session and move it to Active');
				await waitLane(page, id, 'active');
				expect((await page.locator(`${CARD(id)} .snooze-until`).count()) === 0, 'an active row shows no wake time');
				await waitSaved(page, id, (row) => row.is_active && !row.snoozed_until, 'unsnooze never cleared the wake time');
				// Hold: a late push or reload must not put it back in Snoozed.
				await page.waitForTimeout(1500);
				await waitLane(page, id, 'active', 1000);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
