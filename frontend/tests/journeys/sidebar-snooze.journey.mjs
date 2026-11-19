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
// A wide row asks the same finger travel as a phone's: on the Fold's inner
// screen a slow 170px swipe opens the picker (right) and settles (left).
// Guards: the arm point grows with the row again (40% of the phone app's
// 856px list there is 342px). The responsive UI's sidebar is narrow at every
// width, so only `--mobile-shell` showed the bug. The phone app's drawer has
// the same rule (#1636): a slow 150px drag opens it from the chat and closes
// it from a group header on the Fold's inner screen as on its cover screen,
// and an 80px one does neither. Guards: the settle point grows with the
// screen again (30% of 884px is 265px). That case runs only with
// `--mobile-shell`, as the drawer is the phone app's.
//
// Snoozed and Settled rest at the bottom of the panel: Needs you and Active
// read from the top, and the spare height sits between the two. Guards: the
// quiet groups sit right under Active again, a long list squeezes its rows to
// fit instead of scrolling, or the last group cannot be scrolled into view.
//
// The chat is the case's own (created through the API, deleted by the runner
// afterwards), so no seeded chat is touched. A short wake (the server's loop
// and the page's local re-group at the due time) is covered by unit tests and
// checked by hand: waiting a minute per variant is too slow for the suite.
// `--mobile-shell` runs the swipe cases in the built `/mobile/` app.
import { runJourney } from './runner.mjs';
import { api, chatState, expect, gotoApp, showSidebar, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const CARD = (id) => `[data-session-card="${id}"]`;
const laneOf = (page, id) => page.evaluate((selector) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') ?? null, CARD(id));

/**
 * Resize the window and wait until the app shell has the new height. The
 * shell's height is `--app-viewport-height`, which the page writes from its
 * resize listener, a frame after `setViewportSize` returns; measuring before
 * that reads the old height (a 420px window still laid out 1600px tall).
 */
async function resizeWindow(page, size) {
	await page.setViewportSize(size);
	await page.waitForFunction(() => {
		const shell = getComputedStyle(document.documentElement).getPropertyValue('--app-viewport-height').trim();
		return !shell || shell === `${window.visualViewport?.height || window.innerHeight}px`;
	}, null, { timeout: 10_000 });
}

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
 * A finger along `path` (points { x, y, t }, `t` in ms after the press),
 * lifting at `liftAt` ms. Every touch event carries its own time, and the
 * phone app's drawer reads a drag's speed by those stamps, so the speed is
 * the path's however late a busy runner sends the events: nothing here
 * sleeps to make a speed.
 */
async function touchPath(page, path, liftAt) {
	const cdp = await page.context().newCDPSession(page);
	// Stamps in the recent past: Chrome clamps one from before the page
	// loaded, and the page has been open far longer than any gesture here.
	const base = Date.now() / 1000 - (liftAt + 50) / 1000;
	const send = (type, touchPoints, t) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints, timestamp: base + t / 1000 });
	try {
		const [press, ...moves] = path;
		await send('touchStart', [{ x: press.x, y: press.y }], press.t);
		for (const { x, y, t } of moves) await send('touchMove', [{ x, y }], t);
		await send('touchEnd', [], liftAt);
	} finally {
		await cdp.detach();
	}
}

/**
 * A slow finger drag `travel` px sideways from (x, y): 8px steps at
 * 0.27px/ms, resting 150ms before lifting, so only distance can complete it.
 */
async function dragScreen(page, x, y, travel) {
	const steps = Math.ceil(Math.abs(travel) / 8);
	const path = Array.from({ length: steps + 1 }, (_, i) => ({ x: x + (travel * i) / steps, y, t: i * 30 }));
	await touchPath(page, path, path.at(-1).t + 150);
}

/**
 * The phone app's drawer, once it has settled open or shut and finished its
 * slide: a touch that lands while it is still sliding in can hit the scrim.
 */
async function expectDrawer(page, open, what) {
	try {
		await page.waitForFunction((want) => {
			const drawer = document.getElementById('mobile-nav-drawer');
			if (!drawer || drawer.classList.contains('open') !== want || drawer.classList.contains('dragging')) return false;
			if (drawer.getAnimations().length > 0) return false;
			const box = drawer.getBoundingClientRect();
			return want ? Math.abs(box.left) < 1 : box.right <= 1;
		}, open, { timeout: 5_000 });
	} catch {
		throw new Error(what);
	}
}

/**
 * A finger dragging across the row, 60% of its width (past the 40% commit
 * point), paced like a real swipe and held before lifting so it commits on
 * distance, not as a flick. `beforeLift` looks at the row mid-drag.
 * `travel` instead drags a fixed number of pixels, in 8px steps at most
 * 0.3px/ms, far below the flick speed, so only distance can commit it.
 */
async function swipeRow(page, id, direction, beforeLift, { travel: fixed } = {}) {
	await showChats(page);
	const row = page.locator(CARD(id));
	await row.scrollIntoViewIfNeeded();
	const box = await row.boundingBox();
	const y = box.y + box.height / 2;
	// Start clear of the screen edges, where the system takes back swipes.
	const from = direction > 0 ? box.x + 40 : box.x + box.width - 40;
	const travel = (fixed ?? box.width * 0.6) * direction;
	const steps = fixed ? Math.ceil(fixed / 8) : 10;
	const cdp = await page.context().newCDPSession(page);
	const touch = (type, touchPoints) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints });
	try {
		await touch('touchStart', [{ x: from, y }]);
		for (let step = 1; step <= steps; step++) {
			await touch('touchMove', [{ x: from + (travel * step) / steps, y }]);
			await page.waitForTimeout(fixed ? 30 : 16);
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

/** Mid-drag, the lane behind a left-swiped active row is the armed Settle lane. */
async function expectSettleLane(row) {
	const lane = await row.evaluate((frame) => {
		const on = frame.querySelector('.lane.on');
		return { text: on?.textContent?.trim() ?? null, toggle: !!on?.classList.contains('toggle'), armed: frame.classList.contains('armed'), width: frame.offsetWidth };
	});
	expect(lane.toggle && lane.text === 'Settle' && lane.armed, `a left swipe uncovers the armed Settle lane, got ${JSON.stringify(lane)}`);
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

/** Where the chat list's groups and rows sit, in page pixels. */
async function listLayout(page) {
	const sessions = await showChats(page);
	return sessions.locator('.list').evaluate((list) => {
		const box = (el) => el.getBoundingClientRect();
		const rows = (group) => [...list.querySelectorAll(`[data-group="${group}"] [data-session-card]`)];
		const heads = Object.fromEntries([...list.querySelectorAll('.sect')].map((el) => [el.querySelector('.lbl').textContent.trim(), box(el).top]));
		return {
			top: box(list).top,
			// The list's own bottom padding stays under its last group.
			bottom: box(list).bottom - parseFloat(getComputedStyle(list).paddingBottom),
			fits: list.scrollHeight <= list.clientHeight,
			end: box(list.lastElementChild).bottom,
			heads,
			activeEnd: Math.max(...rows('active').map((el) => box(el).bottom)),
			rowHeights: rows('active').map((el) => Math.round(box(el).height))
		};
	});
}

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
			name: 'multi-select-actions',
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				// Chromium exercises the real controls but has no frameless Electron
				// window. Turn on its chrome styles and check the native drag source.
				if (variant.viewport === 'desktop') {
					await page.evaluate(() => document.documentElement.classList.add('desktop'));
				}
				page.__bulkChats = [];
				for (let i = 0; i < 2; i++) {
					const { session_id: id } = await api(page, 'POST', '/sessions', {});
					page.__bulkChats.push(id);
				}
				for (const id of page.__bulkChats) await waitLane(page, id, 'active');
			},
			async act({ page, mark, variant }) {
				const ids = page.__bulkChats;
				const chats = await showSidebar(page);
				const bar = chats.locator('.selbar');
				const action = (name) => bar.getByRole('button', { name, exact: true });
				const cancel = action('Cancel selection');
				const desktop = variant.viewport === 'desktop';
				const dragRegion = () => chats.locator('.p-head').evaluate(el => getComputedStyle(el).getPropertyValue('-webkit-app-region'));
				async function selectBoth() {
					const first = chats.locator(`${CARD(ids[0])} .scard`);
					await first.scrollIntoViewIfNeeded();
					const row = await first.boundingBox();
					await page.mouse.move(row.x + 40, row.y + row.height / 2);
					await page.mouse.down();
					try {
						await cancel.waitFor({ timeout: 5000 });
					} finally {
						await page.mouse.up();
					}
					await chats.locator(`${CARD(ids[1])} .scard`).click();
					await bar.getByText('2 selected', { exact: true }).waitFor();
				}

				await mark('select two chats and cancel from the toolbar');
				if (desktop) expect(await dragRegion() === 'drag', 'the regular desktop header is draggable');
				await selectBoth();
				if (desktop) {
					// Electron applies app regions in DOM order, not visual stacking
					// order. The covered header must not take the toolbar’s clicks.
					const region = await dragRegion();
					expect(region === 'no-drag', `the covered title bar steals clicks from the selection toolbar (app-region: ${region})`);
				}
				await cancel.click();
				await cancel.waitFor({ state: 'hidden' });
				if (desktop) expect(await dragRegion() === 'drag', 'cancel restores the desktop drag region');
				for (const id of ids) await waitLane(page, id, 'active');

				await mark('snooze both chats from the selection toolbar');
				await selectBoth();
				await action('Snooze').click();
				await pickPreset(page, 0);
				await cancel.waitFor({ state: 'hidden' });
				for (const id of ids) {
					await waitLane(page, id, 'snoozed');
					await waitSaved(page, id, row => !row.is_active && !!row.snoozed_until, `bulk Snooze did not save ${id}`);
				}

				await mark('settle both chats from the selection toolbar');
				await selectBoth();
				await action('Settle').click();
				await cancel.waitFor({ state: 'hidden' });
				for (const id of ids) {
					await waitLane(page, id, 'settled');
					await waitSaved(page, id, row => !row.is_active && !row.snoozed_until, `bulk Settle did not save ${id}`);
				}
			}
		},
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
			name: `${mobileShell ? 'native-' : ''}quiet-groups-rest-at-the-bottom`,
			...(mobileShell ? { viewports: ['phone'] } : {}),
			async setup({ page, status, log }) {
				page.__mobileShell = mobileShell;
				if (mobileShell) {
					await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
					expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
				} else {
					await gotoApp(page, status);
					await waitChat(page, { surface: 'new' });
				}
				const { session_id: snoozed } = await api(page, 'POST', '/sessions', {});
				const { session_id: settled } = await api(page, 'POST', '/sessions', {});
				await api(page, 'PATCH', `/sessions/${snoozed}/active`, { is_active: false, snoozed_until: new Date(Date.now() + 86_400_000).toISOString() });
				await api(page, 'PATCH', `/sessions/${settled}/active`, { is_active: false });
				// Tall enough that every seeded row fits with room to spare.
				page.__viewport = page.viewportSize();
				await resizeWindow(page, { width: page.__viewport.width, height: 1600 });
				await waitLane(page, snoozed, 'snoozed');
				const sessions = await showChats(page);
				await sessions.getByRole('button', { name: /^Settled/ }).first().waitFor({ timeout: 30_000 });
				log(`snoozed ${snoozed}, settled ${settled}`);
			},
			async act({ page, mark }) {
				await mark('a list that fits: the quiet groups sit at the bottom');
				const tall = await listLayout(page);
				expect(tall.fits, 'the seeded list fits a 1600px tall window');
				expect(Math.abs(tall.heads.Active - tall.top) <= 1, `Active starts at the top of the list, got ${tall.heads.Active - tall.top}px below it`);
				expect(tall.heads.Snoozed < tall.heads.Settled, 'Snoozed sits above Settled');
				expect(Math.abs(tall.end - tall.bottom) <= 1, `the last group ends at the bottom of the list, got ${Math.round(tall.bottom - tall.end)}px short`);
				expect(tall.heads.Snoozed - tall.activeEnd > 100, `the spare height sits between Active and Snoozed, got ${Math.round(tall.heads.Snoozed - tall.activeEnd)}px`);

				await mark('double-click Settled: its header moves, what takes its place is not clicked');
				const chats = await showChats(page);
				const header = chats.getByRole('button', { name: /^Settled/ }).first();
				if (await header.getAttribute('aria-expanded') === 'true') {
					await header.click();
					await page.waitForTimeout(700);
				}
				const before = JSON.stringify(await chatState(page));
				const box = await header.boundingBox();
				const spot = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
				const under = () => page.evaluate(({ x, y }) => {
					const el = document.elementFromPoint(x, y);
					return el?.closest('.sect') ? 'header' : el?.closest('[data-session-card]') ? 'row' : 'other';
				}, spot);
				await page.mouse.click(spot.x, spot.y);
				await page.waitForFunction((el) => el.getAttribute('aria-expanded') === 'true', await header.elementHandle());
				expect(await under() !== 'header', 'opening Settled moves its header away from the pointer');
				// The second half of the double-click, on the spot the header left.
				await page.mouse.down({ clickCount: 2 });
				await page.mouse.up({ clickCount: 2 });
				await page.waitForTimeout(600);
				// The chat first: on a phone, opening one closes the drawer and hides the header.
				expect(JSON.stringify(await chatState(page)) === before, 'the open chat did not change');
				expect(await header.getAttribute('aria-expanded') === 'true', 'Settled stays open');
				// Closed again for the measurements below; by now a click is a click.
				await header.click();
				await page.waitForFunction((el) => el.getAttribute('aria-expanded') === 'false', await header.elementHandle());

				await mark('a list taller than the window: it scrolls, nothing is squeezed');
				await resizeWindow(page, { width: page.__viewport.width, height: 420 });
				const short = await listLayout(page);
				expect(!short.fits, 'the seeded list overflows a 420px tall window');
				expect(JSON.stringify(short.rowHeights) === JSON.stringify(tall.rowHeights), `rows keep their height, got ${short.rowHeights} for ${tall.rowHeights}`);
				expect(short.heads.Snoozed - short.activeEnd < 16, `Snoozed follows the last active row, got ${Math.round(short.heads.Snoozed - short.activeEnd)}px after it`);
				const sessions = await showChats(page);
				await sessions.locator('.list').evaluate((list) => list.scrollTo({ top: list.scrollHeight }));
				const scrolled = await listLayout(page);
				expect(Math.abs(scrolled.end - scrolled.bottom) <= 1, `scrolling to the end shows the last group, got ${Math.round(scrolled.end - scrolled.bottom)}px past the bottom`);

				await page.setViewportSize(page.__viewport);
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
			// A wide row (the Fold's inner screen, where the phone app's list is
			// 856px wide) must commit on the same finger travel as a phone's: a
			// slow 170px drag either way, past the 160px cap. 40% of the row
			// would have asked for 342px.
			name: `${mobileShell ? 'native' : 'responsive'}-swipe-fixed-travel`,
			viewports: ['fold-inner', 'fold-cover'],
			setup: (ctx) => swipeSetup(ctx),
			async act({ page, mark, log }) {
				const id = page.__snoozeChat;
				await waitLane(page, id, 'active');
				log(`row ${await page.locator(CARD(id)).evaluate((frame) => frame.offsetWidth)}px wide`);

				await mark('slow 170px swipe right: the picker opens');
				await swipeRow(page, id, 1, expectSnoozeLane, { travel: 170 });
				const picker = snoozePicker(page);
				await picker.waitFor({ timeout: 10_000 });
				const sheet = await picker.boundingBox();
				await page.touchscreen.tap(sheet.x + sheet.width / 2, Math.max(8, sheet.y / 2));
				await picker.waitFor({ state: 'hidden', timeout: 10_000 });
				await expectRowInPlace(page, id);

				await mark('slow 170px swipe left: the row settles');
				await swipeRow(page, id, -1, expectSettleLane, { travel: 170 });
				await waitLane(page, id, 'settled');
				await waitSaved(page, id, (row) => !row.is_active && !row.snoozed_until, 'the left swipe never settled the chat');
			}
		},
		...(mobileShell ? [{
			// The phone app's drawer asks the same finger travel on a wide
			// screen as on a phone (#1636): a slow 150px drag opens and closes
			// it, an 80px one does neither.
			name: 'native-drawer-fixed-travel',
			viewports: ['fold-inner', 'fold-cover'],
			setup: (ctx) => swipeSetup(ctx),
			async act({ page, mark, log }) {
				const { width, height } = page.viewportSize();
				log(`screen ${width}px wide`);
				const header = page.locator('#mobile-nav-drawer').getByRole('button', { name: /^Active/ }).first();
				await page.locator('#mobile-nav-drawer .drawer-close').click();
				await expectDrawer(page, false, 'the drawer never closed from its close button');

				await mark('slow 80px drag right on the chat: the drawer stays shut');
				await dragScreen(page, 40, height / 2, 80);
				await expectDrawer(page, false, 'an 80px drag opened the drawer');

				await mark('slow 150px drag right on the chat: the drawer opens');
				await dragScreen(page, 40, height / 2, 150);
				await expectDrawer(page, true, `a slow 150px drag did not open the drawer on a ${width}px screen`);

				const box = await header.boundingBox();
				const from = Math.min(box.x + box.width - 40, width - 40);
				await mark('slow 80px drag left on a group header: the drawer stays open');
				await dragScreen(page, from, box.y + box.height / 2, -80);
				await expectDrawer(page, true, 'an 80px drag closed the drawer');

				await mark('slow 150px drag left on a group header: the drawer closes');
				await dragScreen(page, from, box.y + box.height / 2, -150);
				await expectDrawer(page, false, `a slow 150px drag did not close the drawer on a ${width}px screen`);
			}
		}] : []),
		// Isolate each fresh chat's setup from the recording: inserting sixteen
		// rows during one recording measures the fixtures' accumulated layout
		// shifts, not flick reliability. Eight independent trials per direction.
		...Array.from({ length: 8 }, (_, trial) => [1, -1].map((direction) => ({
			name: `${mobileShell ? 'native' : 'responsive'}-flick-commits-${direction > 0 ? 'right' : 'left'}-${trial + 1}`,
			viewports: mobileShell ? ['phone', 'fold-inner'] : ['phone'],
			async setup(ctx) {
				await swipeSetup(ctx);
				if (mobileShell && ctx.page.viewportSize().width === 884) {
					await resizeWindow(ctx.page, { width: 884, height: 768 });
				}
				if (mobileShell) await expectDrawer(ctx.page, true, 'the drawer must be fully open before touching a row');
			},
			async act({ page, mark, log }) {
				const id = page.__snoozeChat;
				const row = page.locator(CARD(id));
				await row.scrollIntoViewIfNeeded();
				const box = await row.boundingBox();
				const x = box.x + box.width * (direction > 0 ? 0.25 : 0.75);
				const y = box.y + box.height / 2;
				await row.evaluate((frame) => {
					frame.__flickTrace = [];
					for (const kind of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) {
						frame.addEventListener(kind, (e) => frame.__flickTrace.push({
							kind, x: e.clientX, y: e.clientY, t: e.timeStamp, handled: performance.now(), trusted: e.isTrusted
						}), { capture: true });
					}
				});
				await mark(`80px/20ms flick ${direction > 0 ? 'right' : 'left'}, trial ${trial + 1}/8`);
				await touchPath(page, Array.from({ length: 9 }, (_, i) => ({
					x: x + direction * 10 * i, y, t: i * 2.5
				})), 22);
				log(`${page.viewportSize().width}x${page.viewportSize().height}: ${JSON.stringify(await row.evaluate((frame) => frame.__flickTrace))}`);
				if (direction > 0) {
					await snoozePicker(page).waitFor({ timeout: 5000 });
					await expectRowInPlace(page, id);
					const saved = await api(page, 'GET', `/sessions/${id}`);
					expect(saved.is_active && !saved.snoozed_until, 'opening the picker must not change the chat');
				} else {
					// Wait for the action's server result, not a timer guess or painted transform.
					await waitSaved(page, id, (saved) => !saved.is_active && !saved.snoozed_until, 'the flick never settled the chat');
				}
			}
		}))).flat(),
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
