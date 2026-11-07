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
		async act({ page, mark }) {
			const bell = page.getByRole('button', { name: /^Notifications \(\d+ unread\)$/ }).first();
			try {
				for (const close of ['Escape', 'tap outside']) {
					await mark(`open notification history, then close it with ${close}`);
					const opener = close === 'Escape' ? bell : page.getByRole('button', { name: /^Notifications( \(\d+ unread\))?$/ }).first();
					await opener.click();
					const panel = page.getByRole('dialog', { name: 'Notifications' });
					await panel.waitFor();
					if (close === 'Escape') await page.keyboard.press('Escape');
					else await page.locator('[data-ui-mount="MessageArea"], [data-ui-state="chat"]').first().click({ position: { x: 20, y: 300 } });
					await panel.waitFor({ state: 'hidden', timeout: 5000 });
					await page.waitForTimeout(400);
					const now = await focused(page);
					expect(now !== 'body', `focus stays on a control after ${close} (it is on ${now})`);
					// A tap on a control moves focus there; Escape, or a tap on nothing, hands it to the bell.
					if (close === 'Escape') expect(now.startsWith('button[Notifications'), `focus returns to the bell after Escape (it is on ${now})`);
				}
			} finally {
				await api(page, 'POST', `/notifications/${page.__keyNavNotice}/dismiss`).catch(() => {});
			}
		}
	}]
});
