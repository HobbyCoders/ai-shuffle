// Journey: open and close the panels around a chat, and leave for Settings and
// come back. Each must open and close in one move, with no error, no remount
// of the chat underneath (ChatView, MessageArea, ChatInput mount once within a
// step) and the chat as it was: same session, transcript and reading position.
// Runs on desktop, phone and both Fold sizes.
//
// Cases:
//   git-panel          the Git actions panel of a branch chat: open, close (X),
//                      open, close again (the sheet's handle on a phone, else Escape)
//   side-panel         the session side panel (live browser view): open, close
//   settings-and-back  Settings from the workspace nav, then the chat from the
//                      sidebar: back at the same rows the reader had on screen
import { DEFAULT_VIEWPORTS, FOLD_VIEWPORTS, runJourney } from './runner.mjs';
import {
	SEEDED,
	chatState,
	expect,
	gotoApp,
	openFromSidebar,
	scrollTranscriptTo,
	seededChatId,
	showSidebar,
	transcriptScroll,
	waitChatOpen
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ALL_SIZES = [...DEFAULT_VIEWPORTS, ...FOLD_VIEWPORTS];

const openChat = (key, minMessages) => async (ctx) => {
	const { page, status } = ctx;
	await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, key))}`);
	await waitChatOpen(page, { messagesAtLeast: minMessages, hold: 1000 });
	ctx.before = await chatState(page);
};

async function expectSameChat(page, before, what) {
	await waitChatOpen(page, { messagesAtLeast: Number(before.messages), hold: 800 });
	const now = await chatState(page);
	expect(now.session === before.session, `${what}: the same chat (${before.session} → ${now.session})`);
	expect(now.messages === before.messages, `${what}: the same transcript (${before.messages} → ${now.messages} messages)`);
}

await runJourney({
	name: 'side-panels',
	viewports: ALL_SIZES,
	cases: [
		{
			name: 'git-panel',
			setup: openChat('worktree', 2),
			async act({ page, mark, before }) {
				const panel = page.getByRole('dialog', { name: 'Git actions' });
				await mark('open the Git panel');
				await page.getByRole('button', { name: /^Git actions/ }).click();
				await panel.waitFor({ timeout: 15_000 });
				await panel.getByText('feature/second', { exact: false }).first().waitFor({ timeout: 15_000 });
				await page.waitForTimeout(1200);

				await mark('close the Git panel');
				await panel.getByRole('button', { name: 'Close Git panel', exact: true }).click();
				await panel.waitFor({ state: 'hidden', timeout: 5000 });
				await expectSameChat(page, before, 'after the Git panel');

				// On a phone the panel is a bottom sheet: its drag handle closes it too.
				const handle = page.getByRole('button', { name: /^Close Git panel \(drag down or tap\)/ });
				await mark('open the Git panel again');
				await page.getByRole('button', { name: /^Git actions/ }).click();
				await panel.waitFor({ timeout: 15_000 });
				await page.waitForTimeout(800);
				if (await handle.isVisible()) {
					await mark('close the Git panel with its handle');
					await handle.click();
				} else {
					await mark('close the Git panel with Escape');
					await page.keyboard.press('Escape');
				}
				await panel.waitFor({ state: 'hidden', timeout: 5000 });
				await expectSameChat(page, before, 'after closing the Git panel again');
			}
		},
		{
			name: 'side-panel',
			setup: openChat('long', 50),
			async act({ page, mark, before }) {
				const side = page.getByRole('complementary', { name: 'Session side panel' });
				await mark('open the side panel');
				await page.getByRole('button', { name: 'Toggle live browser view' }).click();
				await side.waitFor({ timeout: 10_000 });
				await side.getByRole('button', { name: 'Close side panel' }).waitFor();
				await page.waitForTimeout(1200);

				await mark('close the side panel');
				await side.getByRole('button', { name: 'Close side panel' }).click();
				await side.waitFor({ state: 'detached', timeout: 5000 });
				await expectSameChat(page, before, 'after the side panel');
				const scroll = await transcriptScroll(page);
				expect(scroll.atBottom, `still at the bottom of the transcript (${JSON.stringify(scroll)})`);
			}
		},
		{
			name: 'settings-and-back',
			async setup(ctx) {
				await openChat('long', 50)(ctx);
				await scrollTranscriptTo(ctx.page, 0.4);
				ctx.left = await transcriptScroll(ctx.page);
				expect(!ctx.left.atBottom, `scrolled up before leaving (${JSON.stringify(ctx.left)})`);
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, before, left, log }) {
				await mark('open Settings');
				const sessions = await showSidebar(page);
				await sessions.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Settings', exact: true }).click();
				await page.getByRole('navigation', { name: 'Settings' }).waitFor({ timeout: 15_000 });
				await page.waitForTimeout(1200);

				await mark('back to the chat');
				await openFromSidebar(page, SEEDED.long);
				await expectSameChat(page, before, 'back from Settings');
				const back = await transcriptScroll(page);
				log(`left at ${JSON.stringify(left)}, back at ${JSON.stringify(back)}`);
				expect(
					back.firstRow === left.firstRow || Math.abs(back.top - left.top) <= 150,
					`back at the rows the reader left (left ${JSON.stringify(left)}, back ${JSON.stringify(back)})`
				);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
