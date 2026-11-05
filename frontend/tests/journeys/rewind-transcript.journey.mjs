// Journey: rewinding a message in a Dev (Claude Code) chat with no project,
// recorded through the rewind itself. The transcript goes straight from before
// to after: the removed rows leave once, the rest stay on screen, nothing
// reloads and the removed words come back to the composer. The chat then takes
// the next message. (`claude-rewind-cwd` judges what the CLI resumes afterwards;
// this one judges what the screen does during the rewind.)
//
// Hosted journeys build only the shared desktop UI and run it at desktop and
// phone widths. `--mobile-shell` opens the phone variants in the separately
// built production /mobile/ shell (npm run build, app.preview stamp,
// devloop up --no-vite).
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import {
	chatState,
	chatIdByToken,
	composer,
	countText,
	expect,
	gotoApp,
	pick,
	scriptTag,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { frames, markTime, spans } from './lib/timeline.mjs';

// A deterministic refused write on the private database, not a mocked API reply.
const REWIND_DATABASE = String.raw`
import hashlib, json, os, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert home.name == 'data' and home.parent.name.startswith('ai-shuffle-preview-')
assert (home / '.branch-preview').exists() or (home.parent / '.branch-preview').exists()
os.environ['DATA_DIR'] = str(home)
from app.core.config import settings
assert settings.effective_data_dir.resolve() == home
import psycopg2
from psycopg2.extras import RealDictCursor
chat, stage = sys.argv[2:4]
with psycopg2.connect(**settings.get_database_connection()) as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT sdk_session_id, outcome_baseline_failed, outcome_baseline_order FROM sessions WHERE id=%s', (chat,))
        session = dict(cur.fetchone())
        sdk = session.pop('sdk_session_id')
        cur.execute('SELECT head_node_id, cli_session_id FROM fluid_sessions WHERE session_id=%s', (sdk,))
        fluid = dict(cur.fetchone())
        cli = fluid.get('cli_session_id')
        cur.execute('SELECT id, role, content FROM session_messages WHERE session_id=%s ORDER BY id', (chat,))
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute('SELECT COUNT(*) AS n FROM session_events WHERE session_id=%s', (chat,))
        events = cur.fetchone()['n']
        files = sorted(home.parent.rglob(cli + '.jsonl')) if cli else []
        assert files, 'real CLI must have produced a transcript'
        snapshot = {'session': session, 'fluid': fluid, 'rows': rows, 'events': events,
                    'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        if stage == 'refuse':
            cur.execute("""CREATE FUNCTION journey_refuse_rewind() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.id = TG_ARGV[0] THEN RAISE EXCEPTION 'journey rewind write refused'; END IF;
                RETURN NEW; END $$""")
            cur.execute("""CREATE TRIGGER journey_refuse_rewind BEFORE UPDATE OF outcome_baseline_order, outcome_baseline_failed
                ON sessions FOR EACH ROW EXECUTE FUNCTION journey_refuse_rewind(%s)""", (chat,))
        if stage == 'release':
            cur.execute('DROP TRIGGER journey_refuse_rewind ON sessions')
            cur.execute('DROP FUNCTION journey_refuse_rewind()')
print(json.dumps(snapshot, default=str))
`;

function rewindDatabase(status, sessionId, stage) {
	const local = join(status.checkout, '.venv', 'bin', 'python');
	const python = existsSync(local) ? local : (process.env.PYTHON || 'python3');
	return JSON.parse(execFileSync(python, ['-c', REWIND_DATABASE, status.data_dir, sessionId, stage], {
		cwd: status.checkout,
		env: { ...process.env, PYTHONPATH: join(status.checkout, 'fluid'), PYTHONDONTWRITEBYTECODE: '1' },
		encoding: 'utf8'
	}));
}

const mobileShell = process.argv.includes('--mobile-shell');
const REWIND = 'rewind';
const NEXT = 'send after rewind';
const WARNING = 'The model may still recall the removed turns.';

async function startClaudeChat({ page, status, variant }) {
	expect(status.seed?.claude_cli?.agent_id, 'fake Claude Code is seeded');
	if (mobileShell && variant.viewport === 'phone') {
		await page.goto(new URL('/mobile/', status.urls.ui).toString());
		expect(new URL(page.url()).pathname === '/mobile/', 'the opt-in case must exercise the real mobile shell');
	} else {
		await gotoApp(page, status);
	}
	await waitChat(page, { surface: 'new' });
	const hero = page.getByRole('region', { name: 'New session' });
	const menu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		menu,
		menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
		hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' })
	);
	expect(await hero.getByRole('button', { name: /^No project/ }).isVisible(), 'fresh chat has no project');
}

/**
 * `rewind` is a long reply of fixed length. `expectShift` budgets the move a
 * rewind makes for that turn height, and `default` lists every scenario, so its
 * height follows whatever scenarios exist. A short reply is not covered here.
 */
async function sendTurn(page, variant, words, messagesAtLeast) {
	await typeAndSend(page, `${words} ${scriptTag(variant, 'rewind')}`);
	await waitTurnSettled(page, { messagesAtLeast, text: `You said: “${words}` });
}

async function stageTwoTurns(ctx) {
	await startClaudeChat(ctx);
	const retained = `retained-${uniq()}`;
	const removed = `removed-${uniq()}`;
	await sendTurn(ctx.page, ctx.variant, retained, 2);
	await sendTurn(ctx.page, ctx.variant, removed, 4);
	ctx.state = { retained, removed };
}

/**
 * The browser excuses a layout shift for 500ms after input, so a quick server
 * hides the move a rewind makes from the recording and a slower one (any phone
 * on a slow connection) shows it. Hold every rewind past that grace: the move is
 * then measured on every run, not only when the machine happens to be slow.
 */
const PAST_INPUT_GRACE_MS = 700;

/**
 * The rows a rewind removes leave when the server answers, which is not input on
 * this page by then: the rows that stay close up and the composer refills. It
 * happens once, in the rewind's own step, and a wipe on the way fails it.
 */
const rowsLeave = (from, to) => [
	{
		step: REWIND,
		messages: [from, to],
		maxValue: 0.12,
		reason: 'the rows the owner asked to remove leave when the server answers: what stays closes up once'
	}
];

/** Rewind one user message (default the last) from its own row; answers once the server has. */
async function rewindUserMessage(page, nth = -1) {
	await page.route(
		'**/rewind/execute/**',
		async (route) => {
			await new Promise((resolve) => setTimeout(resolve, PAST_INPUT_GRACE_MS));
			await route.fallback();
		},
		{ times: 1 }
	);
	// Keyboard: the hover-only action row disappears as a pointer crosses the bubble.
	await page.getByTitle('Rewind to before this message', { exact: true }).nth(nth).focus();
	await page.keyboard.press('Enter');
	const answered = page.waitForResponse(
		(r) => r.url().includes('/rewind/execute/') && r.request().method() === 'POST'
	);
	await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
	const result = await (await answered).json();
	expect(result.success && result.chat_rewound, `rewind succeeded: ${JSON.stringify(result)}`);
	await page.getByRole('dialog').waitFor({ state: 'hidden' });
}

/** What the rewind left on screen, and that the chat still answers. */
async function expectRewound(page, variant, { removed, retained, messages }) {
	await waitChat(page, { messages: String(messages), loading: 'false', streaming: 'false' }, { timeout: 15_000 });
	await page.waitForTimeout(1500);
	const state = await chatState(page);
	expect(state.messages === String(messages), `still ${messages} messages 1.5s later, not ${state.messages}`);
	expect((await composer(page).innerText()).includes(removed), 'the removed words came back to the composer');
	expect((await countText(page, `You said: “${removed}`)) === 0, 'the removed reply left the transcript');
	if (retained) expect((await countText(page, `You said: “${retained}`)) === 1, 'the earlier reply stayed');
}

/**
 * Over every painted frame from the rewind until the next send: no reload
 * (loading stays false), the count only falls and goes straight to its final
 * value, and the surface never reads anything but `surfaces`, in that order.
 */
function expectOneStepRewind(recording, { from, to, surfaces }) {
	const start = markTime(recording, REWIND);
	const end = markTime(recording, NEXT);
	// Each frame is the state after a change, so the state the rewind began from
	// is the last one before its mark.
	const all = frames(recording);
	const first = all.findIndex(({ t }) => t >= start);
	const window = all.slice(Math.max(first - 1, 0)).filter(({ t }) => t < end);
	const loading = spans(recording, (s) => s.loading === 'true', { from: start }).filter((s) => s.start < end);
	expect(loading.length === 0, `the chat reloaded during the rewind: ${JSON.stringify(loading)}`);
	const counts = window.map(({ state }) => state.messages).filter((n) => n !== undefined);
	const distinct = counts.filter((n, i) => i === 0 || n !== counts[i - 1]);
	expect(
		JSON.stringify(distinct) === JSON.stringify([from, to]),
		`messages went ${distinct.join(' -> ')}, expected ${from} -> ${to}`
	);
	const seen = window.map(({ state }) => state.surface).filter((s, i, all) => s && s !== all[i - 1]);
	expect(
		JSON.stringify(seen) === JSON.stringify(surfaces),
		`surface went ${seen.join(' -> ')}, expected ${surfaces.join(' -> ')}`
	);
}

/**
 * A half-open feed: from arm() on, the feed connection already open delivers
 * nothing more and never errors, as on a dead network path. A later connection
 * works, so only something that reconnects can bring the rewind in.
 */
function installStallableFeed() {
	const realFetch = window.fetch;
	const stall = (window.__feedStall = { connections: 0, stalled: -1, arm() { this.stalled = this.connections; } });
	window.fetch = async (input, init) => {
		const res = await realFetch(input, init);
		const url = typeof input === 'string' ? input : input?.url || String(input);
		if (!res.body || !/\/sessions\/[^/]+\/events\?/.test(url)) return res;
		const connection = ++stall.connections;
		const reader = res.body.getReader();
		const parked = () => new Promise(() => {});
		const body = new ReadableStream({
			start(controller) {
				init?.signal?.addEventListener('abort', () => {
					try { controller.error(new DOMException('Aborted', 'AbortError')); } catch { /* already closed */ }
				});
			},
			async pull(controller) {
				if (stall.stalled === connection) return parked();
				const { done, value } = await reader.read();
				if (stall.stalled === connection) return parked();
				if (done) controller.close();
				else controller.enqueue(value);
			}
		});
		return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
	};
}

await runJourney({
	name: 'rewind-transcript',
	cases: [
		{
			name: 'refused-write',
			async setup(ctx) {
				await stageTwoTurns(ctx);
				ctx.state.sessionId = await chatIdByToken(ctx.page, ctx.state.retained);
			},
			async act({ page, status, mark, log, state }) {
				const before = rewindDatabase(status, state.sessionId, 'refuse');
				try {
					await mark('refused rewind');
					await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
					await page.keyboard.press('Enter');
					const answered = page.waitForResponse(r => r.url().includes('/rewind/execute/') && r.request().method() === 'POST');
					await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
					const response = await answered;
					expect(response.status() === 200, `refused transaction must return a structured response, not HTTP ${response.status()}`);
					const result = await response.json();
					expect(!result.success && !result.chat_rewound && result.messages_removed === 0, 'refused transaction reports no removal');
					expect(result.error?.includes('journey rewind write refused'), 'the database refusal reaches the user');
					await page.getByRole('dialog').getByText(/journey rewind write refused/).waitFor();
					await waitChat(page, { surface: 'active', messages: '4', loading: 'false' });
					const after = rewindDatabase(status, state.sessionId, 'snapshot');
					expect(JSON.stringify(after) === JSON.stringify(before), 'head, legacy rows, CLI bytes, events and outcome baseline remain intact');
					log('Real CLI-produced transcript: refused reset rolled back with intact head, rows, events and CLI bytes; structured error rendered in dialog.');
				} finally {
					rewindDatabase(status, state.sessionId, 'release');
				}
			},
			async verify({ page, status, state, log }) {
				// After the recording, retry the same dialog against the recovered DB.
				const answered = page.waitForResponse(r => r.url().includes('/rewind/execute/') && r.request().method() === 'POST');
				await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
				const result = await (await answered).json();
				expect(result.success && result.chat_rewound, 'retry succeeds after removing the refusal');
				await page.getByRole('dialog').waitFor({ state: 'hidden' });
				await waitChat(page, { messages: '2', loading: 'false' });
				expect((await composer(page).innerText()).includes(state.removed), 'retry restores the removed prompt');
				const after = rewindDatabase(status, state.sessionId, 'snapshot');
				expect(after.rows.length === 1, 'only retained legacy user row remains after retry');
				log('Recovered database: same UI request rewinds successfully and restores the prompt.');
			}
		},
		{
			name: 'latest-turn',
			expectShift: rowsLeave(4, 2),
			async setup(ctx) {
				await stageTwoTurns(ctx);
			},
			async act({ page, variant, mark, state }) {
				await mark(REWIND);
				await rewindUserMessage(page);
				await expectRewound(page, variant, { ...state, messages: 2 });
				await mark(NEXT);
				await sendTurn(page, variant, `next-${uniq()}`, 4);
			},
			verify({ recording }) {
				expectOneStepRewind(recording, { from: 4, to: 2, surfaces: ['active'] });
			}
		},
		{
			// Everything after the target goes in the same step, not row by row.
			name: 'earlier-turn',
			expectShift: rowsLeave(6, 2),
			async setup(ctx) {
				await stageTwoTurns(ctx);
				const later = `later-${uniq()}`;
				await sendTurn(ctx.page, ctx.variant, later, 6);
				ctx.state.later = later;
			},
			async act({ page, variant, mark, state }) {
				await mark(REWIND);
				await rewindUserMessage(page, 1);
				await expectRewound(page, variant, { ...state, messages: 2 });
				expect((await countText(page, `You said: “${state.later}`)) === 0, 'the later reply left too');
				await mark(NEXT);
				await sendTurn(page, variant, `next-${uniq()}`, 4);
			},
			verify({ recording }) {
				expectOneStepRewind(recording, { from: 6, to: 2, surfaces: ['active'] });
			}
		},
		{
			// Rewinding the only turn empties the chat: the one transcript change
			// that may move the composer, and it may do so once.
			name: 'only-turn',
			expectShift: [
				{
					step: REWIND,
					composer: ['bottom', 'centre'],
					maxValue: 0.15,
					reason: 'the rewind empties the chat when the server answers: the composer moves once to the centred cleared layout'
				}
			],
			async setup(ctx) {
				await startClaudeChat(ctx);
				const removed = `removed-${uniq()}`;
				await sendTurn(ctx.page, ctx.variant, removed, 2);
				ctx.state = { removed };
			},
			async act({ page, variant, mark, state }) {
				await mark(REWIND);
				await rewindUserMessage(page);
				await expectRewound(page, variant, { ...state, messages: 0 });
				await mark(NEXT);
				await sendTurn(page, variant, `next-${uniq()}`, 2);
			},
			verify({ recording }) {
				expectOneStepRewind(recording, { from: 2, to: 0, surfaces: ['active', 'cleared'] });
			}
		},
		{
			// The reload this replaces also repaired a dead feed. The rewind's own
			// answer must still bring the truncation in, well before the feed's
			// own 45s stall watchdog would.
			name: 'stalled-feed',
			expectShift: rowsLeave(4, 2),
			async setup(ctx) {
				await ctx.page.addInitScript(installStallableFeed);
				await stageTwoTurns(ctx);
			},
			async act({ page, variant, mark, state }) {
				await page.evaluate(() => window.__feedStall.arm());
				await mark(REWIND);
				await rewindUserMessage(page);
				await expectRewound(page, variant, { ...state, messages: 2 });
				await mark(NEXT);
				await sendTurn(page, variant, `next-${uniq()}`, 4);
			},
			verify({ recording }) {
				expectOneStepRewind(recording, { from: 4, to: 2, surfaces: ['active'] });
			}
		},
		{
			// A rewind that partly failed says so only in its answer (the CLI's
			// history could not be re-seeded, say), which nothing in the event log
			// repeats. Controlled: the warning is added to the real server's answer,
			// because this dev instance's rewind has nothing to warn about.
			name: 'warning',
			expectShift: rowsLeave(4, 2),
			throttles: ['fast'],
			async setup(ctx) {
				await stageTwoTurns(ctx);
				await ctx.page.route('**/rewind/execute/**', async (route) => {
					const response = await route.fetch();
					await route.fulfill({ response, json: { ...(await response.json()), warning: WARNING } });
				});
			},
			async act({ page, variant, mark, state }) {
				await mark(REWIND);
				await rewindUserMessage(page);
				await expectRewound(page, variant, { ...state, messages: 2 });
				expect(await page.getByText(WARNING).first().isVisible(), 'the warning is on screen after the rewind');
				await mark(NEXT);
				await sendTurn(page, variant, `next-${uniq()}`, 4);
			},
			verify({ recording }) {
				expectOneStepRewind(recording, { from: 4, to: 2, surfaces: ['active'] });
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
