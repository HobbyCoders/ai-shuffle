// Shared steps for UI journeys: finding the app's controls by role and label,
// staging a new chat, opening seeded chats, sending and waiting for a settled
// chat. Every wait reads the flight-recorder signature (data-ui-* on ChatView,
// see src/lib/utils/ui-signature.ts) rather than guessing with timeouts.
//
// Guide: tests/journeys/README.md.

export const PROJECT = 'Dev sandbox';
export const AGENT = 'Dev agent';
/** Seeded chat titles (scripts/devloop/seed.py) by the seed's own keys. */
export const SEEDED = {
	long: 'Long transcript (seeded)',
	running: 'Tool call in progress (seeded)',
	cleared: 'Cleared chat (seeded)',
	other_project: 'Other project chat (seeded)',
	no_project: 'Quick question (seeded)',
	worktree: 'Worktree chat (seeded)'
};

let counter = 0;
/** A short token unique to this run, so a journey's messages never collide with an earlier run's. */
export const uniq = () => `${Date.now().toString(36)}${(counter++).toString(36)}`;

/** The seeded session id for a seed key (status.seed.chats). */
export function seededChatId(status, key) {
	const id = status.seed?.chats?.[key];
	if (!id) throw new Error(`the dev instance has no seeded chat "${key}" (seed: ${JSON.stringify(status.seed)})`);
	return id;
}

export const CHAT = '[data-ui-state="chat"]';

/**
 * A fake-AI scenario tag with options plus the variant's first-token latency:
 * scriptTag(variant, 'slow', { tps: 3 }) → `[script:slow tps=3 latency_ms=1500]`.
 */
export function scriptTag(variant, scenario, options = {}) {
	const opts = { ...options };
	if (variant?.throttle?.aiLatencyMs && opts.latency_ms === undefined) opts.latency_ms = variant.throttle.aiLatencyMs;
	const extra = Object.entries(opts).map(([k, v]) => ` ${k}=${v}`).join('');
	return `[script:${scenario}${extra}]`;
}

/** The slow scenario's reply: its first words and its last. */
export const SLOW_REPLY = { start: 'This reply streams slowly', end: 'switch chats and come back. Done.' };

/** The chat surface's flight-recorder signature right now. */
export function chatState(page) {
	return page.evaluate((selector) => {
		const el = document.querySelector(selector);
		if (!el) return null;
		const out = {};
		for (const attr of el.getAttributeNames()) {
			if (attr.startsWith('data-ui-')) out[attr.slice(8)] = el.getAttribute(attr);
		}
		return out;
	}, CHAT);
}

/**
 * Wait until the chat signature satisfies `want`: an object of field → value
 * (a string, a RegExp source in a RegExp, or a function body is not supported:
 * keep it declarative). Numeric minimums use `{ messagesAtLeast: n }`.
 */
export async function waitChat(page, want, { timeout = 60_000 } = {}) {
	const spec = Object.fromEntries(
		Object.entries(want).map(([k, v]) => [k, v instanceof RegExp ? { re: v.source } : v])
	);
	try {
		await page.waitForFunction(
			({ selector, spec }) => {
				const el = document.querySelector(selector);
				if (!el) return false;
				for (const [key, value] of Object.entries(spec)) {
					if (key === 'messagesAtLeast') {
						if (Number(el.getAttribute('data-ui-messages') || 0) < value) return false;
						continue;
					}
					if (key === 'messagesAtMost') {
						if (Number(el.getAttribute('data-ui-messages') || 0) > value) return false;
						continue;
					}
					const actual = el.getAttribute(`data-ui-${key}`);
					if (value && typeof value === 'object') {
						if (!new RegExp(value.re).test(actual || '')) return false;
					} else if (actual !== String(value)) return false;
				}
				return true;
			},
			{ selector: CHAT, spec },
			{ timeout, polling: 'raf' }
		);
	} catch (error) {
		const now = await chatState(page).catch(() => null);
		throw new Error(`chat never reached ${JSON.stringify(want)}; it shows ${JSON.stringify(now)} (${error.message.split('\n')[0]})`);
	}
}

/** Load the app (unrecorded) and wait for the chat surface to exist. */
export async function gotoApp(page, status, path = '/') {
	await page.goto(new URL(path, status.urls.ui).toString());
	await page.locator(CHAT).waitFor({ timeout: 90_000 });
	// The model chip appears once providers have loaded: the app is hydrated.
	await page.getByRole('button', { name: /^Model: / }).first().waitFor({ timeout: 60_000 });
}

/**
 * Open `trigger`'s popup and click `item` in it until `done` shows. The app
 * may re-render while it hydrates (stores loading in), which closes a popup
 * opened too early; retry instead of failing on that setup noise.
 */
export async function pick(trigger, popup, item, done) {
	for (let attempt = 0; attempt < 5; attempt++) {
		if (await done.isVisible()) return;
		try {
			if (!(await popup.isVisible())) await trigger.click();
			await item.click({ timeout: 5000 });
			await done.waitFor({ timeout: 5000 });
			return;
		} catch {
			/* retry */
		}
	}
	await done.waitFor({ timeout: 1000 });
}

/** Click a trigger until its popup shows (an early click can land before hydration). */
export async function openPopup(trigger, popup) {
	for (let attempt = 0; attempt < 3; attempt++) {
		if (await popup.isVisible()) return popup;
		await trigger.click();
		try {
			await popup.waitFor({ timeout: 10_000 });
			return popup;
		} catch {
			/* retry */
		}
	}
	throw new Error('popup never opened');
}

/** Pick the seed's Dev agent on the new-session hero (the preview cookie cannot persist a default). */
export async function pickAgent(page, hero) {
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${AGENT}` })
	);
}

export async function pickProject(page, hero, project = PROJECT) {
	const item = page.getByRole('button', { name: project, exact: true });
	await pick(hero.getByRole('button', { name: /^No project/ }), item, item, hero.getByRole('button', { name: `Workspace folders: ${project}` }));
}

/**
 * Fresh page on the new-session screen with the Dev agent picked (and a
 * project when given). Returns the hero region.
 */
export async function openNewChat({ page, status }, { project = null } = {}) {
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New session' });
	await hero.waitFor();
	await pickAgent(page, hero);
	if (project) await pickProject(page, hero, project);
	return hero;
}

export const composer = (page) => page.getByRole('textbox', { name: 'Message' });
export const sendButton = (page) => page.getByRole('button', { name: 'Send', exact: true });
export const stopButton = (page) => page.getByRole('button', { name: /^Stop/ });

/** Type into the composer and press its Send button. */
export async function typeAndSend(page, text) {
	const box = composer(page);
	await box.click();
	await box.fill(text);
	await sendButton(page).click();
}

/** Whether this variant is a touch phone (the sidebar is an overlay drawer there). */
export const isPhone = (variant) => variant.viewport !== 'desktop' && variant.viewport !== 'fold-inner';

/** Show the sessions sidebar when it is hidden (always on a phone). */
export async function showSidebar(page) {
	const sessions = page.getByRole('complementary', { name: 'Sessions' });
	if (await sessions.isVisible()) return sessions;
	await page.getByRole('button', { name: 'Show sidebar' }).click();
	await sessions.waitFor({ timeout: 10_000 });
	return sessions;
}

/** The sidebar row button for a chat title. */
export function sidebarRow(sessions, title) {
	return sessions.getByRole('group', { name: `${title} session`, exact: true }).getByRole('button').first();
}

/** Open a chat from the sidebar (opening the drawer first on a phone). */
export async function openFromSidebar(page, title) {
	const sessions = await showSidebar(page);
	const row = sidebarRow(sessions, title);
	await row.scrollIntoViewIfNeeded();
	await row.click();
}

/** Wait for an opened chat to show its settled transcript, then hold a moment for late reloads. */
export async function waitChatOpen(page, { messagesAtLeast = 1, hold = 1500 } = {}) {
	await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast }, { timeout: 60_000 });
	if (hold) await page.waitForTimeout(hold);
}

/**
 * Wait for a reply turn to finish: not streaming, at least `messagesAtLeast`
 * transcript messages, `text` (when given) on screen. Holds a moment after,
 * because a late fold or feed restart is what sends a surface backwards.
 */
export async function waitTurnSettled(page, { messagesAtLeast = 2, text = null, timeout = 90_000, hold = 1500 } = {}) {
	if (text) {
		try {
			await page.getByText(text, { exact: false }).last().waitFor({ timeout });
		} catch (error) {
			const now = await chatState(page).catch(() => null);
			throw new Error(`reply never arrived: "${text}" not on screen after ${timeout}ms; the chat shows ${JSON.stringify(now)} (${error.message.split('\n')[0]})`);
		}
	}
	await waitChat(page, { surface: 'active', streaming: 'false', session: /^s:/, messagesAtLeast }, { timeout });
	if (hold) await page.waitForTimeout(hold);
}

/** Every transcript row id on the page, in order (data-ui-row). */
export function rowIds(page) {
	return page.evaluate(() => [...document.querySelectorAll('[data-ui-row]')].map((el) => el.getAttribute('data-ui-row')));
}

/** Count of text nodes containing `needle` within the transcript (0 when no transcript is shown). */
export function countText(page, needle) {
	return page.evaluate((n) => {
		const area = document.querySelector('[data-ui-mount="MessageArea"]');
		if (!area) return 0;
		let count = 0;
		const walker = document.createTreeWalker(area, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			if (node.textContent && node.textContent.includes(n)) count += 1;
		}
		return count;
	}, needle);
}

/**
 * Call the app's own API from the page (same origin, the preview cookie), for
 * staging in `setup` only: never to stand in for what a journey judges.
 */
export async function api(page, method, path, body) {
	const result = await page.evaluate(
		async ({ method, path, body }) => {
			const response = await fetch(`/api/v1${path}`, {
				method,
				credentials: 'include',
				headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
				body: body === undefined ? undefined : JSON.stringify(body)
			});
			const text = await response.text();
			return { status: response.status, text };
		},
		{ method, path, body }
	);
	if (result.status >= 400) throw new Error(`${method} ${path} → HTTP ${result.status}: ${result.text.slice(0, 200)}`);
	return result.text ? JSON.parse(result.text) : null;
}

/** The id of the newest chat whose title contains `token` (a chat this run created). */
export async function chatIdByToken(page, token) {
	for (let attempt = 0; attempt < 20; attempt++) {
		const rows = await api(page, 'GET', '/sessions?limit=50');
		const row = rows.find((r) => (r.title || '').includes(token));
		if (row) return row.id;
		await page.waitForTimeout(500);
	}
	throw new Error(`no chat titled with ${token}`);
}

/**
 * How many user-message rows of the transcript contain `needle` (the fake AI
 * echoes the prompt back, so a plain text count would see the reply too).
 */
export function countUserRows(page, needle) {
	return page.evaluate(
		(n) => [...document.querySelectorAll('[data-user-turn-id]')].filter((el) => (el.textContent || '').includes(n)).length,
		needle
	);
}

/**
 * Watch, on every painted frame, how many times `needle` shows as a user's
 * message: transcript user rows plus queued (pending) bubbles. Returns a
 * function that resolves the highest count seen so far. A message that is
 * both queued and delivered at once, or delivered twice, counts 2.
 */
export async function watchUserText(page, needle) {
	const key = `w${Math.random().toString(36).slice(2)}`;
	await page.evaluate(
		({ key, needle }) => {
			const watch = (window.__journeyWatch ||= {});
			const state = (watch[key] = { max: 0, at: 0, running: true });
			const started = performance.now();
			const tick = () => {
				if (!state.running) return;
				const rows = [...document.querySelectorAll('[data-user-turn-id], .pending-messages [role="article"]')];
				const count = rows.filter((el) => (el.textContent || '').includes(needle)).length;
				if (count > state.max) {
					state.max = count;
					state.at = Math.round(performance.now() - started);
				}
				requestAnimationFrame(tick);
			};
			requestAnimationFrame(tick);
		},
		{ key, needle }
	);
	return async () =>
		page.evaluate((key) => {
			const state = window.__journeyWatch?.[key];
			if (!state) return { max: -1, at: 0 };
			state.running = false;
			return { max: state.max, at: state.at };
		}, key);
}

const TRANSCRIPT = '[role="log"][aria-label="Conversation messages"]';

/**
 * Where the transcript is scrolled: { top, max, atBottom, firstRow, lastRow }
 * with the ids (data-ui-row) of the first and last rows on screen.
 */
export function transcriptScroll(page) {
	return page.evaluate((selector) => {
		const log = document.querySelector(selector);
		if (!log) return null;
		const box = log.getBoundingClientRect();
		const onScreen = [...log.querySelectorAll('[data-ui-row]')].filter((el) => {
			if (el.parentElement?.closest('[data-ui-row]')) return false; // top-level rows only
			const r = el.getBoundingClientRect();
			return r.bottom > box.top + 1 && r.top < box.bottom - 1 && r.height > 0;
		});
		const max = log.scrollHeight - log.clientHeight;
		return {
			top: Math.round(log.scrollTop),
			max: Math.round(max),
			atBottom: max - log.scrollTop <= 48,
			firstRow: onScreen[0]?.getAttribute('data-ui-row') ?? null,
			lastRow: onScreen[onScreen.length - 1]?.getAttribute('data-ui-row') ?? null
		};
	}, TRANSCRIPT);
}

/** Scroll the transcript to a fraction of its height (0 top, 1 bottom) the way a reader would (wheel). */
export async function scrollTranscriptTo(page, fraction) {
	const log = page.locator(TRANSCRIPT);
	await log.hover();
	for (let i = 0; i < 40; i++) {
		const now = await transcriptScroll(page);
		const target = Math.round(now.max * fraction);
		const delta = target - now.top;
		if (Math.abs(delta) < 200) break;
		await page.mouse.wheel(0, Math.sign(delta) * Math.min(Math.abs(delta), 2400));
		await page.waitForTimeout(60);
	}
	await page.waitForTimeout(400);
}

/**
 * Watch every painted frame for a transcript row (data-ui-row id) that leaves
 * the page and comes back: a row remounting or a fold undone, which the mount
 * markers do not see. Rows folding away for good (a settled work log) are
 * fine. Returns a function that stops the watch and resolves the list of
 * [{ id, goneAt, backAt }] (ms since the watch started).
 */
export async function watchRowFlicker(page) {
	const key = `f${Math.random().toString(36).slice(2)}`;
	await page.evaluate((key) => {
		const watch = (window.__journeyWatch ||= {});
		const state = (watch[key] = { running: true, gone: new Map(), seen: new Set(), back: [] });
		const started = performance.now();
		const tick = () => {
			if (!state.running) return;
			const now = Math.round(performance.now() - started);
			const present = new Set([...document.querySelectorAll('[data-ui-row]')].map((el) => el.getAttribute('data-ui-row')));
			for (const id of state.seen) {
				if (!present.has(id) && !state.gone.has(id)) state.gone.set(id, now);
			}
			for (const id of present) {
				if (state.gone.has(id)) {
					state.back.push({ id: id.slice(0, 12), goneAt: state.gone.get(id), backAt: now });
					state.gone.delete(id);
				}
				state.seen.add(id);
			}
			requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}, key);
	return () =>
		page.evaluate((key) => {
			const state = window.__journeyWatch?.[key];
			if (!state) return [];
			state.running = false;
			return state.back;
		}, key);
}

/** Throw with a readable message (journeys fail by throwing; the runner reports it). */
export function expect(condition, message) {
	if (!condition) throw new Error(`expectation failed: ${message}`);
}
