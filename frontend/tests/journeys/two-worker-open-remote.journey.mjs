// Journey (two real workers): open, on worker A, a chat worker B owns.
//
// Runs against `python -m scripts.devloop up --pair`: worker B's seeded chat
// reaches A through the real sync (catalog and transcript mirror), and A's
// UI runs the real mirror sync check against B. This replaces, for the real
// pair, the in-page simulation mirrored-chat.journey.mjs uses on one
// instance.
//
// Cases:
//   from-new            from the new-session screen, through the sidebar
//   from-another-chat   from one of A's own chats (#813: the sync pane used to
//                       take the transcript away and put it back)
// Each checks the transcript arrives once and never empties, the chat says it
// runs on Dev worker B, and both workers' APIs agree B owns it.
import { runJourney } from './runner.mjs';
import { SEEDED, gotoApp, isPhone, openFromSidebar, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PAIR_NOISE, PAIR_SEEDED, duplicateIds, expectOwner, pairChatId, transcriptOn } from './lib/pair.mjs';
import { frames, markTime } from './lib/timeline.mjs';

const OPEN = "open worker B's chat from the sidebar";
/** Worker B's seeded chat: six turns, two rows each. */
const B_ROWS = 12;

async function expectRunsOnB(page, variant) {
	if (isPhone(variant)) return; // the runner chip lives in the phone's Chat controls sheet
	await page
		.getByRole('toolbar', { name: 'Session context' })
		.getByText('Dev worker B', { exact: false })
		.first()
		.waitFor({ timeout: 30_000 });
}

async function act({ page, mark, variant }) {
	await mark(OPEN);
	await openFromSidebar(page, PAIR_SEEDED.bLoose);
	await waitChatOpen(page, { messagesAtLeast: B_ROWS });
	await page.getByText('Worker B answer 6.', { exact: false }).last().waitFor({ timeout: 30_000 });
	await expectRunsOnB(page, variant);
}

async function verify(ctx) {
	const { recording, pair } = ctx;
	// Once B's transcript has painted, it never goes away and comes back.
	const from = markTime(recording, OPEN);
	const shown = frames(recording).find(({ t, state }) => t >= from && Number(state.rows) >= B_ROWS);
	if (!shown) throw new Error(`no frame painted worker B's ${B_ROWS} rows`);
	const gone = frames(recording).find(({ t, state }) => t > shown.t && Number(state.rows) < B_ROWS);
	if (gone) throw new Error(`the transcript dropped to ${gone.state.rows} rows at ${gone.t}ms after it showed at ${shown.t}ms`);
	const pane = frames(recording).find(({ t, state }) => t > shown.t && Number(state.syncpane) > 0);
	if (pane) throw new Error(`a sync pane replaced the shown transcript at ${pane.t}ms`);
	const chat = pairChatId(pair.b, 'b_loose');
	await expectOwner(ctx, chat, 'b');
	for (const member of [pair.a, pair.b]) {
		const transcript = await transcriptOn(ctx, member, chat);
		const dupes = duplicateIds(transcript);
		if (dupes.length) throw new Error(`worker ${member.role.toUpperCase()} serves duplicate rows: ${dupes.join(', ')}`);
	}
}

await runJourney({
	name: 'two-worker-open-remote',
	pair: true,
	throttles: ['fast'],
	recorder: { selectors: ['rows=[data-ui-row]', 'syncpane=.mirror-sync'] },
	cases: [
		{
			name: 'from-new',
			async setup({ page, status }) {
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			act,
			verify
		},
		{
			name: 'from-another-chat',
			async setup({ page, status }) {
				await gotoApp(page, status);
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2 });
			},
			act,
			verify
		}
	],
	allow: [...DEV_INSTANCE_NOISE, ...PAIR_NOISE]
});
