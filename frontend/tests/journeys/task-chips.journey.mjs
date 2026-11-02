// Journey: an agent suggests work for a separate chat. A chip appears beside
// the composer of the main chat, and one click starts it without disturbing
// that chat. The fake AI's `tool-suggest-task*` scenarios make real SuggestTask
// calls through the real tool path, and the started chat really runs.
//
// Cases:
//   stream     a chip arrives while the reply streams; Start leaves the stream,
//              the scroll position, the draft and composer focus alone, and the
//              chip links to the new chat, which names where it came from
//   subagent   a background subagent suggests while the main chat is parked on
//              it, and the chip stays startable after the subagent has ended
//   nested     a subagent's subagent suggests; the chip is on the main chat
//   failed     a start that really fails shows the worker's reason, Retry works
//   click      a click that never reaches the worker says so, leaves the chip
//              actionable, and a double click starts one chat
//   crowded    three chips, long titles, the running-work strip and an open
//              question fit together, and nothing leaves the screen
//   survives   reload and switching keep each chat's own chips, with no frame
//              of the wrong chat's, and a chip dismissed while the socket was
//              down is gone after the reconnect and stays gone after a reload
//
// Hosted journeys build only the shared desktop UI and run it at desktop and
// phone widths. `--mobile-shell` opens the phone variants in the separately
// built production /mobile/ shell (npm run build, app.preview stamp,
// devloop up --no-vite); `survives` switches chats from the sidebar, which only
// the shared UI drives here.
import { runJourney } from './runner.mjs';
import {
	CHAT, PROJECT, api, chatIdByToken, chatState, composer, expect, openFromSidebar, openNewChat, pickAccess, scriptTag,
	scrollTranscriptTo, showSidebar, transcriptScroll, typeAndSend, uniq, waitChat, waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const inMobileShell = (variant) => mobileShell && variant.viewport === 'phone';

const DOCS = 'Check the docs heading';
const OTHER = 'Check on the safe model';
const LATE = 'Check after a pause';
const SLOW_END = 'When this last sentence arrives';

const tray = (page) => page.getByRole('region', { name: 'Suggested tasks' });
const row = (page, title) => tray(page).getByRole('listitem').filter({ hasText: title });
const startButton = (page, title) => page.getByRole('button', { name: `Start ${title}`, exact: true });
const retryButton = (page, title) => page.getByRole('button', { name: `Retry ${title}`, exact: true });
const dismissButton = (page, title) => page.getByRole('button', { name: `Dismiss ${title}`, exact: true });
const openChatLink = (page, title) => page.getByRole('link', { name: `Open chat: ${title}`, exact: true });
const composerText = (page) => composer(page).inputValue().catch(() => composer(page).innerText());

/** The chat's own flight-recorder tag, and the chat the tray's chips belong to. */
const trayState = (page) => page.evaluate((selector) => {
	const el = document.querySelector('[data-ui-state="task-chips"]');
	return {
		chips: el?.getAttribute('data-ui-chips') ?? null,
		chipSession: el?.getAttribute('data-ui-chip-session') ?? null,
		session: document.querySelector(selector)?.getAttribute('data-ui-session') ?? null
	};
}, CHAT);

async function waitChips(page, chips, timeout = 30_000) {
	await page.waitForFunction((want) => document.querySelector('[data-ui-state="task-chips"]')?.getAttribute('data-ui-chips') === want, chips, { timeout });
}

/**
 * Open a new chat, say hello, and (mobile shell) reopen it there. Returns its
 * token and id. `again` presses New session in the page rather than loading it,
 * which keeps the chat opened before it cached, and keeps the screen's project.
 */
async function openedChat(ctx, { first = 'hello', again = false } = {}) {
	const { page, status, variant } = ctx;
	if (again) {
		await showSidebar(page);
		await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New session', exact: true }).click();
		await waitChat(page, { surface: 'new' });
	} else {
		await openNewChat(ctx, { project: PROJECT });
	}
	// Bypass: SuggestTask runs from a code-mode cell, which prompts under Ask on
	// some hosts (see pickAccess); this journey judges the chip.
	await pickAccess(page, 'Bypass');
	const token = `chip-${uniq()}`;
	await typeAndSend(page, `${first} ${token}`);
	await waitTurnSettled(page);
	const id = await chatIdByToken(page, token);
	if (inMobileShell(variant)) {
		await page.goto(new URL(`/mobile/?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
		await page.locator(CHAT).waitFor({ timeout: 90_000 });
		await waitChat(page, { surface: 'active', loading: 'false' });
	}
	return { token, id };
}

const FULL_ACCESS = "Runs with this chat's access: full permissions";

/** Inside the scrolling list's visible part, not just rendered below its fold. */
async function inTrayView(locator) {
	return locator.evaluate((node) => {
		const line = node.getBoundingClientRect();
		const list = node.closest('ul').getBoundingClientRect();
		return line.height > 0 && line.top >= list.top && line.bottom <= list.bottom + 1;
	});
}

/** The reload ends its own step: chat → nothing → chat again is the reload, not a flicker. */
async function reload(page, mark) {
	await page.reload({ waitUntil: 'domcontentloaded' });
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
	await mark('page reloaded');
}

/** A chat that has already suggested `title`. */
async function chatWithChip(ctx, scenario, title, options) {
	const opened = await openedChat(ctx, options);
	await typeAndSend(ctx.page, `suggest ${scriptTag(ctx.variant, scenario)}`);
	await startButton(ctx.page, title).waitFor({ timeout: 60_000 });
	await waitChat(ctx.page, { streaming: 'false' });
	await ctx.page.waitForTimeout(800);
	return opened;
}

/**
 * Open the started chat by its link and see it run and name its source. Called
 * from `verify`, unrecorded: the first open of any chat not yet visited may flash
 * the loading placeholder (see switch-chats), whichever way it is opened.
 */
async function followLink(page, title) {
	const before = (await chatState(page)).session;
	await openChatLink(page, title).click();
	await page.locator('.started-from a').waitFor({ timeout: 30_000 });
	await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1, streaming: 'false' }, { timeout: 90_000 });
	expect((await chatState(page)).session !== before, 'the link opened another chat');
	expect(await tray(page).count() === 0, 'the started chat has no chips of its own');
}

/** Sample every painted frame for a tray showing chips of a chat other than the open one. */
const watchWrongChips = (page) => page.evaluate((selector) => {
	const seen = (window.__chipSeen = { frames: 0, wrong: [] });
	const sample = () => {
		seen.frames++;
		const tray = document.querySelector('[data-ui-state="task-chips"]');
		const session = document.querySelector(selector)?.getAttribute('data-ui-session');
		const owner = tray?.getAttribute('data-ui-chip-session');
		if (tray && session && owner !== session) seen.wrong.push({ owner, session, text: tray.textContent?.slice(0, 80) });
		seen.raf = requestAnimationFrame(sample);
	};
	seen.raf = requestAnimationFrame(sample);
}, CHAT);
const stopWatchingChips = (page) => page.evaluate(() => {
	cancelAnimationFrame(window.__chipSeen.raf);
	return window.__chipSeen;
});

// Drops the app's socket and holds reconnects off until let go, so a frame can
// be missed. Vite's HMR socket is left alone: closing it reloads the page.
const SOCKET_PROBE = () => {
	const probe = (window.__journeySocket = { sockets: [], blocked: false });
	const Real = window.WebSocket;
	window.WebSocket = class extends Real {
		constructor(url, protocols) {
			const hmr = [protocols].flat().includes('vite-hmr');
			super(probe.blocked && !hmr ? 'ws://127.0.0.1:9/' : url, protocols);
			if (!hmr) probe.sockets.push(this);
		}
	};
};

const STREAM = {
	name: 'stream',
	// A long first reply, so the transcript can be read scrolled up at any width.
	setup: async (ctx) => { await openedChat(ctx, { first: `tell me ${scriptTag(ctx.variant, 'long')}` }); },
	async act({ page, mark, log, variant }) {
		await mark('ask for something that suggests a task while the reply streams');
		await typeAndSend(page, `suggest ${scriptTag(variant, 'tool-suggest-task-slow')}`);
		await startButton(page, DOCS).waitFor({ timeout: 60_000 });
		expect((await trayState(page)).chips === 'pending', 'the new chip waits to be started');
		await waitChat(page, { streaming: 'true' });
		expect((await row(page, DOCS).innerText()).includes('Fake'), 'the chip names the model it will run on');

		await mark('type a draft and scroll up to read');
		const draft = `a draft ${uniq()}`;
		await composer(page).click();
		await composer(page).pressSequentially(draft);
		await scrollTranscriptTo(page, 0.2);

		await mark('press Start with a pointer');
		const url = page.url();
		const session = (await chatState(page)).session;
		const before = await transcriptScroll(page);
		await startButton(page, DOCS).click();
		await waitChips(page, 'started', 60_000);
		const after = await transcriptScroll(page);
		log(`scroll ${JSON.stringify(before)} → ${JSON.stringify(after)}`);

		expect(page.url() === url && (await chatState(page)).session === session, 'Start did not navigate');
		expect((await composerText(page)).includes(draft), 'Start left the draft alone');
		expect(await composer(page).evaluate((el) => el === document.activeElement || el.contains(document.activeElement)), 'Start left focus in the composer');
		expect(!before.atBottom, 'the reader was scrolled up');
		expect(Math.abs(after.top - before.top) <= 4, `Start left the reader where they were (${before.top} → ${after.top})`);
		await waitChat(page, { streaming: 'true' });
		await openChatLink(page, DOCS).waitFor();

		await mark('the reply finishes with nothing missing');
		await page.getByText(SLOW_END, { exact: false }).last().waitFor({ state: 'attached', timeout: 120_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
		expect((await composerText(page)).includes(draft), 'the draft outlived the reply');
		await page.waitForTimeout(1000);
	},
	verify: ({ page }) => followLink(page, DOCS)
};

const SUBAGENT = {
	name: 'subagent',
	allow: [{ kind: 'failed_requests', status: 409, url: /\/git\/fetch(\?|$)/, reason: 'the git panel refreshes when the turn ends and the server refuses to fetch while the subagent is still working; it has nothing to do with chips' }],
	setup: async (ctx) => { await openedChat(ctx); },
	async act({ page, mark, log, variant }) {
		await mark('delegate to a subagent that suggests while this chat waits');
		await typeAndSend(page, `delegate ${scriptTag(variant, 'tool-task-late-suggest-task')}`);
		await page.getByText('Waiting for the subagent.', { exact: false }).last().waitFor({ timeout: 60_000 });
		const before = await trayState(page);
		log(`before the chip: ${JSON.stringify(before)}`);
		expect(!before.chips, 'the chip was not there before the subagent suggested');

		await mark('the chip arrives on the main chat while it is parked');
		await startButton(page, LATE).waitFor({ timeout: 60_000 });
		expect((await row(page, LATE).innerText()).includes('From '), 'the chip says which agent raised it');

		await mark('start it after the subagent has ended');
		await page.getByText('Suggested it and carried on.', { exact: false }).last().waitFor({ state: 'attached', timeout: 60_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
		await page.waitForTimeout(1500);
		await startButton(page, LATE).click();
		await openChatLink(page, LATE).waitFor({ timeout: 60_000 });
		await waitChips(page, 'started');
	}
};

const NESTED = {
	name: 'nested',
	setup: async (ctx) => { await openedChat(ctx); },
	async act({ page, mark, variant }) {
		await mark('a subagent of a subagent suggests');
		await typeAndSend(page, `delegate twice ${scriptTag(variant, 'tool-task-nested-suggest-task')}`);
		await startButton(page, DOCS).waitFor({ timeout: 60_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
		await page.waitForTimeout(1500);
		expect((await row(page, DOCS).innerText()).includes('From '), 'the chip says which agent raised it');

		await mark('start it from the main chat');
		await startButton(page, DOCS).click();
		await openChatLink(page, DOCS).waitFor({ timeout: 60_000 });
	}
};

const FAILED = {
	name: 'failed',
	setup: async (ctx) => { ctx.chat = await chatWithChip(ctx, 'tool-suggest-task-other-model', OTHER); },
	async act({ page, mark, chat: { id } }) {
		// A real failure: a chip of an archived chat cannot start until it is restored.
		await api(page, 'PATCH', `/sessions/${id}/active`, { is_active: false, archived: true });

		await mark('press Start on a chip that cannot start');
		await startButton(page, OTHER).click();
		await retryButton(page, OTHER).waitFor({ timeout: 60_000 });
		await waitChips(page, 'failed');
		const reason = await row(page, OTHER).getByRole('alert').innerText();
		expect(/archived/i.test(reason), `the reason is in plain words (got "${reason}")`);
		await dismissButton(page, OTHER).waitFor();

		await mark('fix the cause, then press Retry');
		await api(page, 'PATCH', `/sessions/${id}/active`, { is_active: true, archived: false });
		await retryButton(page, OTHER).click();
		await openChatLink(page, OTHER).waitFor({ timeout: 60_000 });
		await waitChips(page, 'started');
		expect(await row(page, OTHER).getByRole('alert').count() === 0, 'the reason left with the failure');
	},
	verify: ({ page }) => followLink(page, OTHER)
};

const CLICK = {
	name: 'click',
	setup: async (ctx) => { await chatWithChip(ctx, 'tool-suggest-task', DOCS); },
	async act({ page, mark }) {
		const starts = [];
		const bodies = [];
		page.on('request', (request) => {
			if (request.method() === 'POST' && /\/task-chips\/[^/]+\/start$/.test(request.url())) {
				starts.push(request.url());
				bodies.push(request.postData());
			}
		});
		const route = /\/task-chips\/[^/]+\/start$/;

		// The owner reads what the new chat runs with before the click, without opening anything.
		const access = row(page, DOCS).getByText(FULL_ACCESS, { exact: true });
		expect(await access.isVisible(), 'the chip says what the new chat runs with');
		expect(await inTrayView(access), 'the access line shows without scrolling the tray');

		await mark('press Start with the app out of reach');
		await page.route(route, (r) => r.abort());
		await startButton(page, DOCS).click();
		await row(page, DOCS).getByRole('alert').getByText('Could not reach the app', { exact: false }).waitFor({ timeout: 15_000 });
		await waitChips(page, 'not-sent');
		await startButton(page, DOCS).waitFor();
		await page.unroute(route);

		await mark('reload');
		await reload(page, mark);
		await startButton(page, DOCS).waitFor({ timeout: 60_000 });
		await page.waitForTimeout(800);

		await mark('press Start with the worker offline');
		await page.route(route, (r) => r.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"offline"}' }));
		await startButton(page, DOCS).click();
		await row(page, DOCS).getByRole('alert').getByText('offline', { exact: false }).waitFor({ timeout: 15_000 });
		await waitChips(page, 'not-sent');
		await startButton(page, DOCS).waitFor();
		await page.unroute(route);

		await mark('double click Start');
		starts.length = 0;
		await startButton(page, DOCS).dblclick();
		await openChatLink(page, DOCS).waitFor({ timeout: 60_000 });
		await page.waitForTimeout(800);
		expect(starts.length === 1, `a double click sent one start (sent ${starts.length})`);
		expect(JSON.parse(bodies[0]).shown_access === 'bypassPermissions', `the start names the access on screen (sent ${bodies[0]})`);
		expect(await row(page, DOCS).getByText(FULL_ACCESS).count() === 0, 'a started chip has no access left to approve');
		expect(await row(page, DOCS).getByRole('alert').count() === 0, 'the old error left with the success');
	},
	// The two refused presses above are this case's own doing.
	allow: [
		{ kind: 'failed_requests', url: /\/task-chips\/[^/]+\/start$/, reason: 'this case fails these starts itself' },
		{ kind: 'errors', message: /503|ERR_FAILED/, reason: 'the browser logs the requests this case fails' }
	]
};

const CROWDED_FIRST = 'Work out why the settings page takes so long';

/** Three chips, a long unbroken title, the running-work strip and an open question: nothing may leave the screen. */
const CROWDED = {
	name: 'crowded',
	async setup(ctx) {
		await openedChat(ctx);
	},
	async act({ page, mark, variant }) {
		await typeAndSend(page, `crowd it ${scriptTag(variant, 'tool-suggest-task-crowded')}`);
		await mark('sent');
		// The mobile shell asks in its own dock.
		const questions = page.getByRole('region', { name: inMobileShell(variant) ? 'Waiting on you' : 'Questions for you' });
		await questions.waitFor({ timeout: 30_000 });
		await waitChips(page, 'pending,pending,pending,pending');
		await waitTurnSettled(page);
		await mark('all of it showing');

		expect(await tray(page).getByRole('listitem').count() === 3, 'three chips show before the rest fold away');
		await tray(page).getByRole('button', { name: 'and 1 more, 1 to start' }).waitFor({ timeout: 2_000 });

		const fit = await page.evaluate(() => {
			const box = (el) => el.getBoundingClientRect();
			const composer = document.querySelector('[data-ui-state="chat"] [aria-label="Message"]');
			return {
				sideways: document.documentElement.scrollWidth - innerWidth,
				leaving: [...document.querySelectorAll('[data-ui-state="task-chips"] *')].filter((el) => box(el).right > innerWidth + 0.5).length,
				trayBottom: box(document.querySelector('[data-ui-state="task-chips"]')).bottom,
				questionTop: box(document.querySelector('section[aria-label="Questions for you"], section[aria-label="Waiting on you"]')).top,
				composerTop: box(composer).top,
				composerBottom: box(composer).bottom,
				height: innerHeight
			};
		});
		expect(fit.sideways <= 0, `the page does not scroll sideways (${fit.sideways}px)`);
		expect(fit.leaving === 0, `nothing in the tray runs off the screen (${fit.leaving} elements)`);
		expect(fit.trayBottom <= fit.questionTop, 'the tray sits above the question, not over it');
		expect(fit.questionTop < fit.composerTop, 'the question sits above the composer');
		expect(fit.composerBottom <= fit.height, 'the composer stays on the screen');

		const first = row(page, CROWDED_FIRST).first();
		await first.getByRole('button', { name: /^Start / }).scrollIntoViewIfNeeded();
		expect(await first.getByRole('button', { name: /^Start / }).isVisible(), 'the first chip can still be started');
		expect(await inTrayView(first.getByText(FULL_ACCESS, { exact: true })), 'even with the tray squeezed, the first chip says what it runs with');
	},
	allow: [{ kind: 'layout_shift', reason: 'the open question lifts the composer, which is what this case sets up to measure' }]
};

const SURVIVES = {
	name: 'survives',
	viewports: mobileShell ? ['desktop'] : undefined,
	allow: [{ kind: 'errors', message: /WebSocket error/, reason: 'this case holds the socket down itself, so a reconnect attempt fails' }],
	async setup(ctx) {
		const { page } = ctx;
		await page.addInitScript(SOCKET_PROBE);
		const first = await chatWithChip(ctx, 'tool-suggest-task', DOCS);
		const second = await chatWithChip(ctx, 'tool-suggest-task-other-model', OTHER, { again: true });
		const rows = await api(page, 'GET', '/sessions?limit=50');
		const title = (id) => rows.find((r) => r.id === id)?.title;
		ctx.first = { ...first, title: title(first.id) };
		ctx.second = { ...second, title: title(second.id) };
	},
	async act({ page, mark, first, second }) {
		await watchWrongChips(page);
		for (const chat of [first, second, first, second]) {
			await mark(`switch to ${chat === first ? 'the first' : 'the second'} chat at once`);
			await openFromSidebar(page, chat.title);
			await page.waitForTimeout(60);
		}
		await startButton(page, OTHER).waitFor({ timeout: 30_000 });
		await page.waitForTimeout(600);
		const watched = await stopWatchingChips(page);
		expect(watched.frames > 5, 'the watch saw frames');
		expect(watched.wrong.length === 0, `no frame showed another chat's chips: ${JSON.stringify(watched.wrong.slice(0, 3))}`);
		expect(await startButton(page, DOCS).count() === 0, "the other chat's chip is gone");
		const model = await row(page, OTHER).innerText();
		expect(/safe/i.test(model) && /low/.test(model), `the chip names the other model and its effort (got "${model}")`);

		await mark('dismiss a chip');
		await dismissButton(page, OTHER).click();
		await tray(page).waitFor({ state: 'detached', timeout: 15_000 });
		await page.waitForTimeout(600);

		await mark('back to the first chat');
		await openFromSidebar(page, first.title);
		await startButton(page, DOCS).waitFor({ timeout: 30_000 });
		await page.waitForTimeout(600);

		await mark('reload');
		await reload(page, mark);
		await startButton(page, DOCS).waitFor({ timeout: 60_000 });
		await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1 }, { timeout: 60_000 });
		await page.waitForTimeout(1500);

		await mark('lose the connection; the chip is dismissed elsewhere meanwhile');
		await page.evaluate(() => {
			const probe = window.__journeySocket;
			probe.blocked = true;
			for (const socket of probe.sockets) socket.close();
		});
		const list = await api(page, 'GET', `/sessions/${first.id}/task-chips`);
		await api(page, 'POST', `/sessions/${first.id}/task-chips/${list.chips[0].id}/dismiss`);
		await page.waitForTimeout(1500);
		await startButton(page, DOCS).waitFor();

		await mark('the connection returns');
		await page.evaluate(() => { window.__journeySocket.blocked = false; });
		await tray(page).waitFor({ state: 'detached', timeout: 60_000 });
		await page.waitForTimeout(800);
	},
	async verify({ page, second }) {
		// Both dismissed chips stay gone. The first chat is open; the second has been dismissed from its own tray.
		await page.reload();
		await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1 }, { timeout: 60_000 });
		await page.waitForTimeout(1500);
		expect(await tray(page).count() === 0, 'a chip dismissed while disconnected stayed gone after a reload');
		await openFromSidebar(page, second.title);
		await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1 }, { timeout: 60_000 });
		await page.waitForTimeout(1500);
		expect(await tray(page).count() === 0, 'a chip dismissed with its button stayed gone after a reload');
	}
};

await runJourney({
	name: 'task-chips',
	allow: [...DEV_INSTANCE_NOISE],
	cases: [STREAM, SUBAGENT, NESTED, FAILED, CLICK, CROWDED, SURVIVES]
});
