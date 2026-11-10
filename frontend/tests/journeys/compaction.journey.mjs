// Journey: a Claude Code chat compacts. The dev instance's fake `claude` CLI
// prints each compaction the way Claude Code 2.1.312 does (replay fixtures in
// scripts/devloop/fake_claude/compaction/, see
// docs/internal/claude-code-compaction-protocol.md), including the replay of
// the whole compacted conversation as fresh frames after the boundary.
//
// What the chat must show: the compaction once (its "Context Compacted" card;
// a compaction mid-turn folds into that turn's work log, whose summary says
// it compacted the context), no history row shown twice after the replay (the
// prompts, the answers), the context meter not above where it was, the next
// turn answered, and, after a reload, the same chat and rows straight back.
//
// Cases:
//   auto        two big reads, then [script:compact]: Claude Code compacts on
//               its own mid-turn and Fluid answers (no "compacting" status)
//   manual      two big reads, then a typed /compact
//   after-tool  one big read, then [script:compact-after-tool]: the compaction
//               falls between a tool's result and the model call that reads it
//   message-before-compact
//               a message typed while a tool runs, then an automatic compaction
//               at once: the fake's next model call can still read it (#996)
//   reload-after-auto, reload-after-manual
//               the chat of `auto` or `manual`, built in setup, then reloaded
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { readdir, readFile } from 'node:fs/promises';
import {
	PROJECT,
	countUserRows,
	expect,
	gotoApp,
	pick,
	pickAccess,
	pickProject,
	rowIds,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled,
	watchRowFlicker
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { markTime, spans } from './lib/timeline.mjs';

const CLAUDE_AGENT = 'Dev (Claude Code)';
const READ_REPLY = 'Read 45,000 characters.';
const FOLDED = /^Show work details: .*compacted the context/i;

const cards = (page) => page.locator('[data-testid="compaction-card"]');
const meter = (page) => page.getByRole('button', { name: /^Context and usage — / }).first();

/** The meter's words: on the composer on a wide screen, a row in Chat controls on a phone. */
async function meterLabel(page) {
	const controls = page.getByRole('button', { name: /^All chat controls/ });
	if (!(await controls.isVisible())) return (await meter(page).getAttribute('aria-label')) || '';
	const sheet = page.getByRole('dialog', { name: 'Chat controls' });
	await controls.click();
	await sheet.waitFor({ timeout: 10_000 });
	const row = sheet.getByRole('button', { name: /^Context and usage: / });
	const label = (await row.getAttribute('aria-label')) || '';
	// The row's own "tokens of window" line, so the log names the window (#1362).
	const tokens = (await row.locator('small').allTextContents()).join(' ').trim();
	await page.keyboard.press('Escape');
	await sheet.waitFor({ state: 'hidden', timeout: 5000 });
	return tokens ? `${label} (${tokens})` : label;
}

/** The context meter's reading as a number (percent, or tokens when the window is unknown). */
async function meterReading(page) {
	const label = await meterLabel(page);
	const percent = label.match(/(\d+)% (?:of context window )?used/);
	if (percent) return { label, value: Number(percent[1]) };
	const tokens = label.match(/([\d.,]+)\s*([kKmM]?) tokens/);
	if (!tokens) return { label, value: NaN };
	const scale = { k: 1e3, m: 1e6 }[tokens[2].toLowerCase()] || 1;
	return { label, value: Number(tokens[1].replace(/,/g, '')) * scale };
}

/**
 * Wait until the meter reads below `before` (or, `atMost`, not above it: a
 * turn that adds as much as its compaction removes; `above`: higher, as after
 * a read). It updates from the session's polls, so it can lag a turn.
 */
async function waitMeter(page, before, { atMost = false, above = false, timeout = 30_000 } = {}) {
	const end = Date.now() + timeout;
	// No reading yet (a new chat shows no meter): anything counts as up.
	const floor = Number.isNaN(before.value) ? -Infinity : before.value;
	const ok = (value) => (above ? value > floor : value < floor || (atMost && value <= floor));
	let now = await meterReading(page);
	while (Date.now() < end) {
		if (ok(now.value)) return now;
		await page.waitForTimeout(500);
		now = await meterReading(page);
	}
	const what = above ? 'go up after a read' : 'go down after the compaction';
	throw new Error(`the context meter did not ${what}: "${before.label}" then "${now.label}" (${await meterEvidence(page)})`);
}

/**
 * What the meter was measured against, for a failed wait's message (#1362,
 * #1363: a reading of 2% where the fake's smallest context is 6% of the
 * Standard window). The server's own answer for the open chat (its tokens and
 * window, or why it has none) and the composer's saved window pick.
 */
async function meterEvidence(page) {
	try {
		return await page.evaluate(async () => {
			const id = JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId;
			const pick = `saved window pick ${localStorage.getItem('aihub_lastContextWindowChoice') ?? 'none'}`;
			if (typeof id !== 'string') return `no open chat, ${pick}`;
			const res = await fetch(`/api/v1/sessions/${encodeURIComponent(id)}/context-usage`, { credentials: 'same-origin' });
			if (!res.ok) return `the server's context usage: HTTP ${res.status}, ${pick}`;
			const body = await res.json();
			const usage = body.usage || {};
			const answer = body.available
				? `${body.source}, ${usage.totalTokens} of ${usage.maxTokens} tokens (${usage.autocompactSource ?? 'no'} window source)`
				: `unavailable (${body.reason})`;
			return `the server's context usage: ${answer}, ${pick}`;
		});
	} catch (error) {
		return `the server's context usage could not be read: ${String(error).split('\n')[0]}`;
	}
}

/** Fresh Claude Code chat on the new-session screen, Dev sandbox, Bypass. */
async function openClaudeChat(ctx) {
	const { page, status } = ctx;
	if (!status.seed?.claude_cli?.agent_id) {
		throw new Error(`the dev instance has no Claude Code agent (seed: ${JSON.stringify(status.seed?.claude_cli)})`);
	}
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New chat' });
	await hero.waitFor();
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: CLAUDE_AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${CLAUDE_AGENT}` })
	);
	await pickProject(page, hero, PROJECT);
	await pickAccess(page, 'Bypass');
	await page.waitForTimeout(800);
}

/**
 * The compaction shows once: one card on the page, or one folded work log
 * that names it and, opened, holds the one card.
 */
async function expectOneCompaction(page) {
	const folded = page.getByRole('button', { name: FOLDED });
	const logs = await folded.count();
	const shown = await cards(page).count();
	expect(logs + shown === 1, `the compaction shows once (${shown} card(s), ${logs} folded work log(s) naming it)`);
	if (logs === 1) {
		await folded.click();
		await cards(page).first().waitFor({ timeout: 10_000 });
		const inside = await cards(page).count();
		expect(inside === 1, `the opened work log holds one compaction card (${inside})`);
		await page.getByRole('button', { name: /^Hide work details/ }).first().click();
		await cards(page).first().waitFor({ state: 'detached', timeout: 10_000 }).catch(() => {});
	}
}

/** Every prompt once, every read's answer once, no row twice. */
async function expectNoReplayedHistory(page, prompts, reads) {
	for (const prompt of prompts) {
		const n = await countUserRows(page, prompt);
		expect(n === 1, `"${prompt}" shows once as the user's message after the replay (${n})`);
	}
	const ids = await rowIds(page);
	const repeated = ids.filter((id, i) => ids.indexOf(id) !== i);
	expect(repeated.length === 0, `no transcript row twice: ${JSON.stringify(repeated.slice(0, 5))}`);
	const replies = await page.getByText(READ_REPLY, { exact: true }).count();
	expect(replies === reads, `each read's answer shows once (${replies} of ${reads})`);
}

/**
 * Build a compacted chat and judge it: the reads, the compaction, the follow-up.
 * With `mark` a no-op this is staging (a reload case's unrecorded setup).
 */
async function compactAndFollow(page, { reads, compact, settledText, meterAtMost = false }, { mark, log }) {
	const token = uniq();
	const prompts = [];
	let messages = 0;
	let before = await meterReading(page);
	for (let i = 0; i < reads; i++) {
		const prompt = `read ${i + 1} ${token}`;
		prompts.push(prompt);
		await mark(`read ${i + 1}`);
		await typeAndSend(page, `${prompt} [script:bulk-read]`);
		messages += 2;
		await waitTurnSettled(page, { text: READ_REPLY, messagesAtLeast: messages });
		// The meter has caught up with this read before the next step reads it.
		before = await waitMeter(page, before, { above: true });
	}
	log(`meter before: ${before.label}`);

	await mark('compact');
	const flicker = await watchRowFlicker(page);
	const compactPrompt = compact(token);
	await typeAndSend(page, compactPrompt);
	await waitTurnSettled(page, { text: settledText, messagesAtLeast: messages });
	const after = await waitMeter(page, before, { atMost: meterAtMost });
	log(`meter after: ${after.label}`);
	const back = await flicker();
	expect(back.length === 0, `rows left the page and came back while it compacted: ${JSON.stringify(back)}`);
	if (!compactPrompt.startsWith('/')) prompts.push(compactPrompt.split(' [')[0]);
	await expectNoReplayedHistory(page, prompts, reads);

	await mark('open the compaction');
	await expectOneCompaction(page);

	await mark('follow-up turn');
	const follow = `after it ${token}`;
	await typeAndSend(page, follow);
	await waitTurnSettled(page, { text: `You said: “${follow}”`, messagesAtLeast: messages + 2 });
	prompts.push(follow);
	await expectNoReplayedHistory(page, prompts, reads);
	return { prompts, reads, messages, follow, rows: await rowIds(page) };
}

function compactionCase(name, spec) {
	return {
		name,
		setup: openClaudeChat,
		act: ({ page, mark, log }) => compactAndFollow(page, spec, { mark, log })
	};
}

/** A chat compacted (in setup, unrecorded) and then reloaded: the same chat comes back, as it was. */
function reloadCase(name, spec) {
	return {
		name,
		// No layout-shift allowance: the reloaded project chat's composer and its
		// one-line chip row stay put while the chips fill in (#818).
		async setup(ctx) {
			await openClaudeChat(ctx);
			ctx.built = await compactAndFollow(ctx.page, spec, { mark: async () => {}, log: () => {} });
			await ctx.page.waitForTimeout(800);
		},
		async act({ page, mark, built }) {
			await mark('reload');
			await page.reload({ waitUntil: 'domcontentloaded' });
			// A new step once the recorder has seen the old page go (as in
			// reload-midstream): the reload itself is not the app going backwards.
			await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await mark('page reloaded');
			await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: built.messages }, { timeout: 60_000 });
			await page.getByText(`You said: “${built.follow}”`, { exact: false }).last().waitFor({ timeout: 30_000 });
			await waitTurnSettled(page, { messagesAtLeast: built.messages, hold: 2500 });
			await expectNoReplayedHistory(page, built.prompts, built.reads);
			const reloaded = await rowIds(page);
			expect(
				reloaded.length === built.rows.length,
				`the reload shows the same rows (${built.rows.length} before, ${reloaded.length} after)`
			);

			await mark('open the compaction after the reload');
			await expectOneCompaction(page);
		},
		verify({ recording }) {
			// The reloaded page goes straight to this chat: never the new-chat screen.
			const from = markTime(recording, 'page reloaded');
			const detour = spans(recording, (s) => s.surface === 'new' || s.composer === 'centre', { from });
			expect(detour.length === 0, `the reload showed the new-chat screen first: ${JSON.stringify(detour)}`);
		}
	};
}

// Read only this throwaway fake CLI's transcripts, never an installed worker.
async function scratchCopies(status, token) {
	const root = status.scratch_dir;
	expect(root && status.claude_cli?.mode === 'fake', 'scratch probe requires the isolated fake CLI');
	async function scan(dir) {
		const rows = [];
		for (const entry of await readdir(dir, { withFileTypes: true })) {
			const path = join(dir, entry.name);
			if (entry.isDirectory()) rows.push(...await scan(path));
			else if (entry.name.endsWith('.jsonl')) {
				for (const line of (await readFile(path, 'utf8')).split('\n').filter(Boolean)) {
					let row;
					try { row = JSON.parse(line); } catch { continue; }
					if (row.type !== 'user' || !row.message) continue;
					const text = JSON.stringify(row.message.content);
					if (text.includes(token)) rows.push((text.match(/# Harness scratch/g) || []).length);
				}
			}
		}
		return rows;
	}
	return scan(root);
}

const scratchCase = {
	name: 'scratch-once-per-segment',
	setup: openClaudeChat,
	async act({ page, status, mark, log }) {
		const token = `scratch-${uniq()}`;
		for (let i = 0; i < 3; i++) {
			await mark(`scratch turn ${i}`);
			const prompt = `${token}-${i}`;
			await typeAndSend(page, prompt);
			await waitTurnSettled(page, { text: `You said: “${prompt}”`, messagesAtLeast: 2 * (i + 1) });
		}
		let copies = await scratchCopies(status, token);
		expect(JSON.stringify(copies) === '[1,0,0]', `one scratch block per CLI segment, got ${JSON.stringify(copies)}`);
		// Manual compaction correctly skips a tiny echo-only conversation. As
		// in the existing manual-compaction cases, create reclaimable output
		// through the real tool path before asking it to fold this segment.
		for (let i = 0; i < 2; i++) {
			await mark(`grow scratch segment ${i}`);
			await typeAndSend(page, `read scratch fixture ${i} [script:bulk-read]`);
			await waitTurnSettled(page, { text: READ_REPLY, messagesAtLeast: 8 + 2 * i });
		}
		await mark('compact scratch segment');
		await typeAndSend(page, '/compact');
		await waitTurnSettled(page, { text: 'Context was compacted manually' });
		const follow = `${token}-after-compact`;
		await typeAndSend(page, follow);
		await waitTurnSettled(page, { text: `You said: “${follow}”` });
		copies = await scratchCopies(status, token);
		// SessionStart restores scratch; the following user turn must not repeat it.
		expect(copies.at(-1) === 0, `scratch was already restored by the compaction hook: ${JSON.stringify(copies)}`);
		log(`scratch user-turn copies: ${JSON.stringify(copies)}`);
	}
};

const AUTO = {
	reads: 2,
	compact: (token) => `carry on ${token} [script:compact]`,
	settledText: 'The conversation was compacted mid-turn and I carried on.'
};
const MANUAL = { reads: 2, compact: () => '/compact', settledText: 'Context was compacted manually' };

/**
 * #996: a message delivered on a tool result, then the compaction, with no
 * assistant turn between. What the fake's newest model call could read decides.
 */
// The message rides a tool result, then the conversation compacts at once. With a Fluid
// answer the row keeps it; Claude Code's own summarizer does not, and the app must repeat it.
const messageBeforeCompact = (name, script) => ({
	name,
	setup: openClaudeChat,
	async act({ page, status, mark, log }) {
		const token = uniq();
		const message = `Use the new table ${token}`;
		await mark('read');
		await typeAndSend(page, `read ${token} [script:bulk-read]`);
		await waitTurnSettled(page, { text: READ_REPLY, messagesAtLeast: 2 });

		await mark('send a message while the tool runs');
		await typeAndSend(page, `carry on ${token} [script:${script}]`);
		await page.getByText('Working through the long file.').first().waitFor({ timeout: 30_000 });
		await typeAndSend(page, message);
		await waitTurnSettled(page, { text: 'Compacted right after that tool result.', messagesAtLeast: 2 });
		await expectOneCompaction(page);

		await mark('follow-up turn');
		const follow = `after it ${token}`;
		await typeAndSend(page, follow);
		await waitTurnSettled(page, { text: `You said: “${follow}”`, messagesAtLeast: 4 });

		const seen = readFileSync(join(status.scratch_dir, 'fake-claude-context.txt'), 'utf8');
		log(`context the model can read: ${seen.length} characters`);
		expect(seen.includes(message), 'the message is still in what the model can read');
		const n = await countUserRows(page, message);
		expect(n <= 1, `the message shows at most once as a user's row (${n})`);
	}
});

await runJourney({
	name: 'compaction',
	cases: [
		scratchCase,
		compactionCase('auto', AUTO),
		compactionCase('manual', MANUAL),
		compactionCase('after-tool', {
			reads: 1,
			compact: (token) => `one more ${token} [script:compact-after-tool]`,
			settledText: 'Compacted after reading it; the result is still here.',
			// The turn reads another 45,000 characters before it compacts.
			meterAtMost: true
		}),
		messageBeforeCompact('message-before-compact', 'message-then-compact'),
		messageBeforeCompact('message-before-summarizer', 'message-then-summarize'),
		reloadCase('reload-after-auto', AUTO),
		reloadCase('reload-after-manual', MANUAL)
	],
	allow: [...DEV_INSTANCE_NOISE]
});
