// Real-editor regressions for the shared journey input driver, not mocked locators.
// Observe the actual WebSocket query boundary so CR/Enter and completion-induced
// submissions cannot pass merely because a final Send click also occurred.
import { runJourney } from './runner.mjs';
import { PROJECT, composer, expect, openNewChat, typeAndSend, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const inputs = [
	{ name: 'crlf-normalization', message: token => `first\r\nsecond\rthird ${token}` },
	{ name: 'slash-literal-newline', prefix: '/compact', picker: 'Commands', message: token => `/compact\ncontinue ${token}` },
	{ name: 'mention-literal-newline', prefix: '@README', picker: 'Files', message: token => `@README\ncontinue ${token}` }
];

await runJourney({
	name: 'composer-helper',
	// Desktop Enter submits; both CPU profiles exercise the completion handlers.
	// The existing native-model phone variants cover the shared helper on touch.
	viewports: ['desktop'],
	allow: [...DEV_INSTANCE_NOISE],
	cases: inputs.map(input => ({
		name: input.name,
		async setup(ctx) {
			await ctx.page.addInitScript(() => {
				window.__helperQueries = [];
				const send = WebSocket.prototype.send;
				WebSocket.prototype.send = function(data) {
					try {
						const frame = JSON.parse(data);
						if (frame.type === 'query') window.__helperQueries.push(frame.prompt);
					} catch { /* non-JSON frames are not queries */ }
					return send.apply(this, arguments);
				};
			});
			await openNewChat(ctx, { project: PROJECT });
			if (input.picker) {
				// Warm the real catalog and prove this editor has the completion that
				// used to swallow Shift+Enter. Do not stub any command/file response.
				await composer(ctx.page).fill(input.prefix);
				await ctx.page.getByRole('listbox', { name: input.picker, exact: true }).getByRole('option').first().waitFor();
				await ctx.page.keyboard.press('Escape');
				await composer(ctx.page).fill('');
			}
		},
		async act({ page, mark, log }) {
			const text = input.message(`helper-${uniq()}`);
			const expected = text.replace(/\r\n?/g, '\n');
			let pickerSeen = false;
			const insertText = page.keyboard.insertText;
			// Let the real async lookup finish before the literal newline is
			// dispatched. This is driver scheduling, not a mocked editor/locator:
			// the real keyboard API still inserts into the production Tiptap.
			page.keyboard.insertText = async value => {
				if (input.picker) {
					await page.getByRole('listbox', { name: input.picker, exact: true }).getByRole('option').first().waitFor();
					pickerSeen = true;
				}
				return insertText.call(page.keyboard, value);
			};
			await mark('literal multiline input, then one explicit Send');
			try {
				await typeAndSend(page, text);
			} finally {
				page.keyboard.insertText = insertText;
			}
			await page.waitForFunction(() => window.__helperQueries.length > 0);
			await page.waitForTimeout(500);
			const observed = { queries: await page.evaluate(() => window.__helperQueries), pickerSeen };
			log(JSON.stringify(observed));
			expect(observed.queries.length === 1, `exactly one submission, not an Enter/completion send and a second Send: ${JSON.stringify(observed.queries)}`);
			expect(observed.queries[0] === expected, `the real editor preserves normalized literal text: ${JSON.stringify(observed.queries[0])}`);
			if (input.picker) expect(observed.pickerSeen, `${input.picker} completion really opened during helper typing`);
		}
	}))
});
