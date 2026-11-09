// Global Away: the one switch Shayne flicks at bedtime, and the band that means
// he cannot forget it in the morning.
//
//   flip-on-off      the switch turns it on and off, the band follows, and
//                    the composer does not move
//   band-on-*        every screen shows the band and the way off when it is on
//   per-chat-held    a chat's own Away reads on and locked while global is on,
//                    then goes back to that chat's own value with no reload
//   other-browser    a change made in another browser shows up without a reload
//   put-refused      a refused write leaves the switch truthful and says so
//   native-flip      the real phone bundle (/mobile/, local only: --mobile-shell)
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, isPhone, openNewChat, openPopup, scriptTag, seededChatId, typeAndSend, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const NAME = 'Away for all chats';
const HELD_NOTE = 'On for all chats';
const TIMEOUT = 15_000;

const allChatsSwitch = (page, checked) =>
	page.getByRole('switch', { name: NAME, exact: true, ...(checked === undefined ? {} : { checked }) }).locator('visible=true').first();

const setGlobal = (page, away) => api(page, 'PUT', '/away-mode', { away });

/** The shared instance must never be left away: every later journey would run with it. */
async function leavingItOff(page, body) {
	try {
		await body();
	} finally {
		await setGlobal(page, false).catch(() => {});
	}
}

async function waitSwitch(page, on) {
	await allChatsSwitch(page, on).waitFor({ timeout: TIMEOUT });
}

async function flip(page, to) {
	await allChatsSwitch(page, !to).click();
	await waitSwitch(page, to);
}

/** The band across the very top: it must be there, full width and click-through, or gone. */
async function waitBand(page, on) {
	await page.locator(`[data-ui-away-all="${on ? 'on' : 'off'}"]`).waitFor({ state: 'attached', timeout: TIMEOUT });
	await page.waitForFunction(
		(on) => {
			const band = document.querySelector('[data-ui-away-all]');
			return !!band && (on ? getComputedStyle(band).opacity === '1' : getComputedStyle(band).opacity === '0');
		},
		on,
		{ timeout: TIMEOUT }
	);
	if (!on) return;
	const band = await page.evaluate(() => {
		const element = document.querySelector('[data-ui-away-all]');
		const box = element.getBoundingClientRect();
		return { top: box.top, height: box.height, width: box.width, viewport: innerWidth, events: getComputedStyle(element).pointerEvents };
	});
	expect(band.top === 0 && band.height === 2, `the band sits on the top edge, 2px tall (${JSON.stringify(band)})`);
	expect(band.width === band.viewport, `the band spans the screen (${JSON.stringify(band)})`);
	expect(band.events === 'none', 'the band can never swallow a tap');
}

/** Where the composer sits, so turning Away on or off can be held to not moving it. */
const composerBox = async (page) => page.getByRole('textbox', { name: 'Message' }).first().boundingBox();

function sameBox(before, after, what) {
	expect(!!before && !!after, `the composer is on screen ${what}`);
	const moved = ['x', 'y', 'width', 'height'].filter((key) => Math.abs(before[key] - after[key]) > 0.5);
	expect(moved.length === 0, `the composer did not move ${what} (${JSON.stringify({ before, after })})`);
}

/**
 * The chat's own Away control where each width keeps it: a row in the
 * permission menu at desktop width, a switch in Chat controls on a phone.
 * Opens it, reads it, closes it again so the global switch stays reachable.
 */
async function readChatAway(page, variant) {
	if (isPhone(variant)) {
		const sheet = page.getByRole('dialog', { name: 'Chat controls', exact: true });
		await openPopup(page.getByRole('button', { name: /^All chat controls/ }), sheet);
		const row = sheet.getByRole('switch', { name: /^Away mode/ });
		await row.waitFor({ timeout: TIMEOUT });
		const state = {
			checked: (await row.getAttribute('aria-checked')) === 'true',
			disabled: (await row.getAttribute('aria-disabled')) === 'true',
			text: await row.innerText()
		};
		await page.keyboard.press('Escape');
		await sheet.waitFor({ state: 'hidden', timeout: TIMEOUT });
		return state;
	}
	const row = page.getByRole('menuitemcheckbox', { name: /^Away/ });
	await openPopup(page.getByRole('button', { name: /^Permission mode:/ }), row);
	const state = {
		checked: (await row.getAttribute('aria-checked')) === 'true',
		disabled: await row.isDisabled(),
		text: await row.innerText()
	};
	await page.keyboard.press('Escape');
	await row.waitFor({ state: 'hidden', timeout: TIMEOUT });
	return state;
}

/** Chat controls slides up from below the screen: wait for the row to have landed on it. */
async function untilOnScreen(page, row) {
	const deadline = Date.now() + TIMEOUT;
	let last = null;
	while (Date.now() < deadline) {
		const box = await row.boundingBox();
		if (box && last && box.y === last.y && box.y >= 0 && box.y + box.height <= page.viewportSize().height) return;
		last = box;
		await page.waitForTimeout(50);
	}
	throw new Error(`the row never settled on screen (${JSON.stringify(last)})`);
}

/** Tap the chat's own Away control; a control that is held must simply not respond. */
async function tapChatAway(page, variant) {
	if (isPhone(variant)) {
		const sheet = page.getByRole('dialog', { name: 'Chat controls', exact: true });
		await openPopup(page.getByRole('button', { name: /^All chat controls/ }), sheet);
		const row = sheet.getByRole('switch', { name: /^Away mode/ });
		await untilOnScreen(page, row);
		// `force`: a held row is aria-disabled, and Playwright will not tap one without it.
		await row.click({ force: true });
		await page.waitForTimeout(400);
		await page.keyboard.press('Escape');
		await sheet.waitFor({ state: 'hidden', timeout: TIMEOUT });
		return;
	}
	const row = page.getByRole('menuitemcheckbox', { name: /^Away/ });
	await openPopup(page.getByRole('button', { name: /^Permission mode:/ }), row);
	await row.click({ force: true });
	await page.waitForTimeout(400);
	await page.keyboard.press('Escape');
	await row.waitFor({ state: 'hidden', timeout: TIMEOUT }).catch(() => {});
}

async function waitChatAway(page, variant, want) {
	const deadline = Date.now() + TIMEOUT;
	let state = null;
	while (Date.now() < deadline) {
		state = await readChatAway(page, variant);
		if (state.checked === want.checked && state.disabled === want.disabled) return state;
		await page.waitForTimeout(300);
	}
	throw new Error(`the chat's Away control never read ${JSON.stringify(want)}: ${JSON.stringify(state)}`);
}

function bandCase(name, open) {
	return {
		name: `band-on-${name}`,
		async setup({ page, status }) {
			await gotoApp(page, status);
			await setGlobal(page, true);
			await open(page, status);
			await waitSwitch(page, true);
		},
		async act({ page, mark }) {
			await leavingItOff(page, async () => {
				await mark(`${name}: Away is on and the band says so`);
				await waitBand(page, true);
				await mark(`${name}: one tap turns it off from here`);
				await flip(page, false);
				await waitBand(page, false);
			});
		}
	};
}

/** Settings is a view inside the app, not a page of its own: go there the way a person does. */
async function openSettings(page) {
	const workspace = page.getByRole('navigation', { name: 'Workspace', exact: true });
	if (!(await workspace.isVisible())) await page.getByRole('button', { name: /^Chats(?: \(|$)|Show sidebar/ }).first().click();
	await workspace.getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('navigation', { name: 'Settings', exact: true }).waitFor({ timeout: TIMEOUT });
}

await runJourney({
	name: 'away-all-chats',
	allow: [...DEV_INSTANCE_NOISE],
	cases: [
		{
			name: 'flip-on-off',
			async setup({ page, status }) {
				await gotoApp(page, status, `/?session=${seededChatId(status, 'long')}`);
				await setGlobal(page, false);
				await waitSwitch(page, false);
				await waitBand(page, false);
			},
			async act({ page, mark }) {
				await leavingItOff(page, async () => {
					const before = await composerBox(page);
					await mark('turn it on');
					await flip(page, true);
					await waitBand(page, true);
					sameBox(before, await composerBox(page), 'when Away came on');
					await mark('turn it off');
					await flip(page, false);
					await waitBand(page, false);
					sameBox(before, await composerBox(page), 'when Away went off');
				});
			}
		},
		bandCase('new-chat', (page, status) => gotoApp(page, status)),
		bandCase('settings', (page) => openSettings(page)),
		{
			name: 'per-chat-held',
			async setup(ctx) {
				const { page, variant } = ctx;
				await openNewChat(ctx);
				await setGlobal(page, false);
				await typeAndSend(page, `${scriptTag(variant, 'default')} away per-chat row`);
				await waitTurnSettled(page);
				await waitSwitch(page, false);
			},
			async act({ page, mark, variant }) {
				await leavingItOff(page, async () => {
					await mark('the chat starts with its own Away off');
					await waitChatAway(page, variant, { checked: false, disabled: false });

					await mark('global on: the row reads on and locked, and says why');
					await flip(page, true);
					const held = await waitChatAway(page, variant, { checked: true, disabled: true });
					expect(held.text.includes(HELD_NOTE), `the row explains itself ("${held.text}")`);

					await mark('a tap on the locked row does nothing');
					await tapChatAway(page, variant);
					await waitSwitch(page, true);
					await waitChatAway(page, variant, { checked: true, disabled: true });

					await mark('global off: back to the chat\'s own value, no reload');
					await flip(page, false);
					const restored = await waitChatAway(page, variant, { checked: false, disabled: false });
					expect(!restored.text.includes(HELD_NOTE), `the explanation goes with it ("${restored.text}")`);

					await mark('the chat\'s own Away turns on');
					await tapChatAway(page, variant);
					await waitChatAway(page, variant, { checked: true, disabled: false });

					await mark('global on holds it');
					await flip(page, true);
					await waitChatAway(page, variant, { checked: true, disabled: true });

					await mark('global off: the chat\'s own Away is still on');
					await flip(page, false);
					await waitChatAway(page, variant, { checked: true, disabled: false });

					await mark('and turns off again like it always did');
					await tapChatAway(page, variant);
					await waitChatAway(page, variant, { checked: false, disabled: false });
				});
			}
		},
		{
			name: 'other-browser',
			viewports: ['desktop'],
			async setup({ page, status }) {
				await gotoApp(page, status);
				await setGlobal(page, false);
				await waitSwitch(page, false);
			},
			async act({ page, status, mark }) {
				const other = await page.context().browser().newContext();
				await leavingItOff(page, async () => {
					try {
						await other.addCookies(await page.context().cookies());
						const otherPage = await other.newPage();
						await otherPage.goto(status.urls.ui);
						await waitSwitch(otherPage, false);

						await mark('the other browser turns it on; this one follows with no reload');
						await flip(otherPage, true);
						await waitSwitch(page, true);
						await waitBand(page, true);

						await mark('this browser turns it off; the other one follows');
						await flip(page, false);
						await waitBand(page, false);
						await waitSwitch(otherPage, false);
					} finally {
						await other.close();
					}
				});
			}
		},
		{
			name: 'put-refused',
			viewports: ['desktop'],
			allow: [{ kind: 'failed_requests', status: 500, url: /\/away-mode(\?|$)/, reason: 'the case refuses this write on purpose to judge the failure path' }],
			async setup({ page, status }) {
				await gotoApp(page, status);
				await setGlobal(page, false);
				await waitSwitch(page, false);
			},
			async act({ page, mark }) {
				await leavingItOff(page, async () => {
					await page.route('**/api/v1/away-mode', (route) =>
						route.request().method() === 'PUT'
							? route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"refused by journey"}' })
							: route.continue()
					);
					try {
						await mark('the write is refused');
						await allChatsSwitch(page, false).click();
						await waitSwitch(page, false);
						await waitBand(page, false);
						await mark('the bell says what happened');
						const bell = page.getByRole('button', { name: /^Notifications( \(\d+ unread\))?$/ }).first();
						const panel = page.getByRole('dialog', { name: 'Notifications', exact: true });
						await openPopup(bell, panel);
						await panel.getByText('Could not turn it on', { exact: false }).first().waitFor({ timeout: TIMEOUT });
						await page.keyboard.press('Escape');
						const checked = await allChatsSwitch(page).getAttribute('aria-checked');
						expect(checked === 'false', 'the switch still says off');
					} finally {
						await page.unroute('**/api/v1/away-mode');
					}
				});
			}
		},
		...(mobileShell
			? [
					{
						name: 'native-flip',
						viewports: ['phone'],
						async setup({ page, status }) {
							await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
							expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
							await allChatsSwitch(page).waitFor({ timeout: 90_000 });
							await setGlobal(page, false);
							await waitSwitch(page, false);
						},
						async act({ page, mark }) {
							await leavingItOff(page, async () => {
								await mark('real phone bundle: turn it on');
								await flip(page, true);
								await waitBand(page, true);
								await mark('real phone bundle: turn it off');
								await flip(page, false);
								await waitBand(page, false);
								expect(new URL(page.url()).pathname === '/mobile/', 'still the real phone bundle');
							});
						}
					}
				]
			: [])
	]
});
