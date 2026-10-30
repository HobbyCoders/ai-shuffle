// Journey: the agent asks a question (AskUser, the tool the native engine
// uses for the question tray) and the user answers or skips it. The fake AI's
// `[script:ask-question]` asks one question and `[script:ask-questions]` two in
// one request; the answer the app queues for the agent wakes the scenario,
// whose reply quotes exactly what it received ("User skipped this question."
// for a skip). Every case ends with the tray gone and the turn settled.
//
// Cases:
//   answer         one question: pick an option and send it
//   skip           one question: Skip sends at once and closes the tray
//   skip-two       two questions: Skip moves to the next open one, undo
//                  works, and skipping the last open one sends the batch
//   skip-offline   while the socket is down Skip is disabled and sends
//                  nothing; once it is back Skip sends
//
// Hosted journeys build only the shared desktop UI and run it at desktop and
// phone widths. `--mobile-shell` opens the phone variants in the separately
// built production /mobile/ shell (npm run build, app.preview stamp,
// devloop up --no-vite). There one question with options is answered by a
// tap and has no Skip of its own, so `skip` and `skip-offline` run on the
// desktop UI only and `answer` taps the option.
import { runJourney } from './runner.mjs';
import { PROJECT, CHAT, chatIdByToken, expect, openNewChat, pickAccess, scriptTag, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const inMobileShell = (variant) => mobileShell && variant.viewport === 'phone';

const COLOUR = 'Which colour should the banner use?';
const POSITION = 'Where should the banner go?';
const SKIPPED = 'User skipped this question.';
const REPLY = 'Thanks. I received:';

// Counts the user_question_response frames the page sends, and can drop the
// socket and hold reconnects off (the app's own retry then fails until let go).
const SOCKET_PROBE = () => {
	const probe = (window.__journeySocket = { sockets: [], blocked: false, responses: 0 });
	const Real = window.WebSocket;
	class Probed extends Real {
		constructor(url, protocols) {
			// Leave Vite's HMR socket alone: closing it makes the dev client reload the page.
			const hmr = [protocols].flat().includes('vite-hmr');
			super(probe.blocked && !hmr ? 'ws://127.0.0.1:9/' : url, protocols);
			if (!hmr) probe.sockets.push(this);
		}
		send(data) {
			if (typeof data === 'string' && data.includes('"user_question_response"')) probe.responses++;
			return super.send(data);
		}
	}
	window.WebSocket = Probed;
};
const responsesSent = (page) => page.evaluate(() => window.__journeySocket?.responses ?? 0);

const form = (page) => page.getByRole('form', { name: 'Answer questions' });
const tray = (page) => page.getByRole('region', { name: 'Questions for you' });
const skipButton = (page) => form(page).getByRole('button', { name: /^(Skip this question|Skipped — undo)$/ });
const heading = (page) => form(page).getByRole('heading', { level: 3 });

/** Ask through the scenario on the desktop UI; then (mobile shell) reopen the chat there. */
async function ask(ctx, scenario) {
	const { page, status, variant } = ctx;
	await openNewChat(ctx, { project: PROJECT });
	// Bypass: the AskUser call runs from a code-mode cell, which prompts under
	// Ask on some hosts (see pickAccess); this journey judges the tray.
	await pickAccess(page, 'Bypass');
	const token = `ask-${uniq()}`;
	await typeAndSend(page, `decide ${token} ${scriptTag(variant, scenario)}`);
	await page.getByText("I've asked in the question tray.", { exact: false }).last().waitFor({ timeout: 90_000 });
	await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
	if (inMobileShell(variant)) {
		const id = await chatIdByToken(page, token);
		await page.goto(new URL(`/mobile/?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
		await page.locator(CHAT).waitFor({ timeout: 90_000 });
		await page.getByText(COLOUR, { exact: true }).first().waitFor({ timeout: 60_000 });
	} else {
		await tray(page).waitFor({ timeout: 30_000 });
		await heading(page).getByText(COLOUR, { exact: true }).waitFor();
	}
	await page.waitForTimeout(800);
	return token;
}

/** The agent's final reply: the transcript row that carries REPLY. */
async function finalReply(page) {
	await waitTurnSettled(page, { text: REPLY, messagesAtLeast: 4 });
	return page.evaluate((needle) => {
		const rows = [...document.querySelectorAll('[data-ui-row]')].filter((el) => (el.textContent || '').includes(needle));
		return rows.length ? rows[rows.length - 1].innerText : '';
	}, REPLY);
}

/** The tray (and any answer form) is gone and stays gone. */
async function expectTrayClosed(page) {
	await form(page).waitFor({ state: 'detached', timeout: 15_000 });
	await page.waitForTimeout(600);
	expect((await form(page).count()) === 0 && (await tray(page).count()) === 0, 'the question tray closed');
}

const answerCase = {
	name: 'answer',
	setup: (ctx) => ask(ctx, 'ask-question'),
	async act({ page, mark, variant, log }) {
		await mark('pick an option and send');
		if (inMobileShell(variant)) {
			// One single-choice question: the mobile dock answers with a tap.
			await page.getByRole('button', { name: /^Amber/ }).click();
			await page.getByRole('button', { name: /^Amber/ }).waitFor({ state: 'detached', timeout: 15_000 });
		} else {
			await form(page).getByRole('radio', { name: /^Amber/ }).check();
			await form(page).getByRole('button', { name: 'Send answer', exact: true }).click();
		}
		await expectTrayClosed(page);
		const reply = await finalReply(page);
		log(`reply: ${JSON.stringify(reply.slice(0, 200))}`);
		expect(reply.includes(COLOUR) && reply.includes('Amber'), `the agent received the answer (${reply})`);
		expect(!reply.includes(SKIPPED), 'an answered question is not reported as skipped');
	}
};

const skipCase = {
	name: 'skip',
	setup: (ctx) => ask(ctx, 'ask-question'),
	async act({ page, mark, log }) {
		expect((await responsesSent(page)) === 0, 'nothing answered before the skip');
		await mark('skip the only question');
		await skipButton(page).click();
		await expectTrayClosed(page);
		const reply = await finalReply(page);
		log(`reply: ${JSON.stringify(reply.slice(0, 200))}`);
		expect(reply.includes(`${COLOUR}\n${SKIPPED}`), `the agent received the skip (${reply})`);
		expect((await responsesSent(page)) === 1, 'one skip sends one response');
	}
};

const skipTwoCase = {
	name: 'skip-two',
	setup: (ctx) => ask(ctx, 'ask-questions'),
	async act({ page, mark, log }) {
		await mark('skip the first question');
		await skipButton(page).click();
		await heading(page).getByText(POSITION, { exact: true }).waitFor({ timeout: 5_000 });
		expect((await responsesSent(page)) === 0, 'skipping one of two open questions sends nothing yet');
		await page.waitForTimeout(400);

		await mark('go back and undo the skip');
		await form(page).getByRole('button', { name: /^Colour/ }).click();
		await heading(page).getByText(COLOUR, { exact: true }).waitFor();
		const undo = form(page).getByRole('button', { name: 'Skipped — undo', exact: true });
		expect((await undo.getAttribute('aria-pressed')) === 'true', 'the skipped question shows its undo');
		await undo.click();
		await form(page).getByRole('button', { name: 'Skip this question', exact: true }).waitFor();
		await form(page).getByRole('radio', { name: /^Teal/ }).check();
		await page.waitForTimeout(400);

		await mark('skip the last open question');
		await form(page).getByRole('button', { name: /^Position/ }).click();
		await heading(page).getByText(POSITION, { exact: true }).waitFor();
		await skipButton(page).click();
		await expectTrayClosed(page);
		const reply = await finalReply(page);
		log(`reply: ${JSON.stringify(reply.slice(0, 300))}`);
		expect(reply.includes(`${COLOUR}\nTeal`), `the undone question carries its answer (${reply})`);
		expect(reply.includes(`${POSITION}\n${SKIPPED}`), `the last question was sent as skipped (${reply})`);
		expect((await responsesSent(page)) === 1, 'the batch is sent once');
	}
};

const skipOfflineCase = {
	name: 'skip-offline',
	viewports: mobileShell ? ['desktop'] : undefined,
	setup: (ctx) => ask(ctx, 'ask-question'),
	async act({ page, mark, log }) {
		await mark('lose the connection');
		await page.evaluate(() => {
			const probe = window.__journeySocket;
			probe.blocked = true;
			for (const socket of probe.sockets) socket.close();
		});
		await tray(page).getByText('Reconnect to answer', { exact: true }).waitFor({ timeout: 15_000 });
		expect(await skipButton(page).isDisabled(), 'Skip is disabled while disconnected');
		// A press on the disabled control (as a hurried tap would) sends nothing.
		await skipButton(page).click({ force: true });
		await page.waitForTimeout(800);
		expect((await responsesSent(page)) === 0, 'no answer left the page while disconnected');
		expect((await form(page).count()) === 1, 'the question stays open while disconnected');

		await mark('reconnect and skip');
		await page.evaluate(() => { window.__journeySocket.blocked = false; });
		await page.getByRole('button', { name: 'Reconnect', exact: true }).click().catch(() => {});
		await tray(page).getByText('Answer to continue', { exact: true }).waitFor({ timeout: 30_000 });
		await skipButton(page).click();
		await expectTrayClosed(page);
		const reply = await finalReply(page);
		log(`reply: ${JSON.stringify(reply.slice(0, 200))}`);
		expect(reply.includes(`${COLOUR}\n${SKIPPED}`), `the skip after reconnecting reached the agent (${reply})`);
		expect((await responsesSent(page)) === 1, 'exactly one response, sent after reconnecting');
	}
};

const withProbe = (testCase) => ({
	...testCase,
	async setup(ctx) {
		await ctx.page.addInitScript(SOCKET_PROBE);
		return testCase.setup(ctx);
	}
});

await runJourney({
	name: 'question-tray',
	cases: [
		withProbe(answerCase),
		withProbe({ ...skipCase, viewports: mobileShell ? ['desktop'] : undefined }),
		withProbe(skipTwoCase),
		withProbe(skipOfflineCase)
	],
	allow: [...DEV_INSTANCE_NOISE]
});
