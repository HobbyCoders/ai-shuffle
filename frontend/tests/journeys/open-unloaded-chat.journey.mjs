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
//
// Each case's `verify` names the composer positions it is designed to take,
// read on every painted frame: one entry per position, so a second move, or
// a move there and back, fails even when it is too small or too quick to
// count as a layout shift.
import { runJourney } from './runner.mjs';
import { SEEDED, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

/** The composer takes exactly these positions over the case. */
const composer = (...positions) => ({ recording }) => expectComposerPositions(recording, positions);

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
		{ name: 'long-from-new', setup: fromNew, act: viaSidebar('long'), verify: composer('centre', 'bottom') },
		{ name: 'cleared-from-new', setup: fromNew, act: viaSidebar('cleared'), verify: composer('centre') },
		{ name: 'long-from-chat', setup: fromChat('no_project'), act: viaSidebar('long'), verify: composer('bottom') },
		{ name: 'cleared-from-chat', setup: fromChat('no_project'), act: viaSidebar('cleared'), verify: composer('bottom', 'centre') },
		{ name: 'long-from-cleared', setup: fromChat('cleared'), act: viaSidebar('long'), verify: composer('centre', 'bottom') },
		{ name: 'long-deep-link', setup: fromNew, act: viaDeepLink('long'), verify: composer('centre', 'bottom') },
		{
			name: 'cleared-deep-link',
			setup: fromNew,
			act: viaDeepLink('cleared'),
			// The new page docks the composer for its load, then the cleared
			// chat centres it: one designed move after the reload, never back.
			verify: composer('centre', 'bottom', 'centre'),
			expectShift: [
				{
					step: 'load a link to the cleared chat',
					composer: ['bottom', 'centre'],
					maxValue: 0.15,
					reason:
						'a page opened by a link starts loading with the composer docked, the layout of the usual outcome (a transcript); a cleared chat then centres it once (createChatLayoutTracker), with no input on the page'
				}
			]
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
