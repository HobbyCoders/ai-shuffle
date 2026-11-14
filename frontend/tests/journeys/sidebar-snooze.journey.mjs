// Journey: snooze a chat from its sidebar row and bring it back.
//
// A snoozed chat leaves Active for its own Snoozed group, sorted by wake time,
// with the wake time where the row's age was ("until Fri 9:00 AM"). Unsnooze
// moves it straight back to Active. Guards: the row lands in the wrong group
// (Settled, or stays in Active), the group or its label is missing, or the row
// jumps between groups while the change is in flight.
//
// On a phone a finger swipes a row right to snooze it. A snooze needs a time,
// so the swipe only opens the same picker: the row springs back, and moves to
// Snoozed once a time is picked. Dismissing the picker changes nothing. A
// right swipe on a snoozed row picks a new wake time; a left swipe keeps its
// old meaning (Move to Active). Guards: the swipe stays inert, the drag lane
// is not the snooze lane, the row slides away before a time is picked, or a
// dismissed picker still snoozes it.
//
// The chat is the case's own (created through the API, deleted by the runner
// afterwards), so no seeded chat is touched. A short wake (the server's loop
// and the page's local re-group at the due time) is covered by unit tests and
// checked by hand: waiting a minute per variant is too slow for the suite.
// `--mobile-shell` runs the swipe cases in the built `/mobile/` app.
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, showSidebar, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const CARD = (id) => `[data-session-card="${id}"]`;
const laneOf = (page, id) => page.evaluate((selector) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') ?? null, CARD(id));

/** The chat list: the sidebar, or the drawer the native phone shell opens from its menu button. */
async function showChats(page) {
	if (!page.__mobileShell) return showSidebar(page);
	const sessions = page.getByRole('complementary', { name: 'Chats' });
	if (await sessions.isVisible()) return sessions;
	await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
	await sessions.waitFor({ timeout: 10_000 });
	return sessions;
}

async function waitLane(page, id, lane, timeout = 30_000) {
	const sessions = await showChats(page);
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

/**
 * A finger dragging across the row, 60% of its width (past the 40% commit
 * point), paced like a real swipe and held before lifting so it commits on
 * distance, not as a flick. `beforeLift` looks at the row mid-drag.
 */
async function swipeRow(page, id, direction, beforeLift) {
	await showChats(page);
	const row = page.locator(CARD(id));
	await row.scrollIntoViewIfNeeded();
	const box = await row.boundingBox();
	const y = box.y + box.height / 2;
	// Start clear of the screen edges, where the system takes back swipes.
	const from = direction > 0 ? box.x + 40 : box.x + box.width - 40;
	const travel = box.width * 0.6 * direction;
	const cdp = await page.context().newCDPSession(page);
	const touch = (type, touchPoints) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints });
	try {
		await touch('touchStart', [{ x: from, y }]);
		for (let step = 1; step <= 10; step++) {
			await touch('touchMove', [{ x: from + (travel * step) / 10, y }]);
			await page.waitForTimeout(16);
		}
		await page.waitForTimeout(150);
		if (beforeLift) await beforeLift(row);
		await touch('touchEnd', []);
	} finally {
		await cdp.detach();
	}
}

/** Mid-drag, the lane behind a right-swiped row is the armed snooze lane. */
async function expectSnoozeLane(row) {
	const lane = await row.evaluate((frame) => {
		const on = frame.querySelector('.lane.on');
		return { text: on?.textContent?.trim() ?? null, snooze: !!on?.classList.contains('snooze'), armed: frame.classList.contains('armed') };
	});
	expect(lane.snooze && lane.text === 'Snooze' && lane.armed, `a right swipe uncovers the armed Snooze lane, got ${JSON.stringify(lane)}`);
}

/** After a right swipe the row is back in place, not sliding off or folded. */
async function expectRowInPlace(page, id) {
	await page.waitForFunction((selector) => {
		const frame = document.querySelector(selector);
		const card = frame?.querySelector('.scard');
		return !!frame && !frame.classList.contains('sliding') && !frame.classList.contains('folded') && !frame.classList.contains('dragging')
			&& frame.getBoundingClientRect().height > 20 && !card?.style.transform;
	}, CARD(id), { timeout: 5000 }).catch(() => {
		throw new Error('the row did not spring back into place after the right swipe');
	});
}

const snoozePicker = (page) => page.getByRole('dialog', { name: 'Snooze chat' });

/** A swipe-right case's own chat, on the responsive phone UI or the native shell. */
async function swipeSetup({ page, status, log }, { settled = false } = {}) {
	page.__mobileShell = mobileShell;
	if (mobileShell) {
		await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
		expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
	} else {
		await gotoApp(page, status);
		await waitChat(page, { surface: 'new' });
	}
	const { session_id: id } = await api(page, 'POST', '/sessions', {});
	if (settled) await api(page, 'PATCH', `/sessions/${id}/active`, { is_active: false });
	// A collapsed Settled group renders no rows: waitLane opens it.
	await waitLane(page, id, settled ? 'settled' : 'active');
	page.__snoozeChat = id;
	log(`chat ${id}${settled ? ' (settled)' : ''}${mobileShell ? ' in /mobile/' : ''}`);
}

/**
 * Type a wake time `days` from now at 10:00 into the open picker's own field.
 * Late on a Sunday the picker has a single quick choice, so a second,
 * different time comes from here; returns the value typed.
 */
async function pickCustom(page, days) {
	const picker = snoozePicker(page);
	const value = await page.evaluate((days) => {
		const d = new Date();
		d.setDate(d.getDate() + days);
		const pad = (n) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T10:00`;
	}, days);
	await picker.getByLabel('Pick a date and time').fill(value);
	await picker.getByRole('button', { name: 'Snooze', exact: true }).click();
	await picker.waitFor({ state: 'hidden', timeout: 10_000 });
	return value;
}

/** Pick quick choice `index` in the open picker; returns its label. */
async function pickPreset(page, index) {
	const picker = snoozePicker(page);
	const choices = picker.getByRole('group', { name: 'Quick choices' }).getByRole('button');
	expect((await choices.count()) > index, `the picker offers quick choice ${index + 1}`);
	const picked = await choices.nth(index).getAttribute('aria-label');
	await choices.nth(index).click();
	await picker.waitFor({ state: 'hidden', timeout: 10_000 });
	return picked;
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
		},
		{
			name: `${mobileShell ? 'native' : 'responsive'}-swipe-right-snoozes`,
			viewports: ['phone'],
			setup: (ctx) => swipeSetup(ctx),
			async act({ page, mark }) {
				const id = page.__snoozeChat;
				await waitLane(page, id, 'active');

				await mark('swipe an active row right: the picker opens, the row stays');
				await swipeRow(page, id, 1, expectSnoozeLane);
				await snoozePicker(page).waitFor({ timeout: 10_000 });
				await snoozePicker(page).getByRole('heading', { name: 'Snooze until' }).waitFor();
				await expectRowInPlace(page, id);
				expect(await laneOf(page, id) === 'active', 'the row stays in Active until a time is picked');

				await mark('pick a quick choice: the row moves to Snoozed');
				const first = await pickPreset(page, 0);
				await waitLane(page, id, 'snoozed');
				await waitSaved(page, id, (row) => !row.is_active && !!row.snoozed_until, `the snooze (${first}) never reached the server`);
				const before = (await api(page, 'GET', `/sessions/${id}`)).snoozed_until;

				await mark('swipe the snoozed row right: a new wake time');
				await swipeRow(page, id, 1, expectSnoozeLane);
				await snoozePicker(page).waitFor({ timeout: 10_000 });
				await expectRowInPlace(page, id);
				const second = await pickCustom(page, 10);
				await waitSaved(page, id, (row) => !row.is_active && !!row.snoozed_until && row.snoozed_until !== before,
					`the new wake time (${second}) never replaced ${before}`);
				await waitLane(page, id, 'snoozed');

				await mark('swipe the snoozed row left: Move to Active, as before');
				await swipeRow(page, id, -1);
				await waitLane(page, id, 'active');
				await waitSaved(page, id, (row) => row.is_active && !row.snoozed_until, 'a left swipe on a snoozed row never moved it to Active');
				expect(!(await snoozePicker(page).isVisible()), 'a left swipe opens no picker');
			}
		},
		{
			name: `${mobileShell ? 'native' : 'responsive'}-swipe-right-dismissed`,
			viewports: ['phone'],
			setup: (ctx) => swipeSetup(ctx),
			async act({ page, mark }) {
				const id = page.__snoozeChat;
				await waitLane(page, id, 'active');

				await mark('swipe right, then dismiss the picker: nothing changes');
				await swipeRow(page, id, 1, expectSnoozeLane);
				const picker = snoozePicker(page);
				await picker.waitFor({ timeout: 10_000 });
				// Tap the scrim above the sheet, the way a thumb dismisses it.
				const sheet = await picker.boundingBox();
				await page.touchscreen.tap(sheet.x + sheet.width / 2, Math.max(8, sheet.y / 2));
				await picker.waitFor({ state: 'hidden', timeout: 10_000 });
				await expectRowInPlace(page, id);
				await page.waitForTimeout(1500);
				await waitLane(page, id, 'active', 1000);
				const row = await api(page, 'GET', `/sessions/${id}`);
				expect(row.is_active && !row.snoozed_until, `a dismissed picker changes nothing; the server holds ${JSON.stringify({ is_active: row.is_active, snoozed_until: row.snoozed_until })}`);
			}
		},
		{
			name: `${mobileShell ? 'native' : 'responsive'}-swipe-right-settled`,
			viewports: ['phone'],
			setup: (ctx) => swipeSetup(ctx, { settled: true }),
			async act({ page, mark }) {
				const id = page.__snoozeChat;
				await waitLane(page, id, 'settled');

				await mark('swipe a settled row right and pick a time: it moves to Snoozed');
				await swipeRow(page, id, 1, expectSnoozeLane);
				await snoozePicker(page).waitFor({ timeout: 10_000 });
				await expectRowInPlace(page, id);
				expect(await laneOf(page, id) === 'settled', 'the row stays in Settled until a time is picked');
				await pickPreset(page, 0);
				await waitLane(page, id, 'snoozed');
				await waitSaved(page, id, (row) => !row.is_active && !!row.snoozed_until, 'snoozing a settled chat never reached the server');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
