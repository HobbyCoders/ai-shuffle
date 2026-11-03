// Journey: suggested-task chips are gone, and nothing an older worker still sends
// brings them back. Old data can stay in a database and an old peer can still
// relay it, so this case feeds the page what such a worker sends: list rows
// carrying `pending_task_chips`, pushed counts, and `task_chips_changed` frames
// both bare and wrapped for a watching device. The frames go through the app's
// own sockets; a title sent beside a count proves they were really handled.
//
// Cases:
//   old-signals    no tray, no row words, nothing in Needs you or the tab title,
//                  no call to the old endpoints, across a chat switch and a reload
//   real-question  a real AskUser still lifts its row and counts once in the title
//                  with old counts arriving beside it; answering moves it back
//   stream         a tool turn with old frames arriving mid-turn streams and folds
//                  as usual and never lands in Needs you
import { runJourney } from './runner.mjs';
import {
	CHAT, PROJECT, SEEDED, chatIdByToken, expect, gotoApp, openFromSidebar, openNewChat, pickAccess, scriptTag, settleServiceWorker, showSidebar, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const TITLE = 'AI Shuffle';
const OLD_ENDPOINT = /\/task-chips(\/|$|\?)/;
const LIST = /\/api\/v1\/sessions(\/cursor)?(\?|$)/;

/** Remembers the app's sockets so a case can hand them frames as if the server sent them. */
const SOCKET_PROBE = () => {
	const sockets = (window.__journeySockets = []);
	const Real = window.WebSocket;
	window.WebSocket = class extends Real {
		constructor(url, protocols) {
			super(url, protocols);
			// Vite's HMR socket is not the app's.
			if (![protocols].flat().includes('vite-hmr')) sockets.push(this);
		}
	};
};

/** Every list row says it holds chips, and every call to an old endpoint is recorded. */
async function oldWorker(page) {
	const called = [];
	page.on('request', (request) => {
		if (OLD_ENDPOINT.test(new URL(request.url()).pathname)) called.push(`${request.method()} ${request.url()}`);
	});
	await page.addInitScript(SOCKET_PROBE);
	await page.route(LIST, async (route) => {
		const response = await route.fetch();
		const body = await response.json();
		const rows = Array.isArray(body) ? body : body.items;
		for (const row of rows ?? []) row.pending_task_chips = 3;
		await route.fulfill({ response, json: body });
	});
	return called;
}

async function deliver(page, frames) {
	const sent = await page.evaluate((list) => {
		const open = window.__journeySockets.filter((socket) => socket.readyState === WebSocket.OPEN);
		for (const socket of open) {
			for (const frame of list) socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(frame) }));
		}
		return open.length;
	}, frames);
	expect(sent > 0, 'an open socket took the frames');
}

const oldFrames = (sessionId) => [
	{ type: 'task_chips_changed', session_id: sessionId },
	{ event_type: 'stream_frame', session_id: sessionId, data: { type: 'task_chips_changed', session_id: sessionId } },
	{ type: 'user_session_updated', session_id: sessionId, updates: { pending_task_chips: 4 } }
];

/**
 * The sidebar's lanes as text. A phone's closed drawer holds no rows, so it opens
 * for the read and closes again.
 */
async function lanes(page) {
	const sessions = page.getByRole('complementary', { name: 'Sessions' });
	const wasOpen = await sessions.isVisible();
	await showSidebar(page);
	await page.locator('[data-group]').first().waitFor({ timeout: 15_000 });
	const text = await page.evaluate(() => {
		const lane = (name) => document.querySelector(`[data-group="${name}"]`)?.textContent ?? '';
		return { needs: lane('needs-you'), active: lane('active'), all: document.querySelector('[data-group]')?.parentElement?.textContent ?? '' };
	});
	if (!wasOpen) {
		await page.locator('.sidebar-scrim').click({ position: { x: page.viewportSize().width - 8, y: 300 } });
		await sessions.waitFor({ state: 'hidden', timeout: 15_000 });
	}
	return text;
}

/** Nothing of the feature on screen: no tray, no row words, no old calls, an honest title. */
async function expectNoChips(page, called, { title = TITLE } = {}) {
	const { all } = await lanes(page);
	const seen = await page.evaluate(() => ({
		tray: document.querySelectorAll('[data-ui-state="task-chips"], [aria-label="Suggested tasks"]').length,
		title: document.title
	}));
	expect(seen.tray === 0, 'no suggested-task tray');
	expect(!/tasks? suggested/i.test(all), 'no row says a task was suggested');
	expect(seen.title === title, `the tab title reads "${title}" (got "${seen.title}")`);
	expect(called.length === 0, `no call to the old endpoints: ${called.join(', ')}`);
}

async function expectLaneEmpty(page) {
	const { needs } = await lanes(page);
	expect(needs === '', `nothing waits in Needs you (got "${needs.slice(0, 120)}")`);
}

const OLD_SIGNALS = {
	name: 'old-signals',
	setup: async (ctx) => {
		ctx.called = await oldWorker(ctx.page);
		await settleServiceWorker(ctx.page, ctx.status);
		await gotoApp(ctx.page, ctx.status);
		await waitChat(ctx.page, { surface: 'new' });
	},
	async act({ page, mark, status, called }) {
		const seeded = status.seed.chats;
		await mark('a list full of old counts');
		await expectNoChips(page, called);
		await expectLaneEmpty(page);

		await mark('open a chat with old counts on its row');
		await openFromSidebar(page, SEEDED.long);
		await waitChatOpen(page);
		await expectNoChips(page, called);

		await mark('old frames arrive');
		const renamed = `${SEEDED.no_project} ${uniq()}`;
		await deliver(page, [
			...oldFrames(seeded.long),
			...oldFrames(seeded.other_project),
			{ type: 'user_session_updated', session_id: seeded.no_project, updates: { pending_task_chips: 2, title: renamed } }
		]);
		// Past the list-reload debounce, so a reload they caused would have landed.
		await page.waitForTimeout(1200);
		expect((await lanes(page)).active.includes(renamed), 'a title beside a count still applies, so the frames were handled');
		await expectNoChips(page, called);
		await expectLaneEmpty(page);

		await mark('switch chats');
		await openFromSidebar(page, SEEDED.other_project);
		await waitChatOpen(page);
		await expectNoChips(page, called);

		await mark('reload');
		await page.reload({ waitUntil: 'domcontentloaded' });
		// A new step once the recorder has seen the old page go: the reload itself
		// is not the app going backwards; what boots next is judged.
		await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
		await mark('page reloaded');
		await page.locator(CHAT).waitFor({ timeout: 90_000 });
		await waitChatOpen(page);
		await expectNoChips(page, called);
		await expectLaneEmpty(page);
	}
};

const REAL_QUESTION = {
	name: 'real-question',
	setup: async (ctx) => {
		const { page, variant } = ctx;
		ctx.called = await oldWorker(page);
		await openNewChat(ctx, { project: PROJECT });
		// Bypass: the AskUser call runs from a code-mode cell, which prompts under
		// Ask on some hosts (see pickAccess); this journey judges the attention.
		await pickAccess(page, 'Bypass');
		ctx.token = `ask-${uniq()}`;
		await typeAndSend(page, `decide ${ctx.token} ${scriptTag(variant, 'ask-question')}`);
		await page.getByText("I've asked in the question tray.", { exact: false }).last().waitFor({ timeout: 90_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
		ctx.chatId = await chatIdByToken(page, ctx.token);
	},
	async act({ page, mark, called, token, chatId }) {
		const form = page.getByRole('form', { name: 'Answer questions' });

		await mark('the question waits while old counts arrive');
		await deliver(page, oldFrames(chatId));
		await page.waitForTimeout(1200);
		expect((await lanes(page)).needs.includes(token), 'the question lifts its row to Needs you');
		await expectNoChips(page, called, { title: `(1) ${TITLE}` });

		await mark('answer');
		await form.getByRole('radio', { name: /^Amber/ }).check();
		await form.getByRole('button', { name: 'Send answer', exact: true }).click();
		await waitTurnSettled(page, { text: 'Thanks. I received:' });
		await page.waitForFunction((title) => document.title === title, TITLE, { timeout: 15_000 });
		const after = await lanes(page);
		expect(!after.needs.includes(token), 'answering moves the row out of Needs you');
		expect(after.active.includes(token), 'the row is back in Active');
		await expectNoChips(page, called);
	}
};

const STREAM = {
	name: 'stream',
	setup: async (ctx) => {
		ctx.called = await oldWorker(ctx.page);
		await openNewChat(ctx, { project: PROJECT });
		await pickAccess(ctx.page, 'Bypass');
	},
	async act({ page, mark, variant, called }) {
		const token = `run-${uniq()}`;
		await mark('a tool turn with old frames arriving mid-turn');
		await typeAndSend(page, `run ${token} ${scriptTag(variant, 'tool-bash')}`);
		await waitChat(page, { surface: 'active', session: /^s:/ }, { timeout: 60_000 });
		const chatId = await chatIdByToken(page, token);
		await deliver(page, oldFrames(chatId));
		await waitTurnSettled(page);
		await page.getByRole('button', { name: /Ran 1 command/ }).first().waitFor({ timeout: 30_000 });
		expect(!(await lanes(page)).needs.includes(token), 'a turn that asks nothing stays out of Needs you');
		await expectNoChips(page, called);
	}
};

await runJourney({
	name: 'retired-task-chips',
	allow: [...DEV_INSTANCE_NOISE],
	cases: [OLD_SIGNALS, REAL_QUESTION, STREAM]
});
