// Journey: Fast mode on a Claude Code chat (issue 991). The dev instance's fake
// `claude` handles Fast the way Claude Code 2.1.316 does: `apply_flag_settings`
// succeeds at once, the running turn keeps the speed it started with, and each
// request reports `usage.speed`; the result reports `fast_mode_state` and
// `fast_mode_disabled_reason`.
//
// What the chat must show: a toggle during a running turn says, at the toggle,
// that it applies from the next message; a toggle between turns says nothing;
// the turn says how it really ran, and that survives a reload; an ordinary
// turn with Fast off shows nothing about speed.
//
// The other lane (GPT on the Responses API switches inside a running turn and
// gets no notice) has no fake here: tests/core/test_fast_outcome.py.
//
// Cases:
//   midturn   Fast off, turn running, toggle on; the next turn runs Fast
//   idle      toggling between turns says nothing; Fast off shows no speed line
//   refused   Fast on, the CLI refuses it; a known and an unknown reason
//   reload    the notice is rebuilt from the worker after a reload mid-turn, both ways
//   retried   a safety-check retry runs standard after a Fast request: one note, partly Fast
import { runJourney } from './runner.mjs';
import {
	PROJECT,
	chatState,
	expect,
	gotoApp,
	openPopup,
	pick,
	pickAccess,
	pickProject,
	scriptTag,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CLAUDE_AGENT = 'Dev (Claude Code)';
const FIRST_REQUEST = 'First request of this turn';
const LAST_REQUEST = 'All three requests are finished.';
const WILL_TURN = (state) => `Fast mode turns ${state} with your next message. This turn finishes at its current speed.`;

const speedNote = (page) => page.locator('[data-testid="fast-outcome-note"]');
const notice = (page) => page.locator('[data-testid="fast-applies-notice"]');
// Fast lives in the effort popover at desktop width and in Chat controls on a phone.
const effortButton = (page) => page.getByRole('button', { name: /^(Effort:|All chat controls)/ }).first();
const fastToggle = (page) =>
	page.getByRole('button', { name: 'Fast mode', exact: true }).or(page.getByRole('switch', { name: 'Fast mode', exact: true }));
const fastState = (on) =>
	`button[aria-label="Fast mode"][aria-pressed="${on}"], button[aria-label="Fast mode"][aria-checked="${on}"]`;

/** Fresh Claude Code chat on Opus, the model that offers Fast, Dev sandbox, Bypass. */
async function openClaudeOpusChat({ page, status }) {
	if (!status.seed?.claude_cli?.agent_id) {
		throw new Error(`the dev instance has no Claude Code agent (seed: ${JSON.stringify(status.seed?.claude_cli)})`);
	}
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New session' });
	await hero.waitFor();
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: CLAUDE_AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${CLAUDE_AGENT}` })
	);
	await pickProject(page, hero, PROJECT);
	await pickAccess(page, 'Bypass');
	const models = page.getByRole('dialog', { name: 'Choose model' });
	await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), models);
	await models.getByRole('textbox', { name: 'Search models' }).fill('Opus');
	await models.getByRole('option', { name: /^Opus/ }).first().click();
	await page.keyboard.press('Escape');
	await models.waitFor({ state: 'hidden', timeout: 5000 });
	await page.getByRole('button', { name: /^Model: .*Opus/ }).first().waitFor();
	await page.waitForTimeout(500);
}

/** Open the effort popover, press Fast, and leave the popover open for reading. */
async function toggleFast(page, wantOn) {
	await openPopup(effortButton(page), fastToggle(page));
	await fastToggle(page).click();
	await page.locator(fastState(wantOn)).waitFor({ timeout: 10_000 });
}

async function closePopover(page) {
	await page.keyboard.press('Escape');
	await fastToggle(page).waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
}

const noteText = async (page) => (await speedNote(page).allInnerTexts()).map((t) => t.replace(/\s+/g, ' ').trim());

const midturn = {
	name: 'midturn',
	setup: openClaudeOpusChat,
	async act({ page, mark, variant, log }) {
		await mark('send a turn with Fast off');
		await typeAndSend(page, `fast check ${uniq()} ${scriptTag(variant, 'fast-multi', { tps: 5 })}`);
		await page.getByText(FIRST_REQUEST).first().waitFor({ timeout: 60_000 });

		await mark('turn Fast on during the running turn');
		await toggleFast(page, true);
		await notice(page).waitFor({ timeout: 10_000 });
		const said = (await notice(page).innerText()).trim();
		log(`toggle notice: ${said}`);
		expect(said === WILL_TURN('on'), `the toggle says when the setting changes ("${said}")`);
		await closePopover(page);

		await mark('the turn ends at standard speed');
		await page.getByText(LAST_REQUEST).first().waitFor({ timeout: 90_000 });
		await waitTurnSettled(page, { text: LAST_REQUEST });
		let notes = await noteText(page);
		log(`after the toggled turn: ${JSON.stringify(notes)}`);
		expect(notes.length === 1, `one speed note on the toggled turn (${notes.length})`);
		expect(/standard speed/i.test(notes[0]), `the toggled turn says it ran at standard speed: "${notes[0]}"`);
		expect(
			notes[0].endsWith('Fast mode was turned on during this turn, so it applies from your next message.'),
			`and why: "${notes[0]}"`
		);

		await mark('the next message runs Fast');
		await typeAndSend(page, `again ${uniq()} ${scriptTag(variant, 'default')}`);
		await page.waitForFunction(() => document.querySelectorAll('[data-testid="fast-outcome-note"]').length === 2, null, { timeout: 60_000 });
		await waitTurnSettled(page, { messagesAtLeast: 4 });
		notes = await noteText(page);
		log(`after the next turn: ${JSON.stringify(notes)}`);
		expect(/^Ran on Fast/.test(notes[1]), `the next turn ran Fast: "${notes[1]}"`);
		await openPopup(effortButton(page), fastToggle(page));
		expect((await notice(page).count()) === 0, 'the notice is gone once the setting has taken effect');
		await closePopover(page);

		await mark('reload');
		await page.reload({ waitUntil: 'domcontentloaded' });
		// A new step once the recorder has seen the old page go: the reload
		// itself is not the app going backwards.
		await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
		await mark('page reloaded: both notes are still there');
		await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/ }, { timeout: 60_000 });
		await speedNote(page).nth(1).waitFor({ timeout: 60_000 });
		const reloaded = await noteText(page);
		expect(JSON.stringify(reloaded) === JSON.stringify(notes), `the notes survive a reload: ${JSON.stringify(reloaded)}`);
	}
};

const idle = {
	name: 'idle',
	setup: openClaudeOpusChat,
	async act({ page, mark, variant }) {
		await mark('an ordinary turn with Fast off');
		await typeAndSend(page, `plain ${uniq()} ${scriptTag(variant, 'default')}`);
		await waitTurnSettled(page, {});
		expect((await speedNote(page).count()) === 0, 'an ordinary turn with Fast off shows no speed line');

		await mark('toggle Fast between turns');
		await toggleFast(page, true);
		await page.waitForTimeout(800);
		expect((await notice(page).count()) === 0, 'a toggle between turns says nothing about timing');
		await closePopover(page);

		await mark('the next turn runs Fast');
		await typeAndSend(page, `quick ${uniq()} ${scriptTag(variant, 'default')}`);
		await speedNote(page).first().waitFor({ timeout: 60_000 });
		await waitTurnSettled(page, { messagesAtLeast: 4 });
		const notes = await noteText(page);
		expect(notes.length === 1 && /^Ran on Fast/.test(notes[0]), `a Fast turn says so, once: ${JSON.stringify(notes)}`);
	}
};

const refused = {
	name: 'refused',
	setup: openClaudeOpusChat,
	async act({ page, mark, variant, log }) {
		await mark('turn Fast on before sending');
		await toggleFast(page, true);
		expect((await notice(page).count()) === 0, 'a toggle before the first message says nothing about timing');
		await closePopover(page);

		await mark('Claude refuses Fast: usage credits');
		await typeAndSend(page, `credits ${uniq()} ${scriptTag(variant, 'default', { fast_refused: 'extra_usage_disabled' })}`);
		await speedNote(page).first().waitFor({ timeout: 60_000 });
		await waitTurnSettled(page, {});
		let notes = await noteText(page);
		log(`refused: ${JSON.stringify(notes)}`);
		expect(/standard speed/i.test(notes[0]) && /usage credits/i.test(notes[0]), `the reason in plain words: "${notes[0]}"`);

		await mark('Claude refuses Fast: a reason this app has never seen');
		await typeAndSend(page, `novel ${uniq()} ${scriptTag(variant, 'default', { fast_refused: 'brand_new_reason' })}`);
		await speedNote(page).nth(1).waitFor({ timeout: 60_000 });
		await waitTurnSettled(page, { messagesAtLeast: 4 });
		notes = await noteText(page);
		log(`unknown: ${JSON.stringify(notes)}`);
		expect(notes[1].includes('brand_new_reason'), `an unknown reason shows verbatim: "${notes[1]}"`);
	}
};

/** Reload a chat whose turn is still running, then read the toggle again. */
async function reloadMidTurn(page, mark, wantOn) {
	await mark('reload while the turn runs');
	await page.reload({ waitUntil: 'domcontentloaded' });
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
	await mark('page reloaded: the notice comes back from the worker');
	await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/ }, { timeout: 60_000 });
	await openPopup(effortButton(page), fastToggle(page));
	await page.locator(fastState(wantOn)).waitFor({ timeout: 10_000 });
	await notice(page).waitFor({ timeout: 15_000 });
	const said = (await notice(page).innerText()).trim();
	expect(said === WILL_TURN(wantOn ? 'on' : 'off'), `the notice survives the reload ("${said}")`);
	await closePopover(page);
}

const reloadCase = {
	name: 'reload',
	setup: openClaudeOpusChat,
	async act({ page, mark, variant, log }) {
		await mark('Fast off: start a long turn and turn Fast on');
		await typeAndSend(page, `long ${uniq()} ${scriptTag(variant, 'fast-multi', { tps: 2 })}`);
		await page.getByText(FIRST_REQUEST).first().waitFor({ timeout: 60_000 });
		await toggleFast(page, true);
		await notice(page).waitFor({ timeout: 10_000 });
		await closePopover(page);
		await reloadMidTurn(page, mark, true);

		await mark('the notice ends with the turn it was about');
		await waitTurnSettled(page, { text: LAST_REQUEST, timeout: 180_000 });
		await openPopup(effortButton(page), fastToggle(page));
		expect((await notice(page).count()) === 0, 'no notice once the turn has ended');
		await closePopover(page);

		await mark('Fast on: start a long turn and turn Fast off');
		const before = Number((await chatState(page)).messages);
		await typeAndSend(page, `long again ${uniq()} ${scriptTag(variant, 'fast-multi', { tps: 2 })}`);
		// The finished turn has folded its first request away, so wait on the
		// new turn's own message rather than a second copy of that text.
		await waitChat(page, { streaming: 'true', messagesAtLeast: before + 2 }, { timeout: 60_000 });
		await toggleFast(page, false);
		await notice(page).waitFor({ timeout: 10_000 });
		const said = (await notice(page).innerText()).trim();
		log(`off notice: ${said}`);
		expect(said === WILL_TURN('off'), `the off notice claims no speed ("${said}")`);
		await closePopover(page);
		await reloadMidTurn(page, mark, false);
	}
};

const retried = {
	name: 'retried',
	// Staging, not judged: the declined request's API-error text is painted for
	// one frame before the safety-check card replaces it. That flash belongs to
	// the safety-check flow, and this case is about the speed note after it.
	async setup({ page, status, variant }) {
		await openClaudeOpusChat({ page, status });
		await toggleFast(page, true);
		await closePopover(page);
		await typeAndSend(page, `audit ${uniq()} ${scriptTag(variant, 'default', { refuse_first: 'cyber' })}`);
		await page.locator('[data-testid="guardrail-card"]').waitFor({ timeout: 60_000 });
		// The card ignores clicks for its first 300ms so a stray tap can't answer
		// it. Its screen-reader announcement comes after that, so wait for it: a
		// click sent sooner is dropped and the turn waits forever.
		await page
			.getByRole('status')
			.filter({ hasText: /^Press Enter to / })
			.waitFor({ state: 'attached', timeout: 10_000 });
	},
	async act({ page, mark, log }) {
		expect((await speedNote(page).count()) === 0, 'no speed note while the turn waits on the decision');

		await mark('continue: the retry runs standard, in the same turn');
		await page.getByRole('button', { name: /^Continue/ }).first().click();
		await speedNote(page).first().waitFor({ timeout: 60_000 });
		await waitTurnSettled(page, {});
		const notes = await noteText(page);
		log(`retried: ${JSON.stringify(notes)}`);
		expect(notes.length === 1, `one note for one turn: ${JSON.stringify(notes)}`);
		expect(
			notes[0].startsWith('Ran on Fast for 1 of 2 requests; the other 1 ran at standard speed.'),
			`the note counts both attempts: "${notes[0]}"`
		);
	}
};

await runJourney({
	name: 'fast-midturn',
	cases: [midturn, idle, refused, reloadCase, retried],
	allow: [...DEV_INSTANCE_NOISE]
});
