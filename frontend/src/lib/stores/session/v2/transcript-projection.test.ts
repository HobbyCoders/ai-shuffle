import { describe, expect, it } from 'vitest';
import { reduceAll, withLogEpoch, type WireEvent } from './reducer';
import type { ShuffleMobileRuntime } from '$lib/api/runtime';
import {
	foldProjection, materializeProjection, projectionReferences, ProjectionUnsupported,
	PROJECTION_LIMITS, type ProjectionRow
} from './transcript-projection';

function makeEvents() {
	let seq = 0;
	return (type: string, payload: Record<string, unknown> = {}): WireEvent => ({
		id: `frame-${++seq}`, seq, type, session_id: 'session',
		created_at: '2026-10-30T00:00:00.000Z', ...payload
	});
}

/** An in-memory durable-store adapter: exactly the public projection protocol,
 * including bounded alias lookup and atomic stable-checkpoint publication.
 */
class Store {
	base: string | null = null;
	preview: string | null = null;
	tail: WireEvent[] = [];
	history: WireEvent[] = [];
	rows = new Map<string, ProjectionRow>();
	append(events: WireEvent[]) {
		this.history.push(...events);
		this.tail.push(...events);
		const keys = new Set(projectionReferences(this.base, this.tail));
		const loaded = [...this.rows.values()].filter(row => row.aliases.some(alias => keys.has(alias)));
		const result = foldProjection(this.base, this.tail, loaded);
		if (result.boundary) {
			for (const id of result.boundary.removed) this.rows.delete(id);
			for (const row of result.boundary.upserts) this.rows.set(row.id, row);
			this.base = result.boundary.carry;
			this.tail = this.tail.filter(event => event.seq > result.boundary!.lastSeq);
		}
		this.preview = result.preview.carry;
		return result;
	}
	assertFullParity() {
		const expected = withLogEpoch(reduceAll(this.history), 'source-epoch');
		const actual = materializeProjection(this.preview!, [...this.rows.values()], 'source-epoch');
		expect(actual).toStrictEqual(expected);
		expect([...actual.messageOriginSeq]).toStrictEqual([...expected.messageOriginSeq]);
		expect(Object.keys(actual)).toStrictEqual(Object.keys(expected));
		expect(actual.messages.map(Object.keys)).toStrictEqual(expected.messages.map(Object.keys));
		return actual;
	}
}

function ordinaryHistory(event: ReturnType<typeof makeEvents>, turns = 80): WireEvent[] {
	const result: WireEvent[] = [];
	for (let i = 0; i < turns; i++) result.push(
		event('turn.started', { turn_id: `turn-${i}`, profile_id: 'profile' }),
		event('message.user', { content: `Prompt ${i}`, command_id: `command-${i}` }),
		event('assistant.text_delta', { text: `Answer ${i}` }),
		event('turn.completed', { usage: { tokens_out: 3 } })
	);
	return result;
}

function ingest(store: Store, events: WireEvent[]) {
	for (let start = 0; start < events.length; start += 24) {
		store.append(events.slice(start, start + 24));
		store.assertFullParity();
	}
}

describe('dark transcript projection protocol', () => {
	it('keeps exact full state and origin order at every cut while sealing detached history', () => {
		const event = makeEvents(), store = new Store();
		ingest(store, ordinaryHistory(event));
		expect(store.rows.size).toBeGreaterThan(0);
		expect(store.rows.size).toBeLessThan(PROJECTION_LIMITS.referencedRows);
		store.assertFullParity();
	});

	it('replays the bounded mutable attempt from its base, without duplicating text', () => {
		const event = makeEvents(), store = new Store();
		store.append([event('message.user', { content: 'First' })]);
		store.append([event('assistant.text_delta', { text: 'hello' })]);
		store.assertFullParity();
		store.append([event('assistant.text_delta', { text: ' world' })]);
		expect(store.assertFullParity().messages.at(-1)?.content).toBe('hello world');
		store.append([event('turn.completed')]);
		store.assertFullParity();
	});

	it('applies chronological settlement, rewind and redismissal to unloaded old prose', () => {
		const event = makeEvents(), store = new Store();
		ingest(store, ordinaryHistory(event));
		store.append([event('system.note', { subtype: 'session_settled', through_seq: 320 })]);
		store.assertFullParity();
		store.append([event('transcript.rewound', { from_seq: 300 })]);
		store.assertFullParity();
		const redismiss = event('system.note', { subtype: 'session_settled', through_seq: 320 });
		store.append([redismiss]);
		const actual = store.assertFullParity();
		expect(actual.messages.filter(message => message.role === 'assistant')
			.every(message => message.proseQuestionDismissedAt === redismiss.seq)).toBe(true);
	});

	it('stays exact when rewind retains only messages older than the loaded window', () => {
		const event = makeEvents(), store = new Store();
		ingest(store, ordinaryHistory(event));
		store.append([event('transcript.rewound', { from_seq: 30 })]);
		store.assertFullParity();
	});

	it('retains background cards before the window and handles park then real terminal delivery', () => {
		const event = makeEvents(), store = new Store();
		store.append([
			event('turn.started', { turn_id: 'launch' }),
			event('tool.use', { tool_use_id: 'launch-tool', name: 'Task', input: { prompt: 'Work' } }),
			event('subagent.started', { task_id: 'task', tool_use_id: 'launch-tool', description: 'Background' }),
			event('task.updated', { task_id: 'task', tool_use_id: 'launch-tool', state: 'running' }),
			event('turn.completed', { turn_id: 'launch' }),
			event('turn.parked', { turn_id: 'launch', task_ids: ['task'] })
		]);
		store.assertFullParity();
		ingest(store, ordinaryHistory(event, 70));
		store.append([event('subagent.progress', { task_id: 'task', kind: 'text', note: 'More work' })]);
		store.assertFullParity();
		store.append([event('task.updated', { task_id: 'task', tool_use_id: 'launch-tool', state: 'completed' })]);
		store.assertFullParity();
	});

	it('hydrates a named old tool card for late result updates rather than losing it', () => {
		const event = makeEvents(), store = new Store();
		store.append([
			event('message.user', { content: 'Run' }),
			event('tool.use', { tool_use_id: 'tool-old', name: 'Bash', input: { command: 'pwd' } }),
			event('tool.result', { tool_use_id: 'tool-old', content: 'first' }),
			event('turn.completed')
		]);
		ingest(store, ordinaryHistory(event, 70));
		expect([...store.rows.values()].some(row => row.aliases.includes('tool-old'))).toBe(true);
		store.append([event('tool.result', { tool_use_id: 'tool-old', content: 'late result' })]);
		store.assertFullParity();
	});

	it('materializes native phone artifact scope through the same full-serving helper', () => {
		const event = makeEvents(), store = new Store(), hash = 'a'.repeat(64);
		store.append([
			event('message.user', { content: 'Capture' }),
			event('tool.use', { tool_use_id: 'screen', name: 'browser_screenshot', input: {} }),
			event('tool.result', { tool_use_id: 'screen', content: 'Captured', artifact_hashes: [hash] }),
			event('turn.completed')
		]);
		store.assertFullParity();
		const mobile: ShuffleMobileRuntime = { apiOrigin: 'https://phone-worker.example', token: 'fixture-not-a-key' };
		window.__SHUFFLE_MOBILE__ = mobile;
		try {
			const full = withLogEpoch(reduceAll(store.history), 'source-epoch');
			const actual = materializeProjection(store.preview!, [...store.rows.values()], 'source-epoch', {
				sessionId: 'session', apiRoot: `${mobile.apiOrigin}/api/v1`
			});
			expect(actual).toStrictEqual(full);
			expect(actual.messages.some(message => message.toolScreenshot?.startsWith(mobile.apiOrigin))).toBe(true);
			expect(store.preview).not.toContain(mobile.apiOrigin);
			expect(store.preview).not.toContain(mobile.token);
		} finally { delete window.__SHUFFLE_MOBILE__; }
	});

	it('rejects oversized or non-monotonic work instead of clipping or changing canonical state', () => {
		const event = makeEvents();
		expect(() => foldProjection(null, Array.from({ length: PROJECTION_LIMITS.events + 1 },
			() => event('future.event')))).toThrow('event_limit');
		const first = foldProjection(null, [event('message.user', { content: 'A' })]);
		expect(() => foldProjection(first.preview.carry, [{ id: 'old', seq: 1, type: 'future.event' }]))
			.toThrow('non_monotonic_input');
		expect(() => foldProjection(null, [{ id: 'bad', seq: -1, type: 'future.event' }]))
			.toThrow(ProjectionUnsupported);
	});
});
