// Journey: open a chat whose history this tab has not loaded yet.
//
// The surface may only move forward: the screen you were on → loading → the
// opened chat. The composer moves at most once, when the outcome is known; it
// must never go centre → bottom → centre (or back) while the history arrives,
// and the transcript must not remount.
//
// Bug this guards (flight recorder on main, 2026-10-29): opening a chat that
// turns out empty (a cleared chat) from the new-session screen docked the
// composer at the bottom for the load and snapped it back to the centre 1-3
// frames later. Root cause: the 'loading' surface always used the message
// column layout. Fix: a load keeps the layout the view already had
// (createChatLayoutTracker in src/lib/components/layout/chat-empty-state.ts).
//
// Entry points (every way a chat is opened) × target (a long transcript, an
// empty cleared chat):
//   <target>-from-new      new-session screen → sidebar row
//   <target>-from-chat     another open chat  → sidebar row
//   long-from-cleared      an empty chat      → sidebar row (hero → column)
//   <target>-deep-link     a full load of /?session=<id>
import { runJourney } from './runner.mjs';
import { SEEDED, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/** What each target looks like once open. */
const OPENED = {
	long: (page) => waitChatOpen(page, { messagesAtLeast: 50 }),
	cleared: async (page) => {
		await waitChat(page, { surface: 'cleared', loading: 'false', composer: 'centre', session: /^s:/ });
		await page.getByText('Conversation cleared', { exact: false }).waitFor();
		await page.waitForTimeout(1500);
	}
};

const fromNew = async ({ page, status }) => {
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
	await page.waitForTimeout(1000);
};

const fromChat = (key) => async ({ page, status }) => {
	await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, key))}`);
	await waitChat(page, { loading: 'false', session: /^s:/ });
	await page.waitForTimeout(1000);
};

const viaSidebar = (target) => async ({ page, mark }) => {
	await mark(`open the ${target} chat from the sidebar`);
	await openFromSidebar(page, SEEDED[target]);
	await OPENED[target](page);
};

const viaDeepLink = (target) => async ({ page, status, mark }) => {
	await mark(`load a link to the ${target} chat`);
	await page.goto(new URL(`/?session=${encodeURIComponent(seededChatId(status, target))}`, status.urls.ui).toString());
	await OPENED[target](page);
};

await runJourney({
	name: 'open-unloaded-chat',
	cases: [
		{ name: 'long-from-new', setup: fromNew, act: viaSidebar('long') },
		{ name: 'cleared-from-new', setup: fromNew, act: viaSidebar('cleared') },
		{ name: 'long-from-chat', setup: fromChat('no_project'), act: viaSidebar('long') },
		{ name: 'cleared-from-chat', setup: fromChat('no_project'), act: viaSidebar('cleared') },
		{ name: 'long-from-cleared', setup: fromChat('cleared'), act: viaSidebar('long') },
		{ name: 'long-deep-link', setup: fromNew, act: viaDeepLink('long') },
		{
			name: 'cleared-deep-link',
			setup: fromNew,
			act: viaDeepLink('cleared'),
			allow: [
				{
					kind: 'layout_shift',
					message: /input-slot in ChatView/,
					reason:
						'a page opened by a link starts loading with the composer docked, the layout of the usual outcome (a transcript); a cleared chat then moves it once to the centre (createChatLayoutTracker). One move, never a reversal, which the journey still checks; its measured size straddles the 0.1 limit run to run'
				}
			]
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
