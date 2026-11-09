// Journey: the composer on a phone. Touch-sized screens only: the phone and
// the Fold's cover screen (the Fold's inner screen is tablet-wide and lays the
// composer out like the desktop, which the other journeys cover).
//
// Guards: focusing, typing and sending never move the composer's bottom edge
// (it may only grow upwards as text wraps), its round buttons are round and
// big enough to tap, every sheet/popover it opens fits the screen and closes
// with Escape or a tap outside without moving anything, and a send that cannot
// go out says why at once (the new-session screen without an agent).
//
// Cases:
//   type-and-send        focus, type, send in an open chat; reply settles
//   sheets               model, tool access, effort and context sheets: open, close
//   new-chat-needs-agent tap Send with no agent: told why, text kept; pick one, send
//   controls-pages       Chat controls: Project page fills the sheet; pick and remove
//                        a project, then Runs on and back never shift the pages sideways
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	composer,
	countUserRows,
	expect,
	gotoApp,
	pickAgent,
	seededChatId,
	sendButton,
	typeAndSend,
	uniq,
	waitChat,
	waitChatOpen,
	waitTurnSettled,
	withoutSavedAgentPick
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PHONE_SIZES = ['phone', 'fold-cover'];
const INPUT = 'Chat input with file drop';

/** The composer's box and its round buttons' geometry. */
const composerGeometry = (page) =>
	page.evaluate((label) => {
		const region = document.querySelector(`[aria-label="${label}"]`);
		if (!region) return null;
		const box = region.getBoundingClientRect();
		// The resting circle right of dictate is Send, or Talk when voice calls
		// are on (composerCircles): the dev instance seeds voice on.
		const visible = (name) =>
			[...region.querySelectorAll(`button[aria-label="${name}"]`)].find((el) => el.getClientRects().length > 0);
		const round = [['Send', 'Start a call in this conversation'], ['Attach files'], ['Start voice input']].map((names) => {
			const found = names.map((n) => [n, visible(n)]).find(([, el]) => el);
			const name = found ? found[0] : names.join(' or ');
			const el = found?.[1];
			if (!el) return { name, missing: true };
			const r = el.getBoundingClientRect();
			return { name, w: Math.round(r.width), h: Math.round(r.height), radius: parseFloat(getComputedStyle(el).borderTopLeftRadius) };
		});
		return { top: Math.round(box.top), bottom: Math.round(box.bottom), left: Math.round(box.left), right: Math.round(box.right), vw: innerWidth, round };
	}, INPUT);

function expectComposerFits(geometry, what) {
	expect(geometry, `${what}: the composer is on screen`);
	expect(geometry.left >= -1 && geometry.right <= geometry.vw + 1, `${what}: the composer fits the screen width (${JSON.stringify(geometry)})`);
	for (const button of geometry.round) {
		expect(!button.missing, `${what}: the ${button.name} button is there`);
		expect(Math.abs(button.w - button.h) <= 1, `${what}: ${button.name} is round, not ${button.w}×${button.h}`);
		expect(button.radius >= button.w / 2 - 1, `${what}: ${button.name} has round corners (radius ${button.radius}, width ${button.w})`);
		expect(button.w >= 32, `${what}: ${button.name} is at least 32px to tap (${button.w})`);
	}
}

function expectSameBottom(before, after, what) {
	expect(Math.abs(before.bottom - after.bottom) <= 2, `${what}: the composer's bottom edge stayed put (${before.bottom} → ${after.bottom})`);
}

const openSeededChat = async (ctx) => {
	const { page, status } = ctx;
	await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`);
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
	ctx.start = await composerGeometry(page);
	expectComposerFits(ctx.start, 'at rest');
};

/** Sheets the composer opens on a phone: the trigger and what it opens. */
const SHEETS = [
	{ trigger: /^Model: /, sheet: (page) => page.getByRole('dialog', { name: 'Choose model' }) },
	{ trigger: /^Permission mode: /, sheet: (page) => page.getByRole('dialog', { name: 'Tool access' }) },
	// Only for a model with effort levels to pick from: the dev instance's scripted AI has none.
	{ trigger: /^Effort: /, optional: true, sheet: (page) => page.getByRole('dialog', { name: 'Reasoning effort' }) },
	// A chat with nothing used shows no ring on the composer: Chat controls keeps the row.
	{ trigger: /^Context and usage/, inControls: true, sheet: (page) => page.getByRole('dialog', { name: 'Context and usage' }) }
];

/**
 * Chat controls' sliding pages: the pages box's sideways scroll, the active
 * page and its hosted menu's boxes, and the main page's Setup rows.
 */
const controlsPages = (page) =>
	page.evaluate(() => {
		const box = (el) => {
			if (!el) return null;
			const r = el.getBoundingClientRect();
			return { left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width) };
		};
		const pages = document.querySelector('.mcp-pages');
		const active = document.querySelector('.mcp-page.active');
		const menu = active?.querySelector('.mcp-host > *');
		return {
			vw: innerWidth,
			scrollLeft: pages?.scrollLeft ?? null,
			page: active?.dataset.page ?? null,
			pageBox: box(active),
			menuBox: box(menu),
			rows: [...document.querySelectorAll('.mcp-page[data-page="main"] .mcp-setup .ctx-chip')].map((el) => ({
				label: el.getAttribute('aria-label'),
				...box(el)
			}))
		};
	});

function expectPagesInPlace(state, what) {
	expect(state.scrollLeft === 0, `${what}: the pages are not scrolled sideways (scrollLeft ${state.scrollLeft})`);
	expect(state.pageBox && state.pageBox.left >= -1 && state.pageBox.right <= state.vw + 1, `${what}: the ${state.page} page is on screen (${JSON.stringify(state.pageBox)})`);
}

function expectMenuFillsPage(state, what) {
	const { menuBox, pageBox } = state;
	// The hosted menu sits inside the page's own side padding (16px each side).
	expect(menuBox && pageBox && menuBox.width >= pageBox.width - 48, `${what}: the menu fills the page (${JSON.stringify({ menuBox, pageBox })})`);
	expect(menuBox.left >= pageBox.left - 1 && menuBox.right <= pageBox.right + 1, `${what}: the menu stays inside the page (${JSON.stringify({ menuBox, pageBox })})`);
}

await runJourney({
	name: 'phone-composer',
	viewports: PHONE_SIZES,
	cases: [
		{
			name: 'type-and-send',
			setup: openSeededChat,
			async act({ page, mark, variant, start }) {
				await mark('focus the composer');
				await composer(page).tap();
				await page.waitForTimeout(500);
				expectSameBottom(start, await composerGeometry(page), 'on focus');

				await mark('type');
				const token = `tok-${uniq()}`;
				await page.keyboard.type(`a phone message ${token} that is long enough to wrap onto a second line `, { delay: 5 });
				await page.waitForTimeout(400);
				const typed = await composerGeometry(page);
				expectSameBottom(start, typed, 'while typing');
				expect(await sendButton(page).isEnabled(), 'Send is enabled once there is text');

				await mark('send');
				await page.keyboard.type(fakeAiTag(variant));
				await sendButton(page).tap();
				await waitTurnSettled(page, { text: `You said: “a phone message ${token}`, messagesAtLeast: 4 });
				expect((await countUserRows(page, token)) === 1, 'the message shows once');
				expect((await composer(page).innerText()).trim() === '', 'the composer is empty after the send');
				const after = await composerGeometry(page);
				expectSameBottom(start, after, 'after the send');
				expectComposerFits(after, 'after the send');
			}
		},
		{
			name: 'sheets',
			setup: openSeededChat,
			async act({ page, mark, start }) {
				for (const [index, { trigger, optional, inControls, sheet }] of SHEETS.entries()) {
					const label = String(trigger).replace(/[/^:]/g, '').trim();
					if (inControls) {
						await mark(`open Chat controls for ${label}`);
						await page.getByRole('button', { name: /^All chat controls/ }).tap();
						await page.getByRole('dialog', { name: 'Chat controls' }).waitFor({ timeout: 5000 });
					}
					const scope = inControls ? page.getByRole('dialog', { name: 'Chat controls' }) : page.getByRole('region', { name: INPUT });
					const button = scope.getByRole('button', { name: trigger }).first();
					if (optional && !(await button.isVisible())) {
						expect(
							(await page.getByRole('region', { name: INPUT }).getByText('Unknown', { exact: true }).count()) === 0,
							`${label}: a model with nothing to pick does not say "Unknown"`
						);
						continue;
					}
					await mark(`open ${label}`);
					await button.tap();
					const opened = sheet(page);
					await opened.waitFor({ timeout: 5000 });
					await page.waitForTimeout(400);
					const box = await opened.boundingBox();
					const vp = page.viewportSize();
					expect(
						box && box.x >= -1 && box.x + box.width <= vp.width + 1 && box.y >= -1 && box.y + box.height <= vp.height + 1,
						`${label} fits the screen (${JSON.stringify(box)} in ${vp.width}×${vp.height})`
					);

					// Alternate the two ways a phone user closes a sheet.
					if (index % 2 === 0) {
						await mark(`close ${label} with Escape`);
						await page.keyboard.press('Escape');
					} else {
						await mark(`close ${label} with a tap outside`);
						// The tall sheet covers the transcript: outside is the strip above it.
						if (inControls) await page.touchscreen.tap(vp.width / 2, 8);
						// Clear of the turn rail, which takes taps along the transcript's left edge.
						else await page.getByRole('log', { name: 'Conversation messages' }).tap({ position: { x: 60, y: 20 } });
					}
					await opened.waitFor({ state: 'hidden', timeout: 5000 });
					await page.waitForTimeout(400);
					expectSameBottom(start, await composerGeometry(page), `after ${label}`);
				}
			}
		},
		{
			name: 'new-chat-needs-agent',
			async setup({ page, status }) {
				// A user with no saved agent pick (the seed saves one for everybody).
				await withoutSavedAgentPick(page);
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			async act({ page, mark, variant }) {
				const token = `tok-${uniq()}`;
				const text = `no agent yet ${token} ${fakeAiTag(variant)}`.trim();
				await mark('send without an agent');
				await typeAndSend(page, text);
				const said = page.getByRole('region', { name: 'Alerts' }).getByText('Select an agent first.');
				await said.waitFor({ timeout: 1500 });
				await waitChat(page, { surface: 'new', messages: '0' });
				expect((await composer(page).innerText()).includes(token), 'the typed text is kept');

				await mark('pick an agent and send');
				const hero = page.getByRole('region', { name: 'New chat' });
				await pickAgent(page, hero);
				await sendButton(page).tap();
				await waitTurnSettled(page, { text: `You said: “no agent yet ${token}`, messagesAtLeast: 2 });
				expect((await countUserRows(page, token)) === 1, 'the message shows once');
			}
		},
		{
			name: 'controls-pages',
			async setup({ page, status }) {
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			async act({ page, mark }) {
				const sheet = page.locator('.mcp[role="dialog"]');
				const settle = () => page.waitForTimeout(500);
				const onPage = (name) =>
					page.waitForFunction((n) => document.querySelector('.mcp-page.active')?.dataset.page === n, name, { timeout: 5000 });
				const openPage = async (name, trigger) => {
					await sheet.locator('.mcp-page[data-page="main"]').getByRole('button', { name: trigger }).first().tap();
					await onPage(name);
					await settle();
				};

				await mark('open Chat controls');
				await page.getByRole('button', { name: /^All chat controls/ }).tap();
				await page.getByRole('dialog', { name: 'Chat controls' }).waitFor({ timeout: 5000 });
				await settle();

				await mark('open the Project page');
				await openPage('project', /^No project/);
				let state = await controlsPages(page);
				expectPagesInPlace(state, 'Project page');
				expectMenuFillsPage(state, 'Project page');

				await mark('pick a project');
				await sheet.locator('.mcp-page[data-page="project"]').getByRole('button', { name: 'Dev sandbox' }).tap();
				await onPage('main');
				await settle();

				// The bug: removing the project, then Runs on, left the pages box
				// scrolled sideways (its menu focused a row on a page still off screen).
				await mark('remove the project');
				await sheet.getByRole('button', { name: /^Workspace folders: / }).first().tap();
				await sheet.getByRole('button', { name: /: start without a project$/ }).tap();
				await sheet.getByRole('button', { name: /^No project/ }).first().waitFor({ timeout: 5000 });
				await settle();

				await mark('open Runs on');
				await openPage('runner', /^Runs on/);
				state = await controlsPages(page);
				expectPagesInPlace(state, 'Runs on after removing the project');
				expectMenuFillsPage(state, 'Runs on after removing the project');

				await mark('back to Chat controls');
				await sheet.getByRole('button', { name: 'Back to chat controls' }).tap();
				await onPage('main');
				await settle();
				state = await controlsPages(page);
				expectPagesInPlace(state, 'Chat controls after Runs on');
				expect(state.rows.length >= 3, `the Setup rows are there (${JSON.stringify(state.rows)})`);
				for (const row of state.rows) {
					expect(row.left >= 0 && row.right <= state.vw, `the "${row.label}" row is fully in view (${JSON.stringify(row)} in ${state.vw})`);
				}

				await mark('open the Project page again');
				await openPage('project', /^No project/);
				state = await controlsPages(page);
				expectPagesInPlace(state, 'Project page after removing the project');
				expectMenuFillsPage(state, 'Project page after removing the project');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
