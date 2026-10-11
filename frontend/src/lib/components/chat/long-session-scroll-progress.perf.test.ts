/**
 * Lab benchmark + ratchet for #1282: a `subagent.progress` update in a long
 * session must cost the card it touches, not the whole history.
 *
 * The owner's session (2026-10-11 snapshot) reduced to 4,127 messages: 178
 * user prompts, 2,596 tool cards, 828 assistant prose rows, 112 thinking rows,
 * 326 system rows and 87 subagent cards holding 13,564 retained children.
 * Progress arrived in bursts of up to 24 events/s. Before this fix every update
 * rebuilt the whole transcript model and re-lexed every historical prose row
 * for the media gallery (7.996 ms p75 on the real shape, gallery excluded).
 *
 * The synthetic transcript below has the same shape at three history sizes.
 * Each update runs the real path the feed controller and MessageArea run:
 * `releaseMessages` → `reduce(subagent.progress)` → the cached model builder
 * (work-log grouping + media-gallery extraction with the real `marked` lexer).
 *
 * Guardrails, in order of how deterministic they are:
 * 1. Operation counts: historical Markdown lexes and display items whose
 *    identity changed per update. These do not depend on the machine and must
 *    stay independent of history length.
 * 2. Wall clock: warm p75 against a generous budget, so the test catches a
 *    return to history-wide work (≈10× over) without flaking on a slow runner.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { marked } from 'marked';
import type { ChatMessage, SubagentChildMessage } from '$lib/stores/types';
import { initialState, reduce, releaseMessages, type V2SessionState, type WireEvent } from '$lib/stores/session/v2/reducer';
import { createTranscriptModelBuilder, type BuildOptions, type DisplayItem, type TranscriptModel } from '$lib/utils/work-log';

/** Deterministic PRNG (mulberry32) so every run builds the same transcript. */
function rng(seed: number) {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

const TOOLS = ['Read', 'Read', 'Read', 'Bash', 'Bash', 'Grep', 'Glob', 'Edit', 'Write', 'WebFetch'];

const PROSE = [
	'Now checking the workflow.',
	'I found the cause.\n\nThe reducer copies the card and the builder rebuilds **every** turn:\n\n- `work-log.ts` groups the whole history\n- `media-gallery.ts` lexes every answer\n\nSee [the issue](https://github.com/HobbyCoders/ai-shuffle/issues/1282).',
	'## Result\n\n| Case | p75 |\n|---|---|\n| before | 8 ms |\n| after | 0.1 ms |\n\n```ts\nconst model = build(messages, options);\n```',
	'Done. The screenshots are below:\n\n![before](/api/v1/files/before.png)\n![after](/api/v1/files/after.png)',
	'Should I also bound the mounted window?'
];

interface Shape {
	messages: number;
	users: number;
	tools: number;
	prose: number;
	thinking: number;
	system: number;
	subagents: number;
	children: number;
}

/** The owner's reduced session, scaled to `messages` rows. */
function shape(messages: number): Shape {
	const k = messages / 4127;
	return {
		messages,
		users: Math.max(2, Math.round(178 * k)),
		tools: Math.round(2596 * k),
		prose: Math.round(828 * k),
		thinking: Math.round(112 * k),
		system: Math.round(326 * k),
		subagents: Math.max(2, Math.round(87 * k)),
		children: Math.round(13564 * k)
	};
}

interface Fixture {
	state: V2SessionState;
	/** Task id of the running worker in the latest (parked) turn. */
	liveTask: string;
	/** Task id of a running worker launched by an early, otherwise settled turn. */
	historicalTask: string;
	options: BuildOptions;
}

function children(count: number, task: string, next: () => number): SubagentChildMessage[] {
	return Array.from({ length: count }, (_, i): SubagentChildMessage => {
		const r = next();
		if (r < 0.6) {
			return {
				id: `${task}-c${i}`,
				type: 'tool_use',
				content: '',
				toolName: TOOLS[i % TOOLS.length],
				toolId: `${task}-tool${i}`,
				toolInput: { file_path: `/src/lib/file-${i}.ts` },
				toolResult: 'x'.repeat(200 + Math.floor(r * 800)),
				toolStatus: 'complete'
			};
		}
		return { id: `${task}-c${i}`, type: 'text', content: `Worker note ${i}: ${'y'.repeat(80)}` };
	});
}

/**
 * Build a transcript of the owner's shape. Rows are dealt round-robin into
 * turns so every turn carries tools, prose and system rows; subagent cards
 * land in evenly spaced turns, with the last one in the latest turn.
 */
function buildFixture(size: number): Fixture {
	const s = shape(size);
	const next = rng(1282 + size);
	const turns = s.users;
	const perTurn: ChatMessage[][] = Array.from({ length: turns }, () => []);
	const t0 = Date.parse('2026-10-10T12:00:00.000Z');
	let clock = t0;
	const stamp = () => new Date((clock += 1500)).toISOString();

	// Non-user rows in a fixed mixed order, dealt to turns in contiguous runs.
	const kinds: string[] = [];
	for (let i = 0; i < s.tools; i++) kinds.push('tool');
	for (let i = 0; i < s.prose; i++) kinds.push('prose');
	for (let i = 0; i < s.thinking; i++) kinds.push('thinking');
	for (let i = 0; i < s.system; i++) kinds.push('system');
	for (let i = kinds.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[kinds[i], kinds[j]] = [kinds[j], kinds[i]];
	}
	const rowsPerTurn = Math.ceil(kinds.length / turns);
	let row = 0;
	for (let turn = 0; turn < turns; turn++) {
		const turnId = `turn-${turn}`;
		perTurn[turn].push({
			id: `user-${turn}`,
			role: 'user',
			type: 'text',
			content: `Please look at item ${turn} and report back.`,
			turnId,
			timestamp: stamp()
		});
		const slice = kinds.slice(turn * rowsPerTurn, (turn + 1) * rowsPerTurn);
		for (const kind of slice) {
			const id = `row-${row++}`;
			if (kind === 'tool') {
				const name = TOOLS[row % TOOLS.length];
				perTurn[turn].push({
					id,
					role: 'assistant',
					type: 'tool_use',
					content: '',
					toolName: name,
					toolId: `tu-${id}`,
					toolInput: { file_path: `/repo/src/${id}.ts`, command: 'npm test' },
					toolResult: 'ok '.repeat(50 + (row % 200)),
					toolStatus: row % 97 === 0 ? 'error' : 'complete',
					turnId,
					timestamp: stamp()
				});
			} else if (kind === 'prose') {
				perTurn[turn].push({
					id,
					role: 'assistant',
					type: 'text',
					content: PROSE[row % PROSE.length],
					turnId,
					timestamp: stamp()
				});
			} else if (kind === 'thinking') {
				perTurn[turn].push({
					id,
					role: 'assistant',
					type: 'thinking',
					content: '**Planning**\nLooking at the next file.',
					turnId,
					timestamp: stamp()
				});
			} else {
				perTurn[turn].push({
					id,
					role: 'system',
					type: 'system',
					content: 'Task progress',
					systemSubtype: row % 3 === 0 ? 'task_notification' : 'task_progress',
					turnId,
					timestamp: stamp()
				});
			}
		}
		// Every turn ends on an answer.
		perTurn[turn].push({
			id: `answer-${turn}`,
			role: 'assistant',
			type: 'text',
			content: PROSE[(turn + 1) % PROSE.length],
			turnId,
			timestamp: stamp(),
			metadata: { duration_ms: 60_000 + turn }
		});
	}

	// Subagent cards: evenly spaced, the last one inside the latest turn.
	const perAgent = Math.round(s.children / s.subagents);
	const agentTurns = Array.from({ length: s.subagents }, (_, i) =>
		Math.min(turns - 1, Math.round((i * (turns - 1)) / (s.subagents - 1)))
	);
	const historicalIndex = Math.max(1, Math.floor(s.subagents / 4));
	agentTurns.forEach((turn, i) => {
		const task = `task-${i}`;
		const running = i === s.subagents - 1 || i === historicalIndex;
		const card: ChatMessage = {
			id: `subagent-${task}`,
			role: 'assistant',
			type: 'subagent',
			content: '',
			toolId: task,
			agentName: `worker ${i}`,
			agentDescription: `Investigate part ${i}`,
			agentStatus: running ? 'running' : 'completed',
			agentChildren: children(perAgent, task, next),
			turnId: `turn-${turn}`,
			timestamp: stamp()
		};
		const rows = perTurn[turn];
		rows.splice(Math.max(1, rows.length - 1), 0, card);
	});

	const state = initialState();
	state.messages = perTurn.flat();
	for (const message of state.messages) state.messageOriginSeq.set(message.id, 1);
	const liveTask = `task-${s.subagents - 1}`;
	const historicalTask = `task-${historicalIndex}`;
	return {
		state,
		liveTask,
		historicalTask,
		options: {
			isStreaming: false,
			// Parked turn with background work: the latest turn stays live.
			latestTurnUnsettled: true,
			runningToolIds: new Set([liveTask, historicalTask]),
			groupingEnabled: true,
			sessionId: 'perf-session',
			swarms: []
		}
	};
}

let seq = 1_000_000;
function progress(task: string, i: number): WireEvent {
	seq += 1;
	// The real mix: mostly tool calls and their results, some streamed text.
	const kind = i % 3 === 0 ? 'tool_use' : i % 3 === 1 ? 'tool_result' : 'chunk';
	return {
		type: 'subagent.progress',
		id: `progress-${seq}`,
		seq,
		session_id: 'perf-session',
		created_at: '2026-10-11T15:00:00.000Z',
		task_id: task,
		kind,
		note: kind === 'chunk' ? 'working… ' : undefined,
		payload:
			kind === 'chunk'
				? {}
				: { name: 'Read', tool_use_id: `${task}-p${i - (kind === 'tool_result' ? 1 : 0)}`, input: { file_path: '/a.ts' }, output: 'ok' }
	} as WireEvent;
}

/** A main-lane tool call landing in the live turn (not a worker update). */
function toolUse(i: number): WireEvent {
	seq += 1;
	return {
		type: 'tool.use',
		id: `tool-${seq}`,
		seq,
		session_id: 'perf-session',
		created_at: '2026-10-11T15:00:00.000Z',
		tool_use_id: `main-tool-${seq}`,
		name: 'Read',
		input: { file_path: `/repo/src/new-${i}.ts` }
	} as WireEvent;
}

function changedItems(before: DisplayItem[], after: DisplayItem[]): number {
	const kept = new Set(before);
	let changed = 0;
	for (const item of after) if (!kept.has(item)) changed++;
	return changed;
}

function p75(samples: number[]): number {
	const sorted = [...samples].sort((a, b) => a - b);
	return sorted[Math.floor(sorted.length * 0.75)];
}

interface Result {
	messages: number;
	items: number;
	p75Ms: number;
	/** marked.lexer calls per update, averaged over measured updates. */
	lexesPerUpdate: number;
	/** Display items whose identity changed per update (max over measured). */
	maxChangedItems: number;
}

type Target = 'live' | 'historical' | 'append';

function run(size: number, target: Target, warmup = 30, measured = 120): Result {
	const fixture = buildFixture(size);
	const task = target === 'historical' ? fixture.historicalTask : fixture.liveTask;
	const build = createTranscriptModelBuilder();
	let state = fixture.state;
	let model: TranscriptModel = build(releaseMessages(state), fixture.options);
	const lexer = vi.spyOn(marked, 'lexer');
	const samples: number[] = [];
	let lexes = 0;
	let maxChanged = 0;
	for (let i = 0; i < warmup + measured; i++) {
		lexer.mockClear();
		const event = target === 'append' ? toolUse(i) : progress(task, i);
		const start = performance.now();
		state = reduce(state, event);
		const next = build(releaseMessages(state), fixture.options);
		const elapsed = performance.now() - start;
		if (i >= warmup) {
			samples.push(elapsed);
			lexes += lexer.mock.calls.length;
			maxChanged = Math.max(maxChanged, changedItems(model.items, next.items));
		}
		model = next;
	}
	lexer.mockRestore();
	return {
		messages: state.messages.length,
		items: model.items.length,
		p75Ms: p75(samples),
		lexesPerUpdate: lexes / measured,
		maxChangedItems: maxChanged
	};
}

// Generous on purpose: a shared CI runner is several times slower than a
// desktop and this must never flake. The pre-fix builder measured ≈10× this
// at the 4k shape in the same harness (see the PR for the table).
const P75_BUDGET_MS = 1.5;

describe('long-session subagent progress (#1282)', () => {
	afterEach(() => vi.restoreAllMocks());

	it('builds the owner-shaped transcript', () => {
		const s = buildFixture(4127).state.messages;
		const count = (pred: (m: ChatMessage) => boolean) => s.filter(pred).length;
		expect(s.length).toBeGreaterThan(4000);
		expect(count((m) => m.type === 'tool_use')).toBe(2596);
		expect(count((m) => m.type === 'subagent')).toBe(87);
		expect(count((m) => m.role === 'user')).toBe(178);
		const retained = s.reduce((n, m) => n + (m.agentChildren?.length ?? 0), 0);
		expect(retained).toBeGreaterThan(13_000);
	});

	for (const target of ['live', 'historical'] as const) {
		it(`a ${target}-turn worker update touches only its own card's group, at every history size`, () => {
			const results = [400, 4127, 8000].map((size) => run(size, target, 10, 40));
			// eslint-disable-next-line no-console
			console.info(`[perf #1282] ${target}`, JSON.stringify(results));
			for (const result of results) {
				// No historical prose is re-lexed for the media gallery.
				expect(result.lexesPerUpdate).toBe(0);
				// Only the group holding the card gets a new identity.
				expect(result.maxChangedItems).toBeLessThanOrEqual(1);
			}
		});
	}

	it('a new main-lane row rebuilds only the live turn: no historical lexing, one changed group', () => {
		const results = [400, 4127, 8000].map((size) => run(size, 'append', 10, 40));
		// eslint-disable-next-line no-console
		console.info('[perf #1282] append', JSON.stringify(results));
		for (const result of results) {
			expect(result.lexesPerUpdate).toBe(0);
			expect(result.maxChangedItems).toBeLessThanOrEqual(1);
		}
	});

	it('keeps a progress update under budget at the 4k shape (warm p75)', () => {
		const result = run(4127, 'live');
		// eslint-disable-next-line no-console
		console.info('[perf #1282] p75', JSON.stringify(result));
		expect(result.p75Ms).toBeLessThan(P75_BUDGET_MS);
	});
});
