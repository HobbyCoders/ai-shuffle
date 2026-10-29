// Journey (two real workers): send, on worker A, in a chat worker B owns.
//
// A forwards the turn to its owner (app/network/control.py); B runs it on the
// fake AI and the reply comes back to A through the transcript mirror. The
// user sees their message once, the reply streams in and settles, and the
// composer sends again. Server-side, the turn ran on B: B's own transcript
// has it, ownership did not move, and neither worker serves a row twice.
import { fakeAiTag, runJourney } from './runner.mjs';
import { gotoApp, openFromSidebar, typeAndSend, uniq, waitChatOpen, waitTurnSettled, watchUserText, chatState, expect } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PAIR_NOISE, PAIR_SEEDED, duplicateIds, expectOwner, pairChatId, transcriptOn, userMessagesWith } from './lib/pair.mjs';

const chatOf = (ctx) => pairChatId(ctx.pair.b, 'b_loose');
/** The messages the running case sent (act and verify get separate ctx copies). */
let sent = [];

/** Send `text` and wait until the echo has settled; returns the highest count of the message seen. */
async function sendAndSettle(page, text) {
	const before = Number((await chatState(page))?.messages || 0);
	const seen = await watchUserText(page, text);
	await typeAndSend(page, text);
	// The fake AI echoes what was written: the reply carries the token too.
	await waitTurnSettled(page, { messagesAtLeast: before + 2, timeout: 120_000 });
	return seen();
}

/** B's own transcript has the message exactly once (the turn ran there). */
async function expectRanOnB(ctx, chat, token) {
	const deadline = Date.now() + 60_000;
	let count = 0;
	while (Date.now() < deadline) {
		count = userMessagesWith(await transcriptOn(ctx, ctx.pair.b, chat), token);
		if (count === 1) return;
		await new Promise((r) => setTimeout(r, 500));
	}
	throw new Error(`expectation failed: worker B's transcript has the message ${count} times (want 1)`);
}

await runJourney({
	name: 'two-worker-remote-send',
	pair: true,
	cases: [
		{
			name: 'send-twice',
			async setup({ page, status }) {
				sent = [];
				await gotoApp(page, status);
				await openFromSidebar(page, PAIR_SEEDED.bLoose);
				await waitChatOpen(page, { messagesAtLeast: 12 });
			},
			async act(ctx) {
				const { page, mark, variant } = ctx;
				const first = `remote ${uniq()}`;
				await mark('send a message in the chat worker B owns');
				const once = await sendAndSettle(page, `${first} ${fakeAiTag(variant)}`.trim());
				expect(once.max === 1, `the message showed ${once.max} times on some frame (want 1)`);
				sent.push(first);
				const second = `again ${uniq()}`;
				await mark('send a second message');
				const twice = await sendAndSettle(page, second);
				expect(twice.max === 1, `the second message showed ${twice.max} times on some frame (want 1)`);
				sent.push(second);
			},
			async verify(ctx) {
				const chat = chatOf(ctx);
				for (const token of sent) await expectRanOnB(ctx, chat, token);
				await expectOwner(ctx, chat, 'b');
				for (const member of [ctx.pair.a, ctx.pair.b]) {
					const transcript = await transcriptOn(ctx, member, chat);
					const dupes = duplicateIds(transcript);
					if (dupes.length) throw new Error(`worker ${member.role.toUpperCase()} serves duplicate rows: ${dupes.join(', ')}`);
					for (const token of sent) {
						const n = userMessagesWith(transcript, token);
						if (n !== 1) throw new Error(`worker ${member.role.toUpperCase()} has "${token}" ${n} times (want 1)`);
					}
				}
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE, ...PAIR_NOISE]
});
