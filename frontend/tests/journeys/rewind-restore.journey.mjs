// Journey: a rewound sent message always comes back to the composer whole.
//
// Cases:
//   reload-before-answer  (#1083) the server rewinds, but the page reloads before
//                         its answer arrives; the reopened chat still hands the
//                         rewound words back.
//   attachment-chip       (#1084) a message sent with an attached file comes back
//                         with its removable chip, not a raw `@…/uploads/…` path.
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, countUserRows, expect, openNewChat, sendButton, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

async function sendAndSettle(page, variant, words, messagesAtLeast) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant)}`.trim());
	await waitTurnSettled(page, { messagesAtLeast, text: `You said: “${words}` });
}

async function confirmRewindOfLastUserMessage(page) {
	await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
	await page.keyboard.press('Enter');
	await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
}

await runJourney({
	name: 'rewind-restore',
	cases: [
		{
			name: 'reload-before-answer',
			async setup(ctx) {
				await openNewChat(ctx);
				const kept = `kept-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, kept, 2);
				const rewound = `rewound-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, rewound, 4);
				ctx.state = { kept, rewound };
			},
			async act({ page, mark, state }) {
				// The server carries the rewind out; its answer never reaches the page.
				let done;
				const carriedOut = new Promise((resolve) => { done = resolve; });
				await page.route('**/rewind/execute/**', async (route) => {
					const response = await route.fetch();
					done(await response.json());
					// Held: the page reloads before this request is answered.
				});
				await mark('confirm the rewind');
				await confirmRewindOfLastUserMessage(page);
				const result = await carriedOut;
				expect(result.success && result.chat_rewound, `the server rewound: ${JSON.stringify(result)}`);
				// The feed tells this page the row went, but the answer still hasn't come.
				await waitChat(page, { messages: '2' }, { timeout: 30_000 });
				await mark('reload before the answer');
				// Reload while the route still holds the request: removing the last
				// route turns interception off, and the browser then lets held
				// requests through, which would send this rewind a second time and
				// answer the old page after all. The reload drops the held request.
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				// A new step once the recorder has seen the old page go: the reload
				// itself (chat → nothing) is not the app going backwards.
				await page.evaluate(() => new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(() => ok()))));
				await mark('page reloaded');
				await waitChatOpen(page, { messagesAtLeast: 2 });
				await waitChat(page, { messages: '2', loading: 'false' }, { timeout: 30_000 });
				await composer(page).getByText(state.rewound).waitFor({ timeout: 10_000 });
				expect((await countUserRows(page, state.rewound)) === 0, 'the rewound row is gone');
				expect((await countUserRows(page, state.kept)) === 1, 'the earlier turn stayed');
			}
		},
		{
			name: 'attachment-chip',
			async setup(ctx) {
				await openNewChat(ctx, { project: 'Dev sandbox' });
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant }) {
				const words = `attached-${uniq()}`;
				const filename = `notes-${uniq()}.txt`;
				await mark('attach a file and send');
				await composer(page).click();
				await page.keyboard.type(`${words} ${fakeAiTag(variant)}`.trim());
				await page.locator('input[type="file"]').first().setInputFiles({ name: filename, mimeType: 'text/plain', buffer: Buffer.from('hello from the journey\n') });
				const chip = composer(page).locator('[data-file-chip]');
				await chip.first().waitFor({ timeout: 30_000 });
				// The stored name can differ from the picked one (a clash gets a suffix).
				const attached = await chip.first().getAttribute('data-path');
				expect(/(^|[\\/])uploads[\\/]/.test(attached ?? '') && (attached ?? '').includes(filename.replace(/\.txt$/, '')), `the chip holds the uploaded file: ${attached}`);
				// The chip sits right after the words, with no space between: the sent
				// text reads `words@/…/uploads/notes.txt`.
				await sendButton(page).click();
				await waitTurnSettled(page, { messagesAtLeast: 2, text: `You said: “${words}` });
				await mark('rewind it');
				await confirmRewindOfLastUserMessage(page);
				await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 30_000 });
				await composer(page).getByText(words).waitFor({ timeout: 10_000 });
				await chip.first().waitFor({ timeout: 10_000 });
				const text = await composer(page).innerText();
				expect(!/[\\/]uploads[\\/]|session-uploads/.test(text), `no raw upload path in the composer: ${JSON.stringify(text)}`);
				const back = await chip.first().getAttribute('data-path');
				// The sent chip may hold a project-relative path; the restored one holds the full path.
				const base = (p) => (p ?? '').split(/[\\/]/).pop();
				expect(!!back && base(back) === base(attached), `the chip holds the same upload: ${back} (sent ${attached})`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
