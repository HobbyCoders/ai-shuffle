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
	waitTurnSettled
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
		const round = ['Send', 'Attach files', 'Start voice input'].map((name) => {
			const el = region.querySelector(`button[aria-label="${name}"]`);
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
	{ trigger: /^Permission mode: /, sheet: (page) => page.getByRole('menu', { name: 'Tool access' }) },
	{ trigger: /^Effort: /, sheet: (page) => page.getByRole('dialog', { name: 'Reasoning effort' }) },
	{ trigger: /^Context and usage/, sheet: (page) => page.getByRole('dialog', { name: 'Context and usage' }) }
];

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
				for (const [index, { trigger, sheet }] of SHEETS.entries()) {
					const label = String(trigger).replace(/[/^:]/g, '').trim();
					await mark(`open ${label}`);
					await page.getByRole('region', { name: INPUT }).getByRole('button', { name: trigger }).first().tap();
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
						await page.getByRole('log', { name: 'Conversation messages' }).tap({ position: { x: 20, y: 20 } });
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
				const hero = page.getByRole('region', { name: 'New session' });
				await pickAgent(page, hero);
				await sendButton(page).tap();
				await waitTurnSettled(page, { text: `You said: “no agent yet ${token}`, messagesAtLeast: 2 });
				expect((await countUserRows(page, token)) === 1, 'the message shows once');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
