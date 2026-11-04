// Journey: the sidebar says what is true of each chat, whatever else is going on.
//
// A change the user makes to one row is shown at once and undone if the server
// refuses it. Undoing it must touch that change alone: a row another device
// settled while the request was out stays settled (#1034). A chat whose last
// turn ended in an error says so instead of Done, opened or not, on any engine
// (#1032). A project is one entry in the Filter however its chats were made
// (#948).
//
// Cases:
//   rollback-settle   Settle fails: only that row goes back; another device's
//                     settle of a second chat and rename of the first both stay
//   rollback-archive  the same for Archive
//   rollback-rename   the same for Rename
//   rollback-delete   the same for Delete: the row returns where it was
//   failed-open       a provider error on the open chat: the row says Failed,
//                     the lane says it needs you, a reload agrees, and a good
//                     turn clears it
//   failed-unopened   the same on a chat that is not open when the error lands
//   failed-claude     the delegated Claude engine: its CLI reports the error as
//                     an ordinary end of turn
//   failed-settled    settling a failed chat keeps Failed in the Settled lane
//   failed-cleared    clearing a failed chat takes the failure with it, here
//                     and after a reload
//   stopped           the user's own Stop is not a failure
//   project-choice-survives  choosing a project and a branch holds while it gains a remote (#1078)
//   project-once      a project is listed once after its first worktree chat
//
// Hosted journeys build only the shared desktop UI and run it at desktop and
// phone widths. `--mobile-shell` opens the phone variants of the failed-* cases
// in the separately built production /mobile/ shell (npm run build, app.preview
// stamp, devloop up --no-vite).
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { runJourney } from './runner.mjs';
import {
	api, chatIdByToken, chatState, expect, openNewChat, pick, pickAccess, pickProject, scriptTag, showSidebar as showDesktopSidebar, stopButton, typeAndSend, uniq,
	waitChat, waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CLAUDE_AGENT = 'Dev (Claude Code)';
const mobileShell = process.argv.includes('--mobile-shell');
const inMobileShell = (variant) => mobileShell && variant.viewport === 'phone';

const CARD = (id) => `[data-session-card="${id}"]`;

/** The list of chats, open: the shared UI's sidebar, or the /mobile/ shell's menu holding the same one. */
async function showSidebar(page) {
	if (!new URL(page.url()).pathname.startsWith('/mobile/')) return showDesktopSidebar(page);
	const sessions = page.getByRole('complementary', { name: 'Chats' });
	if (await sessions.isVisible()) return sessions;
	await page.getByRole('button', { name: /^Open menu/ }).click();
	await sessions.waitFor({ timeout: 10_000 });
	return sessions;
}
const laneOf = (page, id) => page.evaluate((selector) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') ?? null, CARD(id));

async function waitLane(page, id, lane, timeout = 30_000) {
	await page.getByRole('complementary', { name: 'Chats' })
		.or(page.getByRole('button', { name: /^(Show sidebar|Chats|Open menu)/ }))
		.first().waitFor({ timeout: 60_000 });
	await showSidebar(page);
	try {
		await page.waitForFunction(({ selector, lane }) => document.querySelector(selector)?.closest('[data-group]')?.getAttribute('data-group') === lane,
			{ selector: CARD(id), lane }, { timeout });
	} catch {
		throw new Error(`the row never reached the ${lane} lane; it is in ${await laneOf(page, id)}`);
	}
}

/** The row's status word as the user reads it. */
// A quiet row shows no status word; its screen-reader details always start with it.
const statusWord = async (page, id) => (await page.locator(`${CARD(id)} .details`).first().textContent())?.split(' · ')[0].trim();

async function waitStatus(page, id, word, timeout = 60_000) {
	await showSidebar(page);
	try {
		await page.waitForFunction(({ selector, word }) => document.querySelector(`${selector} .details`)?.textContent?.split(' · ')[0].trim() === word,
			{ selector: CARD(id), word }, { timeout });
	} catch {
		throw new Error(`the row never said "${word}"; it says "${await statusWord(page, id).catch(() => '(no row)')}"`);
	}
}

/**
 * Hold one mutation inside the page, the way a slow server would, and answer it
 * with a refusal when the case says. A page-side wrapper rather than page.route,
 * because the built app's service worker makes the requests page.route would see.
 */
async function installHold(page) {
	await page.addInitScript(() => {
		const hold = (window.__journeyHold = { armed: null, held: 0, answer: null });
		const original = window.fetch.bind(window);
		window.fetch = async (input, init) => {
			const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
			let path = '';
			try {
				path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
			} catch {
				/* not a URL */
			}
			if (hold.armed && hold.armed.method === method && path === hold.armed.path) {
				hold.armed = null;
				hold.held += 1;
				await new Promise((resolve) => (hold.answer = resolve));
				return new Response(JSON.stringify({ detail: 'Journey: the server refused this change' }), {
					status: 503,
					headers: { 'content-type': 'application/json' }
				});
			}
			return original(input, init);
		};
	});
}

const armHold = (page, method, path) => page.evaluate(({ method, path }) => { window.__journeyHold.armed = { method, path: `/api/v1${path}` }; }, { method, path });
const waitHeld = (page) => page.waitForFunction(() => window.__journeyHold.held > 0, null, { timeout: 15_000 });
const refuseHeld = (page) => page.evaluate(() => window.__journeyHold.answer());

/**
 * Another device changes a chat: a request from outside this page, so the page
 * only learns of it the way it learns of any other device's change, by its push.
 */
async function otherDevice(page, status, method, path, data) {
	const origin = new URL(status.urls.ui).origin;
	const response = await page.request.fetch(`${origin}/api/v1${path}`, { method, data, headers: { origin, 'sec-fetch-site': 'same-origin' } });
	if (!response.ok()) throw new Error(`other device: ${method} ${path} → HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`);
}

/** Watch every painted frame for the row leaving `lane`. Returns a function that stops and gives the first departure. */
async function watchStaysInLane(page, id, lane) {
	await page.evaluate(({ selector, lane }) => {
		const state = (window.__journeyLaneWatch = { running: true, left: null });
		const started = performance.now();
		const tick = () => {
			if (!state.running) return;
			const card = document.querySelector(selector);
			// An unmounted row (a closed phone drawer) says nothing either way.
			const now = card ? card.closest('[data-group]')?.getAttribute('data-group') ?? null : lane;
			if (now !== lane && !state.left) state.left = { to: now, at: Math.round(performance.now() - started) };
			requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}, { selector: CARD(id), lane });
	return () => page.evaluate(() => {
		window.__journeyLaneWatch.running = false;
		return window.__journeyLaneWatch.left;
	});
}

/** A real chat with one finished turn, on the given agent, staged from the new-session screen. */
async function makeChat(ctx, label, { agent = null, first = 'hello', again = false } = {}) {
	const { page } = ctx;
	if (again) await backToNew(page);
	else await openNewChat(ctx);
	if (agent) {
		const hero = page.getByRole('region', { name: 'New chat' });
		const menu = page.getByRole('menu', { name: 'Agent selection' });
		await pick(hero.getByRole('button', { name: /^Agent:/ }), menu, menu.getByRole('button', { name: agent, exact: true }), hero.getByRole('button', { name: `Agent: ${agent}` }));
	}
	await pickAccess(page, 'Bypass');
	const token = `${label}-${uniq()}`;
	await typeAndSend(page, `${first} ${token}`);
	await waitTurnSettled(page);
	const id = await chatIdByToken(page, token);
	return { id, token };
}

async function backToNew(page) {
	await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New chat', exact: true }).click();
	await waitChat(page, { surface: 'new' });
}

/** Two active chats and the page ready to hold a mutation. */
async function twoChats(ctx) {
	await installHold(ctx.page);
	ctx.a = await makeChat(ctx, 'rollback-a');
	ctx.b = await makeChat(ctx, 'rollback-b', { again: true });
	await ctx.page.waitForTimeout(500);
}

/** A row action through its button; on touch the same button is there for assistive tech, under the swipe. */
async function rowAction(page, id, name) {
	await showSidebar(page);
	await page.locator(CARD(id)).getByRole('button', { name, exact: true }).dispatchEvent('click');
}

/**
 * Open a row's chat and wait until the view has moved to it. `reopen` is for the
 * chat that is already open (a reload comes back to it): the view stays put, and
 * on a phone the tap still closes the drawer.
 */
async function openRow(page, id, { reopen = false } = {}) {
	const from = reopen ? null : (await chatState(page)).session;
	await showSidebar(page);
	await page.locator(CARD(id)).getByRole('button').first().click();
	await page.waitForFunction((from) => {
		const el = document.querySelector('[data-ui-state="chat"]');
		return el && el.getAttribute('data-ui-session') !== from && el.getAttribute('data-ui-loading') === 'false' && el.getAttribute('data-ui-surface') === 'active';
	}, from, { timeout: 60_000 });
}

/** Hold, let another device settle chat B, then refuse the held change. B must stay settled through all of it. */
async function refuseAfterOtherDevice({ page, status, mark, log, b }) {
	await waitHeld(page);
	await mark('another device settles the other chat');
	await otherDevice(page, status, 'PATCH', `/sessions/${b.id}/active`, { is_active: false });
	await waitLane(page, b.id, 'settled');
	const stopWatch = await watchStaysInLane(page, b.id, 'settled');
	await mark('the server refuses the first change');
	await refuseHeld(page);
	return async () => {
		await page.waitForTimeout(2500);
		const left = await stopWatch();
		log(`other chat after the rollback: left the lane ${JSON.stringify(left)}, now in ${await laneOf(page, b.id)}`);
		expect(!left, `the other device's settle survives the rollback (the row jumped to ${left?.to} ${left?.at}ms after the refusal)`);
		await waitLane(page, b.id, 'settled', 1000);
	};
}

// The toast is the app saying the refused change did not happen; the failed
// request behind it is the journey's own refusal.
const REFUSAL_NOISE = [{ kind: 'failed_requests', status: 503, reason: 'the journey refuses the held change on purpose: that refusal is the case' }];

const ROLLBACK_SETTLE = {
	name: 'rollback-settle',
	setup: twoChats,
	allow: [...REFUSAL_NOISE],
	async act(ctx) {
		const { page, status, mark, log, a } = ctx;
		await mark('settle the first chat; the server holds on to it');
		await armHold(page, 'PATCH', `/sessions/${a.id}/active`);
		await rowAction(page, a.id, 'Settle session');
		await waitLane(page, a.id, 'settled');

		const newTitle = `renamed elsewhere ${uniq()}`;
		await waitHeld(page);
		await mark('another device renames the chat being settled');
		await otherDevice(page, status, 'PATCH', `/sessions/${a.id}?title=${encodeURIComponent(newTitle)}`);
		await page.locator(CARD(a.id)).getByText(newTitle).waitFor({ timeout: 15_000 });

		const check = await refuseAfterOtherDevice(ctx);
		await waitLane(page, a.id, 'active');
		await check();
		const title = await page.locator(CARD(a.id)).innerText();
		log(`refused chat says: ${title.replace(/\s+/g, ' ')}`);
		expect(title.includes(newTitle), 'the refused settle gives back the settled state only, not the title from before the rename');
	}
};

const ROLLBACK_ARCHIVE = {
	name: 'rollback-archive',
	setup: twoChats,
	allow: [...REFUSAL_NOISE],
	async act(ctx) {
		const { page, mark, a } = ctx;
		await mark('archive the first chat; the server holds on to it');
		await armHold(page, 'PATCH', `/sessions/${a.id}/active`);
		await rowAction(page, a.id, 'Archive session');
		await page.locator(CARD(a.id)).waitFor({ state: 'detached', timeout: 15_000 });

		const check = await refuseAfterOtherDevice(ctx);
		await waitLane(page, a.id, 'active');
		await check();
	}
};

/** The chat's own actions, on its title: a menu at desktop width, a sheet on the phone. */
async function chatAction(page, name) {
	await page.getByRole('button', { name: 'Chat actions', exact: true })
		.or(page.locator('button[aria-current="page"][aria-haspopup="menu"]')).first().click();
	await page.getByRole('menuitem', { name, exact: true })
		.or(page.getByRole('group', { name: 'Session actions' }).getByRole('button', { name, exact: true })).first().click();
}

const ROLLBACK_RENAME = {
	name: 'rollback-rename',
	setup: twoChats,
	allow: [...REFUSAL_NOISE],
	async act(ctx) {
		const { page, mark, a } = ctx;
		await showSidebar(page);
		await openRow(page, a.id);
		await mark('rename the open chat; the server holds on to it');
		await armHold(page, 'PATCH', `/sessions/${a.id}`);
		await chatAction(page, 'Rename session');
		const box = page.getByRole('textbox', { name: 'Rename session' });
		await box.fill('A name the server will refuse');
		await box.press('Enter');

		const check = await refuseAfterOtherDevice(ctx);
		await page.waitForFunction(({ selector, token }) => (document.querySelector(selector)?.textContent || '').includes(token),
			{ selector: CARD(a.id), token: a.token }, { timeout: 15_000 });
		await check();
	}
};

const ROLLBACK_DELETE = {
	name: 'rollback-delete',
	setup: twoChats,
	allow: [...REFUSAL_NOISE],
	async act(ctx) {
		const { page, mark, a } = ctx;
		await showSidebar(page);
		await openRow(page, a.id);
		await mark('delete the open chat; the server holds on to it');
		await armHold(page, 'DELETE', `/sessions/${a.id}`);
		await chatAction(page, 'Delete');
		await page.getByRole('button', { name: 'Delete', exact: true }).last().click();
		await page.locator(CARD(a.id)).waitFor({ state: 'detached', timeout: 15_000 });

		const check = await refuseAfterOtherDevice(ctx);
		await waitLane(page, a.id, 'active');
		await check();
	}
};

/** The element is drawn in the theme's danger colour, whatever the theme makes that. */
async function expectDangerColour(locator, what, log = () => {}) {
	// A status mark is a dot, so its colour is its fill; a word's is its text.
	const { colour, danger } = await locator.evaluate((el) => {
		const probe = document.createElement('span');
		probe.style.color = 'var(--destructive)';
		document.body.append(probe);
		const danger = getComputedStyle(probe).color;
		probe.remove();
		const style = getComputedStyle(el);
		return { colour: el.classList.contains('pip') ? style.backgroundColor : style.color, danger };
	});
	log(`${what}: ${colour}`);
	expect(colour === danger, `${what} is in the danger colour (${colour}, the theme's is ${danger})`);
}

/** Everything a failed row must show: the word, the lane, and agreement after a reload. */
async function expectFailedRow({ page, mark, log, variant }, id) {
	await waitStatus(page, id, 'Failed');
	await waitLane(page, id, 'needs-you');
	await expectDangerColour(page.locator(`${CARD(id)} .st`).first(), 'the status word', log);
	await expectDangerColour(page.locator(`${CARD(id)} .lead .pip.warn.failed`), 'the row\'s sign', log);
	await mark('reload');
	await page.reload({ waitUntil: 'domcontentloaded' });
	// A new step once the recorder has seen the old page go: the reload itself
	// is not the app going backwards.
	await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
	await mark('page reloaded: the list read says the same');
	await page.locator('[data-ui-state="chat"]').waitFor({ timeout: 90_000 });
	await waitStatus(page, id, 'Failed');
	await waitLane(page, id, 'needs-you');
	if (inMobileShell(variant)) log('real /mobile/ shell');
}

async function failedSetup(ctx) {
	const { page, status, variant } = ctx;
	ctx.chat = await makeChat(ctx, 'failed');
	if (inMobileShell(variant)) {
		await page.goto(new URL(`/mobile/?session=${encodeURIComponent(ctx.chat.id)}`, status.urls.ui).toString());
		await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 1 }, { timeout: 60_000 });
	}
}

const FAILED_OPEN = {
	name: 'failed-open',
	setup: failedSetup,
	async act(ctx) {
		const { page, mark, variant, chat } = ctx;
		await mark('the provider fails the turn');
		await typeAndSend(page, `fail ${scriptTag(variant, 'error-500')}`);
		await page.getByRole('log', { name: 'Conversation messages' }).getByRole('alert').filter({ hasText: /HTTP 500/ }).first().waitFor({ timeout: 90_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });
		await page.waitForTimeout(800);
		await expectFailedRow(ctx, chat.id);

		await mark('a good turn clears it');
		await openRow(page, chat.id, { reopen: true });
		await waitChat(page, { streaming: 'false' }, { timeout: 60_000 });
		await typeAndSend(page, `all good ${uniq()}`);
		await waitTurnSettled(page, { messagesAtLeast: 3 });
		await waitStatus(page, chat.id, 'Done');
		await waitLane(page, chat.id, 'active');
	},
	allow: [{ kind: 'layout_shift', reason: 'the error and the row changing lane both move the layout, which is what this case sets up' }]
};

const FAILED_UNOPENED = {
	name: 'failed-unopened',
	setup: failedSetup,
	// The sidebar's New session button belongs to the shared UI.
	viewports: mobileShell ? ['desktop'] : undefined,
	async act(ctx) {
		const { page, mark, variant, chat } = ctx;
		await mark('start a turn that will fail, and leave the chat before it does');
		await typeAndSend(page, `fail ${scriptTag(variant, 'error-500')}`);
		// The send has left for this chat: pressing New any sooner sends it from there instead.
		await waitChat(page, { streaming: 'true', messagesAtLeast: 3 }, { timeout: 30_000 });
		await backToNew(page);
		await mark('the error lands on a chat that is not open');
		await expectFailedRow(ctx, chat.id);
	},
	allow: [{ kind: 'layout_shift', reason: 'the row changes lane, which is what this case sets up' }]
};

const FAILED_CLAUDE = {
	name: 'failed-claude',
	// The agent menu is the shared new-session screen's.
	viewports: mobileShell ? ['desktop'] : undefined,
	async setup(ctx) {
		ctx.chat = await makeChat(ctx, 'failed-claude', { agent: CLAUDE_AGENT });
	},
	async act(ctx) {
		const { page, mark, chat } = ctx;
		await mark('the delegated CLI reports an API error as the end of its turn');
		await typeAndSend(page, `fail [script:api-error]`);
		await page.getByRole('log', { name: 'Conversation messages' }).getByText(/API Error|overloaded/i).first().waitFor({ timeout: 90_000 });
		await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });
		await page.waitForTimeout(800);
		await expectFailedRow(ctx, chat.id);
	},
	allow: [{ kind: 'layout_shift', reason: 'the row changes lane, which is what this case sets up' }]
};

const FAILED_SETTLED = {
	name: 'failed-settled',
	setup: failedSetup,
	async act(ctx) {
		const { page, mark, variant, chat } = ctx;
		await typeAndSend(page, `fail ${scriptTag(variant, 'error-500')}`);
		await waitStatus(page, chat.id, 'Failed');
		await mark('settle it: not now, but still what happened');
		await rowAction(page, chat.id, 'Settle session');
		await waitLane(page, chat.id, 'settled');
		// In the shared UI settling the open chat leaves it (on a phone that closes
		// the drawer too), so read the row once that has happened, not while it is
		// going. The /mobile/ shell stays on the chat.
		if (!inMobileShell(variant)) await waitChat(page, { surface: 'new' }, { timeout: 30_000 });
		await waitLane(page, chat.id, 'settled');
		// A settled row is the compact one: no status word to read, but the alert
		// sign is in the danger colour and assistive tech hears the word.
		const sign = page.locator(`${CARD(chat.id)} .lead .pip.warn.failed`);
		await sign.waitFor({ timeout: 5000 });
		const said = await page.locator(`${CARD(chat.id)} .title .sr-only`).first().textContent();
		expect(/^Failed: /.test(said ?? ''), `a settled failed row announces it: ${JSON.stringify(said)}`);
		await expectDangerColour(sign, 'the settled row\'s sign');
		await page.waitForTimeout(800);
	},
	allow: [{ kind: 'layout_shift', reason: 'the row changes lane, which is what this case sets up' }]
};

const FAILED_CLEARED = {
	name: 'failed-cleared',
	setup: failedSetup,
	async act(ctx) {
		const { page, mark, variant, chat } = ctx;
		await typeAndSend(page, `fail ${scriptTag(variant, 'error-500')}`);
		await waitStatus(page, chat.id, 'Failed');
		await waitLane(page, chat.id, 'needs-you');
		await mark('clear it: the error it showed is gone with the conversation');
		await api(page, 'POST', `/sessions/${encodeURIComponent(chat.id)}/clear`);
		await page.waitForFunction((selector) => {
			const row = document.querySelector(selector);
			return row && row.querySelector('.details')?.textContent?.split(' · ')[0].trim() !== 'Failed' && !row.querySelector('.lead .warn.failed');
		}, CARD(chat.id), { timeout: 30_000 });
		expect(await laneOf(page, chat.id) !== 'needs-you', 'a cleared chat no longer needs you');
		await mark('reload');
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
		await mark('page reloaded: the list read agrees');
		await page.locator('[data-ui-state="chat"]').waitFor({ timeout: 90_000 });
		await showSidebar(page);
		await page.locator(CARD(chat.id)).waitFor({ timeout: 30_000 });
		expect(await statusWord(page, chat.id).catch(() => '') !== 'Failed', 'the reload does not bring the failure back');
		expect(await laneOf(page, chat.id) !== 'needs-you', 'nor the lane');
	},
	allow: [
		{ kind: 'layout_shift', reason: 'the row changes lane and the composer centres in the cleared chat, which is what this case sets up' }
	]
};

const STOPPED = {
	name: 'stopped',
	setup: failedSetup,
	async act(ctx) {
		const { page, mark, variant, chat } = ctx;
		await mark('the user stops a turn');
		await typeAndSend(page, `go ${scriptTag(variant, 'slow', { tps: 2 })}`);
		await stopButton(page).first().waitFor({ timeout: 30_000 });
		await page.waitForTimeout(800);
		await stopButton(page).first().click();
		await waitChat(page, { streaming: 'false' }, { timeout: 30_000 });
		await page.waitForTimeout(1500);
		await showSidebar(page);
		const word = await statusWord(page, chat.id);
		expect(word !== 'Failed', 'stopping a turn on purpose is not a failure');
		expect(await laneOf(page, chat.id) === 'active', 'and it stays in Active');
	}
};

/**
 * A git repository with a remote, put under a project that has been working without
 * one: the app links the project to the remote the next time it looks. What a project
 * does the first time, every time, whatever else has run on this instance.
 */
function giveProjectARemote(status, projectId) {
	const dir = join(status.workspace_dir, projectId);
	const remote = join(status.scratch_dir, `${projectId}.git`);
	const git = (cwd, ...args) => execFileSync('git', ['-c', 'init.defaultBranch=main', '-c', 'commit.gpgsign=false', '-c', 'user.name=Journey', '-c', 'user.email=journey@example.invalid', ...args], { cwd, stdio: 'pipe' });
	mkdirSync(dir, { recursive: true });
	git(dir, 'init', '-q');
	writeFileSync(join(dir, 'README.md'), `# ${projectId}\n`);
	git(dir, 'add', 'README.md');
	git(dir, 'commit', '-q', '-m', 'First commit');
	git(status.scratch_dir, 'init', '-q', '--bare', remote);
	git(dir, 'remote', 'add', 'origin', pathToFileURL(remote).href);
	git(dir, 'push', '-q', '-u', 'origin', 'main');
}

const PROJECT_ALLOW = [
	{ kind: 'failed_requests', status: 409, url: /\/git\/(status|diff-stats|fetch)(\?|$)/, reason: 'the new-session screen asks a just-linked project for a checkout it has not got yet (see new-worktree-session)' },
	{ kind: 'errors', text: /No matching Git checkout exists/, reason: 'the same refused read, logged' },
	{ kind: 'layout_shift', reason: 'the first send moves the composer down' }
];

const NO_PROJECT_DROP = /the chosen project was dropped/;

const noProjectChosen = (hero) => hero.getByRole('button', { name: /^No project/ });

/** Choose the project unless the screen already holds it (it keeps the last one chosen). */
async function chooseProject(page, hero, name) {
	if (await noProjectChosen(hero).isVisible()) await pickProject(page, hero, name);
}

/**
 * A project that worked without a remote, a chat in it, then a remote: what the app
 * links the next time it looks. Leaves the new-session screen open, which opens on
 * the project last chosen: the choice under test.
 *
 * `rememberCatalogRow` stages the state #1078 needs instead of waiting for it to come
 * up by chance: a catalog load between the first chat and the link, which remembers
 * the project by the catalog row its remote then retires.
 */
async function projectGainingARemote(ctx, { rememberCatalogRow = false } = {}) {
	const { page, status } = ctx;
	let remembered = false;
	const rememberWrite = (request) => {
		if (request.method() === 'PUT' && /\/preferences\/composer\.project(\?|$)/.test(request.url()) && /pv2-/.test(request.postData() || '')) remembered = true;
	};
	if (rememberCatalogRow) page.on('request', rememberWrite);
	const hero = await openNewChat(ctx);
	const id = `linked-late-${uniq()}`.toLowerCase().replace(/[^a-z0-9-]/g, '-');
	ctx.project = { id, name: `Linked late ${id.slice(-6)}` };
	await api(page, 'POST', '/projects', { id, name: ctx.project.name });
	await pickProject(page, hero, ctx.project.name);
	await pickAccess(page, 'Bypass');
	ctx.older = `older-${uniq()}`;
	await typeAndSend(page, `a chat from before the link ${ctx.older}`);
	await waitTurnSettled(page);
	ctx.olderId = await chatIdByToken(page, ctx.older);

	if (rememberCatalogRow) {
		// Adding any project makes the app load the catalog again.
		ctx.nudge = `${id}-nudge`;
		if (!remembered) await api(page, 'POST', '/projects', { id: ctx.nudge, name: `Nudge ${id.slice(-6)}` });
		const deadline = Date.now() + 15_000;
		while (!remembered && Date.now() < deadline) await page.waitForTimeout(100);
		if (!remembered) throw new Error('the app never remembered the project by its catalog row');
		page.off('request', rememberWrite);
	}

	giveProjectARemote(status, id);
	await backToNew(page);
}

/** The worktree toggle, a new branch, and the composer showing it. Drop gets its own error. */
async function chooseNewWorktreeBranch(page, hero, branch) {
	try {
		await hero.getByRole('button', { name: /Switch to a worktree$/ }).click({ timeout: 10_000 });
		await hero.getByRole('button', { name: /^Worktree branch:/ }).click({ timeout: 10_000 });
		const menu = page.getByRole('dialog', { name: 'Worktree branch' });
		await menu.getByRole('button', { name: /New branch/ }).click({ timeout: 10_000 });
		await page.getByPlaceholder('Enter a branch name').fill(branch, { timeout: 10_000 });
		await page.getByRole('button', { name: 'Continue', exact: true }).click({ timeout: 10_000 });
		await hero.getByRole('button', { name: `Worktree branch: ${branch}` }).waitFor({ timeout: 10_000 });
	} catch (error) {
		if (await noProjectChosen(hero).isVisible()) throw new Error(`the chosen project was dropped while choosing its worktree branch (#1078): ${error.message.split('\n')[0]}`);
		throw error;
	}
}

// Issue 1078, on its own so nothing else has to step around it: the owner's choice of a
// project survives the project gaining a remote and the catalog refreshes that follow.
const PROJECT_CHOICE_SURVIVES = {
	name: 'project-choice-survives',
	// The drop waits for the app to look at the project and link it, which the desktop
	// screen does at once. A phone had not got there by the end of this case, so it would
	// only say the choice holds up to the link. The store under test is the same one.
	viewports: ['desktop'],
	allow: PROJECT_ALLOW,
	knownBug: {
		issue: 1078,
		error: NO_PROJECT_DROP,
		reason: 'the app remembers a project by its catalog row, and gaining a remote retires that row, so it reads the project as gone'
	},
	setup: (ctx) => projectGainingARemote(ctx, { rememberCatalogRow: true }),
	async cleanup({ page, project, nudge }) {
		if (project) await api(page, 'DELETE', `/projects/${project.id}`).catch(() => {});
		if (nudge) await api(page, 'DELETE', `/projects/${nudge}`).catch(() => {});
	},
	async act({ page, mark, project }) {
		const hero = page.getByRole('region', { name: 'New chat' });
		const holds = async (when) => {
			const deadline = Date.now() + 4_000;
			while (Date.now() < deadline) {
				if (await noProjectChosen(hero).isVisible()) throw new Error(`the chosen project was dropped ${when} (#1078)`);
				await page.waitForTimeout(100);
			}
		};
		await mark('the screen keeps the project while it links to its remote');
		await holds('while it linked to its remote');
		await hero.getByRole('button', { name: `Workspace folders: ${project.name}` }).waitFor({ timeout: 5_000 });

		await mark('a worktree on a new branch, and that holds too');
		const branch = `journey/${uniq()}`;
		await chooseNewWorktreeBranch(page, hero, branch);
		await holds('after its worktree branch was chosen');
		expect(await hero.getByRole('button', { name: `Worktree branch: ${branch}` }).isVisible(), 'the worktree branch is still chosen');
	}
};

const PROJECT_ONCE = {
	name: 'project-once',
	allow: PROJECT_ALLOW,
	setup: projectGainingARemote,
	async cleanup({ page, project }) {
		if (project) await api(page, 'DELETE', `/projects/${project.id}`).catch(() => {});
	},
	async act({ page, mark, log, project, olderId }) {
		const hero = page.getByRole('region', { name: 'New chat' });
		await mark('a worktree on a new branch, then the first message');
		// This case is about the listing. Losing the choice has its own case above
		// (#1078), so step around that one failure here and nothing else: the screen
		// saying "No project" is the only thing that sends it round again.
		const branch = `journey/${uniq()}`;
		for (let attempt = 1; ; attempt++) {
			try {
				if (await noProjectChosen(hero).isVisible()) log('known bug #1078: the screen dropped the project; choosing it again');
				await chooseProject(page, hero, project.name);
				await chooseNewWorktreeBranch(page, hero, branch);
				break;
			} catch (error) {
				if (attempt === 3 || !NO_PROJECT_DROP.test(error.message)) throw error;
				log(`known bug #1078 on attempt ${attempt}: choosing the project again`);
				await page.keyboard.press('Escape');
			}
		}
		const token = `once-${uniq()}`;
		await typeAndSend(page, `first message ${token}`);
		await waitTurnSettled(page, { timeout: 120_000 });
		const own = await chatIdByToken(page, token);
		await showSidebar(page);
		await page.locator(CARD(own)).waitFor({ timeout: 30_000 });

		await mark('the Filter lists the project once');
		const sessions = await showSidebar(page);
		await sessions.getByRole('button', { name: 'Filter', exact: true }).click();
		const sheet = page.getByRole('dialog', { name: 'Filter sessions' });
		await sheet.waitFor({ timeout: 10_000 });
		const entries = sheet.getByRole('button', { name: project.name, exact: true });
		const count = await entries.count();
		const rows = await api(page, 'GET', '/sessions?limit=100');
		const named = rows.filter((r) => r.project_id === project.id);
		log(`project keys in the rows: ${JSON.stringify([...new Set(named.map((r) => `${r.link_group_id}|${r.origin}|${r.project_id}`))])}`);
		expect(count === 1, `"${project.name}" is listed once in the Filter (found ${count})`);

		await mark('filtering by it keeps every chat of the project');
		await entries.first().click();
		await page.waitForTimeout(300);
		const shown = await page.evaluate(() => [...document.querySelectorAll('[data-session-card]')].map((el) => el.getAttribute('data-session-card')));
		log(`rows under the filter: ${shown.length}`);
		expect(shown.includes(own) && shown.includes(olderId), 'the chat from before the link and the worktree chat are both under the project');
		await page.keyboard.press('Escape');
	}
};

await runJourney({
	name: 'sidebar-truth',
	allow: [...DEV_INSTANCE_NOISE],
	cases: [ROLLBACK_SETTLE, ROLLBACK_ARCHIVE, ROLLBACK_RENAME, ROLLBACK_DELETE, FAILED_OPEN, FAILED_UNOPENED, FAILED_CLAUDE, FAILED_SETTLED, FAILED_CLEARED, STOPPED, PROJECT_CHOICE_SURVIVES, PROJECT_ONCE]
});
