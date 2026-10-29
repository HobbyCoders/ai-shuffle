// Journey (two real workers): move a chat to the other worker and back.
//
// Owner moves are where the "which worker owns this chat" rule lives in
// several places (the session row, the catalog mirror, the runtime summary,
// the UI's runner chip); fixes to one have undone another. Each case starts a
// fresh chat, moves it from the runner chip ("Continue here" pulls it to the
// worker the page is on; a worker's name pushes it there) and back, and sends
// on each new owner. Both workers' APIs are polled throughout: each must see
// the owner switch exactly once per move (no flip back and forth), and the
// transcript must survive intact (every message once, no duplicate rows).
//
// Cases (desktop; the phone draws the runner chip in its Chat controls sheet):
//   no-project        a chat started on worker B, pulled to A ("Continue
//                     here"), then pushed back to B
//   worktree-handoff  a Dev sandbox chat in its own worktree started on A,
//                     handed to B (commit, push, B fetches the branch into its
//                     checkout, cloned by the app's self-seed), then pulled back
import { runJourney } from './runner.mjs';
import { chatState, expect, gotoApp, openNewChat, pickAccess, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled, watchUserText } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PAIR_NOISE, duplicateIds, expectOwner, memberApi, transcriptOn, userMessagesWith, watchOwners } from './lib/pair.mjs';

/** Per-case state (act and verify get separate ctx copies). */
let state = {};

const runnerChip = (page, name) => page.getByRole('toolbar', { name: 'Session context' }).getByRole('button', { name: new RegExp(`^Runner: ${name}`) });

/** Start a chat on `member` (its own UI, in a second tab) and return its id. */
async function startChatOn(ctx, member, { project = null } = {}) {
	const page = await ctx.context.newPage();
	try {
		await openNewChat({ page, status: member.status }, { project });
		await pickAccess(page, 'Bypass');
		const token = `made on ${member.role.toUpperCase()} ${uniq()}`;
		await typeAndSend(page, token);
		await waitTurnSettled(page, { messagesAtLeast: 2 });
		const session = (await chatState(page))?.session || '';
		const rows = await memberApi(ctx.context, member, 'GET', '/sessions?limit=20');
		const row = rows.find((r) => (r.title || '').includes(token)) || rows.sort((x, y) => Date.parse(y.created_at) - Date.parse(x.created_at))[0];
		if (!row) throw new Error(`no chat found on worker ${member.role.toUpperCase()} after starting one (${session})`);
		return { id: row.id, token };
	} finally {
		await page.close();
	}
}

async function sendAndSettle(page, text) {
	const before = Number((await chatState(page))?.messages || 0);
	const seen = await watchUserText(page, text);
	await typeAndSend(page, text);
	await waitTurnSettled(page, { messagesAtLeast: before + 2, timeout: 120_000 });
	const { max } = await seen();
	expect(max === 1, `"${text}" showed ${max} times on some frame (want 1)`);
}

/** Pick a row of the runner chip's menu, confirm a Git move when asked, wait for the new chip. */
async function moveWithChip(page, from, row, to) {
	await runnerChip(page, from).click();
	const menu = page.getByRole('menu', { name: 'Move this session' });
	await menu.waitFor({ timeout: 15_000 });
	await menu.getByRole('menuitemradio', { name: new RegExp(`^${row}`) }).click();
	// A chat with a worktree asks first ("Commit, push and move"); one without
	// moves at once. Wait for whichever comes (isVisible does not wait).
	const confirm = page.getByRole('button', { name: /^Commit, push and (move|continue)$/ });
	// Each wait settles on its own (the loser times out later, quietly).
	const asked = await Promise.race([
		confirm.waitFor({ timeout: 30_000 }).then(() => true, () => false),
		runnerChip(page, to).waitFor({ timeout: 30_000 }).then(() => false, () => false)
	]);
	if (asked) await confirm.click();
	try {
		await runnerChip(page, to).waitFor({ timeout: 180_000 });
	} catch (error) {
		const alert = await page.getByRole('alert').allTextContents().catch(() => []);
		throw new Error(`the chat never showed "Runner: ${to}" after "${row}" (alerts: ${JSON.stringify(alert)}): ${error.message.split('\n')[0]}`);
	}
}

/** Every owner each worker reported, in order, must be exactly `roles` (one switch per move). */
function expectOwnerSequence(ctx, seen, roles) {
	const want = roles.map((role) => ctx.pair[role].deviceId);
	for (const role of ['a', 'b']) {
		const got = seen[role];
		if (JSON.stringify(got) !== JSON.stringify(want)) {
			const name = (id) => (id === ctx.pair.a.deviceId ? 'A' : id === ctx.pair.b.deviceId ? 'B' : id);
			throw new Error(`worker ${role.toUpperCase()} saw the owner go ${got.map(name).join(' → ') || 'nowhere'} (want ${roles.map((r) => r.toUpperCase()).join(' → ')})`);
		}
	}
}

async function expectTranscriptIntact(ctx, chat, tokens) {
	for (const member of [ctx.pair.a, ctx.pair.b]) {
		let transcript = [];
		const deadline = Date.now() + 60_000;
		for (;;) {
			transcript = await transcriptOn(ctx, member, chat);
			if (tokens.every((token) => userMessagesWith(transcript, token) === 1) || Date.now() > deadline) break;
			await new Promise((r) => setTimeout(r, 500));
		}
		const dupes = duplicateIds(transcript);
		if (dupes.length) throw new Error(`worker ${member.role.toUpperCase()} serves duplicate rows: ${dupes.join(', ')}`);
		for (const token of tokens) {
			const n = userMessagesWith(transcript, token);
			if (n !== 1) throw new Error(`worker ${member.role.toUpperCase()} has "${token}" ${n} times (want 1)`);
		}
	}
}

function moveCase({ name, origin, project, there, back, knownBug }) {
	return {
		name,
		viewports: ['desktop'],
		throttles: ['fast'],
		knownBug,
		async setup(ctx) {
			const { page, status, pair } = ctx;
			const chat = await startChatOn(ctx, pair[origin], { project });
			await expectOwner(ctx, chat.id, origin);
			state = { chat: chat.id, tokens: [chat.token] };
			await gotoApp(page, status, `/?session=${encodeURIComponent(chat.id)}`);
			await waitChatOpen(page, { messagesAtLeast: 2 });
			await pickAccess(page, 'Bypass');
		},
		async act(ctx) {
			const { page, mark } = ctx;
			state.stopWatch = watchOwners(ctx, state.chat);
			await mark(there.step);
			await moveWithChip(page, there.from, there.row, there.to);
			await waitChat(page, { surface: 'active', loading: 'false' });
			const one = `on the new owner ${uniq()}`;
			await mark('send on the new owner');
			await sendAndSettle(page, one);
			state.tokens.push(one);
			await mark(back.step);
			await moveWithChip(page, back.from, back.row, back.to);
			await waitChat(page, { surface: 'active', loading: 'false' });
			const two = `back on the first owner ${uniq()}`;
			await mark('send after moving back');
			await sendAndSettle(page, two);
			state.tokens.push(two);
		},
		async verify(ctx) {
			await expectOwner(ctx, state.chat, origin);
			const seen = await state.stopWatch();
			expectOwnerSequence(ctx, seen, [origin, origin === 'a' ? 'b' : 'a', origin]);
			await expectTranscriptIntact(ctx, state.chat, state.tokens);
		}
	};
}

await runJourney({
	name: 'two-worker-move-chat',
	pair: true,
	cases: [
		moveCase({
			name: 'no-project',
			origin: 'b',
			project: null,
			there: { step: 'continue the chat here (worker A)', from: 'Dev worker B', row: 'Continue here', to: 'Local' },
			back: { step: 'move it back to worker B', from: 'Local', row: 'Dev worker B', to: 'Dev worker B' },
			knownBug: {
				issue: 820,
				error: /^Error: worker A has "back on the first owner [a-z0-9]+" 0 times \(want 1\)$/,
				reason: "the worker a chat left serves its own stale transcript over the REST API"
			}
		}),
		moveCase({
			name: 'worktree-handoff',
			origin: 'a',
			project: 'Dev sandbox',
			there: { step: 'hand the chat and its worktree to worker B', from: 'Local', row: 'Dev worker B', to: 'Dev worker B' },
			back: { step: 'continue it here again (worker A)', from: 'Dev worker B', row: 'Continue here', to: 'Local' },
			knownBug: {
				issue: 820,
				error: /^Error: worker B has "back on the first owner [a-z0-9]+" 0 times \(want 1\)$/,
				reason: "the worker a chat left serves its own stale transcript over the REST API"
			}
		})
	],
	allow: [...DEV_INSTANCE_NOISE, ...PAIR_NOISE]
});
