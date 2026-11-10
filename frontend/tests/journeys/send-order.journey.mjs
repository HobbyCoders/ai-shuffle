// Journey: two quick sends to one chat are written in the order they were
// made, whichever component made them (#1444).
//
// Owner report this guards: a message typed in the composer and a request
// sent from a panel a moment later reached the agent in the wrong order.
// The composer awaits before its message goes (the cold-cache check, the
// chat socket); a panel sends straight to the store. Before the fix each
// component kept its own order, so the panel's later request overtook.
//
// Here the composer's send waits on a slow /cache-state answer (held 1 s,
// under the check's 1.5 s cap) while "Resolve with agent" in the Git panel
// sends straight to the store. The chat socket's frames must carry the
// composer message first. Resolve then goes after it or, finding the agent
// now working, is refused with its note (its own rule, re-checked after the
// wait). The socket-held-connecting wait is covered by the real-store tests
// in ChatInput.note-mention.test.ts (#1444 case 1): a page cannot hold its
// own chat socket connecting without patching WebSocket.
//
//   node tests/journeys/send-order.journey.mjs [--case=composer-then-panel@desktop-fast] [--list]
import { runJourney } from './runner.mjs';
import { composer, expect, isPhone, openNewChat, scriptTag, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PROJECT_NAME, restoreRepo, stageConflict } from './lib/git-repo.mjs';

/** Every query frame's prompt, in the order the page wrote them. */
const queriesWritten = (page) => page.evaluate(() => window.__sendOrder?.slice() ?? []);

/** Composer message, then Resolve with agent, while the cold-cache check is held. */
const composerThenPanel = {
	setup: async (ctx) => {
		const { page, status, variant } = ctx;
		restoreRepo(status);
		stageConflict(status);
		await openNewChat(ctx, { project: PROJECT_NAME });
		await typeAndSend(page, `${scriptTag(variant, 'default')} send order ${uniq()}`);
		await waitTurnSettled(page, { messagesAtLeast: 2, hold: 800 });
	},
	act: async (ctx) => {
		const { page, mark, status, variant } = ctx;
		try {
			// The composer's message is typed first; the Git panel opens over it.
			ctx.typed = `${scriptTag(variant, 'default')} composer first ${uniq()}`;
			const box = composer(page);
			await box.click();
			await box.pressSequentially(ctx.typed.slice(0, 12));
			await page.keyboard.insertText(ctx.typed.slice(12));
			await page.getByRole('button', { name: /^Git actions/ }).click();
			const panel = page.getByRole('dialog', { name: 'Git actions' });
			await panel.waitFor({ timeout: 15_000 });
			const sync = panel.getByRole('button', { name: 'Sync', exact: true });
			await sync.and(page.locator(':not([disabled])')).waitFor({ timeout: 30_000 });
			await page.waitForTimeout(800);
			await mark('press Sync');
			await sync.click();
			const resolve = panel.getByRole('status').getByRole('button', { name: 'Resolve with agent' });
			await resolve.waitFor({ timeout: 20_000 });
			await page.waitForTimeout(800);

			// Hold the cold-cache answer in the page (a service worker may answer
			// before a Playwright route sees the request), and record the order
			// the chat socket writes queries in.
			await page.evaluate(() => {
				const order = (window.__sendOrder = []);
				const fetch = window.fetch;
				window.fetch = async (...args) => {
					const url = String(args[0] instanceof Request ? args[0].url : args[0]);
					if (/\/cache-state(\?|$)/.test(url)) {
						order.push('(cache-state asked)');
						await new Promise((resolve) => setTimeout(resolve, 1_000));
					}
					return fetch(...args);
				};
				const send = WebSocket.prototype.send;
				WebSocket.prototype.send = function (data) {
					try {
						const frame = JSON.parse(String(data));
						if (frame?.type === 'query') order.push(String(frame.prompt));
					} catch { /* not JSON */ }
					return send.call(this, data);
				};
			});
			// Send without a pointer press: a click outside closes the panel. A
			// phone's Enter is a new line, so its composer form is submitted.
			await mark('send from the composer, then Resolve with agent');
			await box.focus();
			ctx.sentAt = Date.now();
			if (isPhone(variant)) await box.evaluate((field) => field.closest('form').requestSubmit());
			else await page.keyboard.press('Enter');
			await page.waitForTimeout(150);
			ctx.log(`Resolve with agent pressed ${Date.now() - ctx.sentAt}ms after Enter`);
			await resolve.click();
			// Resolve with agent goes after the composer message, or, finding the
			// agent now working on it, is refused with its note (its own rule,
			// re-checked after the wait). It never goes first.
			const refused = panel.getByText(/wasn.t sent/i);
			await page.waitForFunction(() => (window.__sendOrder?.filter((entry) => !entry.startsWith('(')).length ?? 0) >= 2, null, { timeout: 20_000 })
				.catch(() => {});
			const seen = await queriesWritten(page);
			ctx.log(`in order: ${JSON.stringify(seen.map((prompt) => prompt.slice(0, 60)))}`);
			expect(seen[0] === '(cache-state asked)', 'the composer asked the cold-cache check before it sent');
			const written = seen.filter((entry) => !entry.startsWith('('));
			expect(written[0] === ctx.typed, 'the composer message, sent first, is written first');
			if (written.length === 2) {
				expect(/resolve the merge conflicts/i.test(written[1]), 'the Resolve with agent request is written after it');
			} else {
				expect(written.length === 1 && await refused.isVisible(), `Resolve with agent was refused with its note (${written.length} written)`);
				ctx.log('Resolve with agent was refused: the agent was working on the composer message');
			}
			await waitTurnSettled(page, { messagesAtLeast: 2 + 2 * written.length, hold: 1200 });
		} finally {
			try { restoreRepo(status); } catch (error) { ctx.log(`restore failed: ${error.message}`); }
		}
	}
};

await runJourney({
	name: 'send-order',
	cases: [
		{ name: 'composer-then-panel', viewports: ['desktop'], ...composerThenPanel },
		{
			// The phone's Git panel is a bottom sheet: Resolve's refusal note,
			// arriving with the composer's send, grows it upward (#1465).
			name: 'composer-then-panel-phone',
			viewports: ['phone'],
			...composerThenPanel,
			// Only that one move of the sheet's own rows, in that step, up to
			// 0.12 (measured 0.095); any other shift, the composer's included,
			// is still judged.
			expectShift: [{
				step: 'send from the composer, then Resolve with agent',
				moved: /^(?:div\.gp-(?:rows|pipe|files|foot)|p\.gp-why)$/,
				maxValue: 0.12,
				reason: 'known bug #1465: a late Git action note shifts the phone sheet\'s rows'
			}]
		}
	],
	allow: DEV_INSTANCE_NOISE
});
