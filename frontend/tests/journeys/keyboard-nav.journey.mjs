// Keyboard, focus and navigation in the real app: Shift+Enter with a suggestion
// menu open is a new line (#1194), Ctrl+N works from the message editor
// (#1202), New chat pressed right after Send never carries the message into the
// new chat (#1077), browser Back after a sidebar chat switch returns to the
// previous chat (#953), and closing the phone's notification history keeps
// focus on a real control (#1168).
import { runJourney } from './runner.mjs';
import {
	PROJECT, SEEDED, api, chatState, composer, countUserRows, expect, gotoApp, isPhone,
	openFromSidebar, openNewChat, sendButton, showSidebar, uniq, waitChat, waitChatOpen
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/** Record every chat query the page sends over the WebSocket (setup, before load). */
async function watchQueries(page) {
	await page.addInitScript(() => {
		window.__keyNavQueries = [];
		const send = WebSocket.prototype.send;
		WebSocket.prototype.send = function (data) {
			try {
				const frame = JSON.parse(data);
				if (frame.type === 'query') window.__keyNavQueries.push(frame.prompt);
			} catch { /* non-JSON frames are not queries */ }
			return send.apply(this, arguments);
		};
	});
}
const queries = (page) => page.evaluate(() => window.__keyNavQueries || []);
const editorText = (page) => composer(page).evaluate((el) => el.innerText.replace(/\n+$/, ''));
const focused = (page) => page.evaluate(() => {
	const el = document.activeElement;
	return !el || el === document.body ? 'body' : `${el.tagName.toLowerCase()}[${el.getAttribute('aria-label') || ''}]`;
});

/**
 * A point outside the notification panel and its bell. In order of preference:
 * empty chat area, any other empty page, then a control. A full history covers
 * the whole chat area but the composer (as on a shared CI instance).
 */
const outsideSpot = (page) => page.evaluate(() => {
	const panel = document.querySelector('[role="dialog"][aria-label="Notifications"]');
	const bellRoot = panel?.closest('.notif-center');
	const control = 'button, a[href], input, textarea, select, [contenteditable="true"], [role="button"], [role="textbox"], [tabindex]:not([tabindex="-1"])';
	const found = [];
	for (let y = 8; y < window.innerHeight - 8; y += 16) {
		for (let x = 12; x < window.innerWidth - 8; x += 24) {
			const el = document.elementFromPoint(x, y);
			if (!el || panel?.contains(el) || bellRoot?.contains(el)) continue;
			// The composer box's padding focuses the editor, so it counts as a control.
			const empty = !el.closest(control) && !el.closest('[data-ui-mount="ChatInput"]');
			const rank = empty ? (el.closest('[data-ui-state="chat"]') ? 0 : 1) : 2;
			found[rank] ??= { x, y, empty };
		}
	}
	return found.find(Boolean) ?? null;
});

async function clearComposer(page) {
	await composer(page).click();
	await composer(page).press('ControlOrMeta+A');
	await composer(page).press('Backspace');
}

await runJourney({
	name: 'keyboard-nav',
	allow: [...DEV_INSTANCE_NOISE],
	// The New-chat-after-Send case holds the cold-cache request with page.route,
	// which cannot see API fetches once the product service worker has claimed
	// the page (it did on a hosted phone run, so the send went straight out).
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [{
		name: 'shift-enter-with-a-suggestion-menu-open-is-a-new-line',
		viewports: ['desktop'],
		async setup(ctx) {
			await watchQueries(ctx.page);
			await openNewChat(ctx, { project: PROJECT });
		},
		async act({ page, mark, log }) {
			for (const [prefix, picker] of [['/compact', 'Commands'], ['@README', 'Files']]) {
				await mark(`type ${prefix} and wait for the ${picker} menu`);
				await clearComposer(page);
				await composer(page).pressSequentially(prefix);
				const menu = page.getByRole('listbox', { name: picker, exact: true });
				await menu.getByRole('option').first().waitFor({ timeout: 30_000 });
				await mark(`Shift+Enter with the ${picker} menu open`);
				await composer(page).press('Shift+Enter');
				await menu.waitFor({ state: 'hidden', timeout: 5000 });
				await composer(page).pressSequentially('next');
				await page.waitForTimeout(500);
				const text = await editorText(page);
				log(`${picker}: ${JSON.stringify(text)}`);
				expect(text === `${prefix}\nnext`, `Shift+Enter inserted a new line and kept what was typed (${JSON.stringify(text)})`);
				expect((await queries(page)).length === 0, 'Shift+Enter sent nothing');
				expect((await chatState(page))?.surface === 'new', 'still on the new-chat screen: nothing was sent');
			}
			await clearComposer(page);
		}
	}, {
		name: 'ctrl-n-from-the-message-editor',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
		},
		async act({ page, mark }) {
			await mark('Ctrl+N with the caret in the message editor');
			await composer(page).click();
			expect((await focused(page)).includes('Message'), 'the message editor has focus');
			await page.keyboard.press('Control+N');
			await waitChat(page, { surface: 'new' }, { timeout: 10_000 });
			await page.getByRole('region', { name: 'New chat' }).waitFor();
		}
	}, {
		name: 'new-chat-right-after-send-keeps-the-message-in-its-chat',
		throttles: ['fast'],
		async setup({ page, status }) {
			await watchQueries(page);
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
		},
		async act({ page, mark, variant, log }) {
			const token = `kept-${uniq()}`;
			const chatA = (await chatState(page)).session;
			// Hold the cold-cache check before a send (a request the send awaits)
			// until New chat has been pressed, so the switch lands in that window
			// every time, as it does on a slow connection.
			let release;
			let held = 0;
			const released = new Promise((resolve) => { release = resolve; });
			await page.route('**/cache-state*', async (route) => { held += 1; await released; await route.continue().catch(() => {}); });
			await composer(page).click();
			await composer(page).pressSequentially(token);
			await mark('Send, then New chat at once');
			await sendButton(page).click();
			if (isPhone(variant)) {
				const sessions = await showSidebar(page);
				await sessions.getByRole('button', { name: 'New chat', exact: true }).first().click();
			} else {
				await page.keyboard.press('Control+N');
			}
			await waitChat(page, { surface: 'new' }, { timeout: 10_000 });
			expect(held > 0, 'the send was still waiting on its cold-cache check when New chat was pressed');
			release();
			await page.unroute('**/cache-state*');
			await page.waitForTimeout(2000);
			const sent = await queries(page);
			log(`queries: ${JSON.stringify(sent)}`);
			expect(!sent.some((q) => q.includes(token)), 'the message was not sent anywhere');
			expect((await chatState(page)).surface === 'new', 'the new chat stays empty: no chat was started with the message');
			expect(await page.getByText('Message not sent: the chat changed before it went out', { exact: false }).first().isVisible(), 'the composer says the message was kept, not sent');
			expect(!(await editorText(page)).includes(token), 'the new chat’s composer does not hold the message');

			await mark('back to the chat it was written in');
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
			expect((await chatState(page)).session === chatA, 'the first chat is open again');
			expect((await editorText(page)).includes(token), 'the message waits there as its draft');
			expect((await countUserRows(page, token)) === 0, 'and it was not sent in that chat either');
			await clearComposer(page);
			await page.waitForTimeout(500);
		}
	}, {
		// An edit made while the send waits is the newest draft; the called-off
		// send must not put back the text as it was at Send.
		name: 'edits-made-while-a-send-waits-stay-in-the-draft',
		viewports: ['desktop'],
		throttles: ['fast'],
		async setup({ page, status }) {
			await watchQueries(page);
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
		},
		async act({ page, mark, log }) {
			const token = `kept-${uniq()}`;
			const fix = `fix-${uniq()}`;
			let release;
			let held = 0;
			const released = new Promise((resolve) => { release = resolve; });
			await page.route('**/cache-state*', async (route) => { held += 1; await released; await route.continue().catch(() => {}); });
			await composer(page).click();
			await composer(page).pressSequentially(token);
			await mark('Send, correct the message while it waits, then New chat');
			await sendButton(page).click();
			await composer(page).press('End');
			await composer(page).pressSequentially(` ${fix}`);
			await page.keyboard.press('Control+N');
			await waitChat(page, { surface: 'new' }, { timeout: 10_000 });
			expect(held > 0, 'the send was still waiting on its cold-cache check');
			release();
			await page.unroute('**/cache-state*');
			await page.waitForTimeout(2000);
			expect(!(await queries(page)).some((q) => q.includes(token)), 'the message was not sent anywhere');

			await mark('back to the chat it was written in');
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
			const draft = await editorText(page);
			log(`draft: ${JSON.stringify(draft)}`);
			expect(draft.includes(token) && draft.includes(fix), 'the draft holds the message with the correction made while it waited');
			await clearComposer(page);
			await page.waitForTimeout(500);
		}
	}, {
		// After a reload the store waits for the chat's history before it sends.
		// New chat in that wait used to send the message into the new chat.
		name: 'new-chat-during-a-reloaded-chats-restore-keeps-the-message',
		viewports: ['desktop'],
		throttles: ['fast'],
		async setup({ page, status }) {
			await watchQueries(page);
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
		},
		async act({ page, mark, log }) {
			const token = `kept-${uniq()}`;
			const chatA = (await chatState(page)).session;
			await composer(page).click();
			await composer(page).pressSequentially(token);
			await page.waitForTimeout(500);
			// Hold the chat's history feed so the reload stays in its restore.
			let release;
			let held = 0;
			const released = new Promise((resolve) => { release = resolve; });
			// The page state names chats by a hash, and only this chat's feed is
			// asked for after the reload, so hold any chat's.
			const feed = (url) => /\/sessions\/[^/]+\/events$/.test(url.pathname);
			await page.route(feed, async (route) => { held += 1; await released; await route.continue().catch(() => {}); });
			await mark('reload with the history held');
			await page.reload({ waitUntil: 'domcontentloaded' });
			// A reload is an intentional document replacement, not a boot reversal.
			await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
			await mark('page reloaded: Send');
			await composer(page).waitFor();
			await page.waitForFunction((t) => document.querySelector('[data-message-editor]')?.textContent?.includes(t), token, { timeout: 15_000 });
			// The draft paints from the tab's mirror before the history is asked for.
			for (let waited = 0; held === 0 && waited < 15_000; waited += 100) await page.waitForTimeout(100);
			expect(held > 0, 'the reloaded chat is waiting for its history');
			await sendButton(page).click();
			// The cold-cache check answers; the send then waits for the restore.
			await page.waitForTimeout(800);
			await mark('New chat while the send waits for the history');
			await page.keyboard.press('Control+N');
			await waitChat(page, { surface: 'new' }, { timeout: 10_000 });
			release();
			await page.unroute(feed);
			await page.waitForTimeout(2000);
			const sent = await queries(page);
			log(`queries: ${JSON.stringify(sent)}`);
			expect(!sent.some((q) => q.includes(token)), 'the message was not sent anywhere');
			expect((await chatState(page)).surface === 'new', 'the new chat stays empty: no chat was started with the message');
			expect(await page.getByText('Message not sent: the chat changed before it went out', { exact: false }).first().isVisible(), 'the composer says the message was kept, not sent');

			await mark('back to the chat it was written in');
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
			expect((await chatState(page)).session === chatA, 'the first chat is open again');
			expect((await editorText(page)).includes(token), 'the message waits there as its draft');
			expect((await countUserRows(page, token)) === 0, 'and it was not sent in that chat either');
			await clearComposer(page);
			await page.waitForTimeout(500);
		}
	}, {
		name: 'browser-back-after-a-sidebar-switch',
		throttles: ['fast'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openFromSidebar(page, SEEDED.no_project);
			await waitChatOpen(page);
		},
		async act({ page, mark, variant, log }) {
			const chatA = (await chatState(page)).session;
			await mark('open another chat from the sidebar');
			await openFromSidebar(page, SEEDED.worktree);
			await waitChat(page, { surface: 'active', loading: 'false' }, { timeout: 30_000 });
			await page.waitForFunction((a) => document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-session') !== a, chatA);
			await page.waitForTimeout(800);
			const origin = new URL(page.url()).origin;
			await mark('browser Back');
			await page.goBack();
			await page.waitForTimeout(800);
			log(`after Back: ${page.url()}`);
			expect(new URL(page.url()).origin === origin, 'Back stays in the app');
			await waitChat(page, { session: chatA, loading: 'false' }, { timeout: 10_000 });
			if (isPhone(variant)) expect(!(await page.getByRole('complementary', { name: 'Chats' }).isVisible()), 'on a phone the drawer stays closed after Back');
		}
	}, {
		name: 'phone-bell-keeps-focus-after-notification-history-closes',
		viewports: ['phone'],
		throttles: ['fast'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			const id = `keynav-${uniq()}`;
			await api(page, 'POST', '/notifications', { id, severity: 'warning', surface: 'notification', category: 'journey', title: 'Journey notice', message: 'An unread notice for the bell' });
			page.__keyNavNotice = id;
			await gotoApp(page, status);
		},
		async act({ page, mark, log }) {
			const bell = page.getByRole('button', { name: /^Notifications \(\d+ unread\)$/ }).first();
			try {
				for (const close of ['Escape', 'tap outside']) {
					await mark(`open notification history, then close it with ${close}`);
					const opener = close === 'Escape' ? bell : page.getByRole('button', { name: /^Notifications( \(\d+ unread\))?$/ }).first();
					await opener.click();
					const panel = page.getByRole('dialog', { name: 'Notifications' });
					await panel.waitFor();
					let spot = null;
					if (close === 'Escape') await page.keyboard.press('Escape');
					else {
						// The panel grows with the history (a shared instance holds other
						// journeys' notices), so pick a point outside it, on empty page if any.
						spot = await outsideSpot(page);
						expect(spot, 'some point on screen lies outside the notification panel');
						log(`tap outside at ${spot.x},${spot.y} on ${spot.empty ? 'empty page' : 'a control'}`);
						await page.mouse.click(spot.x, spot.y);
					}
					await panel.waitFor({ state: 'hidden', timeout: 5000 });
					await page.waitForTimeout(400);
					const now = await focused(page);
					expect(now !== 'body', `focus stays on a control after ${close} (it is on ${now})`);
					// A tap on a control moves focus there; Escape, or a tap on nothing, hands it to the bell.
					if (close === 'Escape' || spot?.empty) expect(now.startsWith('button[Notifications'), `focus returns to the bell after ${close} (it is on ${now})`);
				}
			} finally {
				await api(page, 'POST', `/notifications/${page.__keyNavNotice}/dismiss`).catch(() => {});
			}
		}
	}]
});
