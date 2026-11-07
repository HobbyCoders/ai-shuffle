// Journey: a rewound sent message always comes back to the composer whole.
//
// Cases:
//   reload-before-answer  (#1083) the server rewinds, but the page reloads before
//                         its answer arrives; the reopened chat still hands the
//                         rewound words back.
//   typed-while-rewinding (#1083) the row leaves on the feed before the answer
//                         comes, and the owner types a new prompt in that gap;
//                         the rewound words go to the prompt stash, never into
//                         the new prompt, and the new prompt is sent alone.
//   attachment-chip       (#1084) a rewound message with an attached file comes
//                         back with its chip, not the raw upload path. The
//                         server names the uploads; the browser never reads them
//                         out of the text.
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, composer, countUserRows, expect, openNewChat, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
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

/** Hold the rewind's answer until `release()`; resolves `carriedOut` with the server's result. */
async function holdRewindAnswer(page) {
	let done, release;
	const carriedOut = new Promise((resolve) => { done = resolve; });
	const released = new Promise((resolve) => { release = resolve; });
	await page.route('**/rewind/execute/**', async (route) => {
		const response = await route.fetch();
		done(await response.json());
		await released;
		await route.fulfill({ response }).catch(() => {});
	});
	return { carriedOut, release };
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
			name: 'typed-while-rewinding',
			async setup(ctx) {
				await openNewChat(ctx);
				const kept = `kept-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, kept, 2);
				const rewound = `rewound-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, rewound, 4);
				ctx.state = { kept, rewound, fresh: `fresh-${uniq()}` };
			},
			async act({ page, mark, state }) {
				const { carriedOut, release } = await holdRewindAnswer(page);
				await mark('confirm the rewind');
				await confirmRewindOfLastUserMessage(page);
				const result = await carriedOut;
				expect(result.success && result.chat_rewound, `the server rewound: ${JSON.stringify(result)}`);
				// The row and its dialog leave on the feed; the answer is still held.
				await waitChat(page, { messages: '2' }, { timeout: 30_000 });
				await mark('type a new prompt before the answer');
				const box = composer(page);
				await box.click();
				await box.pressSequentially(state.fresh);
				await mark('the answer arrives');
				release();
				await page.getByText('Your rewound message is in the prompt stash', { exact: false }).waitFor({ timeout: 15_000 });
				const text = (await box.innerText()).trim();
				expect(text === state.fresh, `the composer holds only the new prompt: ${JSON.stringify(text)}`);
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await mark('send the new prompt');
				await page.getByRole('button', { name: 'Send', exact: true }).click();
				await waitTurnSettled(page, { messagesAtLeast: 4, text: `You said: “${state.fresh}` });
				expect((await countUserRows(page, state.fresh)) === 1, 'the new prompt was sent');
				expect((await countUserRows(page, state.rewound)) === 0, 'the rewound words were not sent with it');
				// They are kept: adding them back is the owner's choice.
				await page.getByRole('button', { name: 'Add to composer', exact: true }).click();
				await box.getByText(state.rewound).waitFor({ timeout: 10_000 });
			}
		},
		{
			name: 'attachment-chip',
			async setup(ctx) {
				await openNewChat(ctx, { project: PROJECT });
				const kept = `kept-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, kept, 2);
				ctx.state = { kept, words: `attached-${uniq()}`, file: `note-${uniq()}.txt` };
			},
			async act({ page, mark, state, variant }) {
				const box = composer(page);
				await mark('attach a file');
				await page.locator('input[type="file"]').setInputFiles({ name: state.file, mimeType: 'text/plain', buffer: Buffer.from('rewind me\n') });
				await box.locator('[data-file-chip]').first().waitFor({ timeout: 20_000 });
				// Focus without clicking: at phone width the chip fills the middle row,
				// so a centre click selects the chip and the typed words replace it.
				await box.focus();
				await page.keyboard.press('ControlOrMeta+End');
				await box.pressSequentially(` ${state.words} ${fakeAiTag(variant)}`.trimEnd());
				expect(await box.locator('[data-file-chip]').count() === 1, 'the chip is still in the composer when sending');
				await page.getByRole('button', { name: 'Send', exact: true }).click();
				await waitTurnSettled(page, { messagesAtLeast: 4, text: `You said:` });
				await mark('rewind the message with the attachment');
				await confirmRewindOfLastUserMessage(page);
				await waitChat(page, { messages: '2' }, { timeout: 30_000 });
				await box.getByText(state.words).waitFor({ timeout: 15_000 });
				const chips = await box.locator('[data-file-chip]').count();
				const text = await box.innerText();
				expect(chips === 1, `the attachment came back as one chip (found ${chips})`);
				expect(!/@\/\S*uploads\//.test(text), `no raw upload path in the composer: ${JSON.stringify(text)}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
