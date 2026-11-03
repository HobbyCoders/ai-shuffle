// Journey: a native (Dev agent) chat carries the "# Harness scratch" reminder
// once, and the model keeps seeing it after the conversation is compacted.
// It used to ride every user message, so history held one more copy per turn.
//
// The evidence is the fake AI's request log: what each model request really
// carried, through the real composer, adapter, native loop and compactor. The
// rule has no UI of its own, so one desktop, fast variant is enough.
//
// Case:
//   once-then-compact  twelve large turns (exactly one copy in every request),
//                      a typed /compact that folds the turn that carried it,
//                      and a follow-up that still shows exactly one copy, with
//                      no second one written after the compaction. A manual
//                      compaction stops at its target with a known window, so
//                      the history has to be this large for anything to fold.
import { readFile } from 'node:fs/promises';
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const HEADER = '# Harness scratch';
const TURNS = 12;
const FILLER = 'history '.repeat(2500);

const textOf = (content) =>
	typeof content === 'string' ? content : JSON.stringify(content ?? '');

/** The body's conversation as text entries, whichever dialect the provider speaks. */
function userTexts(body) {
	const rows = body.messages || body.input || [];
	return (Array.isArray(rows) ? rows : [])
		.filter((row) => row && row.role === 'user')
		.map((row) => textOf(row.content));
}

/**
 * This instance's agent requests whose newest user message holds `needle`, oldest
 * first. Reads only the throwaway fake AI's own log. A title or checkpoint side
 * call offers no tools, so it is not counted.
 */
async function requestsFor(status, needle) {
	const path = status.fake_ai?.request_log;
	expect(path, 'the scratch probe needs the fake AI request log');
	const rows = [];
	for (const line of (await readFile(path, 'utf8')).split('\n')) {
		if (!line.trim()) continue;
		let entry;
		try {
			entry = JSON.parse(line);
		} catch {
			continue;
		}
		const body = entry.body;
		if (entry.method !== 'POST' || !body || !(body.tools || []).length) continue;
		const users = userTexts(body);
		if (users.length && users[users.length - 1].includes(needle)) rows.push(users);
	}
	return rows;
}

const copies = (users) => users.join('\n').split(HEADER).length - 1;

async function expectOneCopy(status, prompt, when) {
	const requests = await requestsFor(status, prompt);
	expect(requests.length > 0, `no model request carried "${prompt}" (${when})`);
	const counts = requests.map(copies);
	expect(
		counts.every((n) => n === 1),
		`every request shows exactly one scratch block ${when}, got ${JSON.stringify(counts)}`
	);
	return requests[requests.length - 1];
}

await runJourney({
	name: 'native-scratch-context',
	viewports: ['desktop'],
	throttles: ['fast'],
	allow: [...DEV_INSTANCE_NOISE],
	cases: [
		{
			name: 'once-then-compact',
			async setup(ctx) {
				await openNewChat(ctx, { project: PROJECT });
				await pickAccess(ctx.page, 'Bypass');
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, status, mark, log }) {
				const token = `scratch-${uniq()}`;
				for (let i = 0; i < TURNS; i++) {
					const prompt = `${token}-turn-${i} ${FILLER}`.trim();
					await mark(`turn ${i}`);
					await typeAndSend(page, prompt);
					await waitTurnSettled(page, { messagesAtLeast: 2 * (i + 1) });
					await expectOneCopy(status, `${token}-turn-${i} `, `after turn ${i}`);
				}

				await mark('compact');
				await typeAndSend(page, '/compact');
				await page.getByTestId('compaction-card').first().waitFor({ timeout: 60_000 });
				await waitTurnSettled(page, { messagesAtLeast: 2 * TURNS });

				await mark('follow-up');
				const follow = `${token}-after-compact`;
				await typeAndSend(page, follow);
				await waitTurnSettled(page, { text: `You said: “${follow}”`, messagesAtLeast: 2 * TURNS + 2 });
				const users = await expectOneCopy(status, follow, 'after the compaction');
				// The fold really happened, and its message is what kept the block:
				// the first turn's own text is no longer in the request verbatim.
				expect(
					!users.join('\n').includes(`${token}-turn-0 ${FILLER}`.trim()),
					'the compaction folded the turn that carried the scratch block'
				);
				log(`requests after compaction show ${copies(users)} scratch block over ${users.length} user messages`);
			}
		}
	]
});
