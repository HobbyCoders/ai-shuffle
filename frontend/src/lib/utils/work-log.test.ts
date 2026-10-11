/**
 * Behavior tests for the transcript's turn grouping and settled disclosures.
 */
import { describe, it, expect, vi } from 'vitest';
import { createSettleHold, createTurnHandoffHold, SETTLE_HOLD_MS } from './settle-hold';
import type { ChatMessage } from '$lib/stores/types';
import type { SwarmSnapshot } from './swarm';
import producer from './__fixtures__/agent-delivery-producer.json';
import { deriveUserTurnNavigationItems } from './transcript-navigation';
import { deriveStatus, needsYouCount, openSessionNeeds } from '$lib/components/layout/session-card-data';
import { initialState, reduce, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { longTurn } from '$lib/components/chat/test-fixtures/long-turn';
import {
	buildDisplayItems,
	buildTranscriptModel,
	createTranscriptModelBuilder,
	hasLinkedSubagent,
	userPromptOccurrenceAt,
	formatDuration,
	summarizeWork,
	summarizeWorkCompact,
	turnDurationMs,
	turnWasInterrupted,
	parkedSendTurnId,
	latestTurnKey,
	isProtectedProse,
	type DisplayItem,
	type WorkLogEntry
} from './work-log';

describe('streaming model reuse', () => {
	it.each([60, 600, 1800])('does not reread settled fields for a tail delta after %i messages', count => {
		const build = createTranscriptModelBuilder();
		const history = Array.from({length: count}, (_, i) => text(`History ${i}`));
		const prompt = user('Continue');
		const tail = msg({type:'text', content:'Hello', streaming:true});
		const messages = [...history, prompt, tail];
		const options = {isStreaming:true, groupingEnabled:true};
		const before = build(messages, options);
		let reads = 0;
		for (const m of history) {
			const content = m.content;
			Object.defineProperty(m, 'content', {configurable:true, get:() => {reads++; return content;}});
		}
		const next = [...history, prompt, {...tail, content:'Hello world'}];
		const after = build(next, options);
		expect(reads).toBe(0);
		expect(after.items[0]).toBe(before.items[0]);
		// MessageArea's per-row deriveds rely on these keeping identity (#1282):
		// a new array would re-evaluate every user row on each streaming fold.
		expect(after.promptOccurrence).toBe(before.promptOccurrence);
		expect(after.rewindEligible).toBe(before.rewindEligible);
		expect(after).toEqual(buildTranscriptModel(next, options));
	});
	it('invalidates the fast path on options, session, Swarm and metadata changes', () => {
		const build = createTranscriptModelBuilder();
		const messages = [user('Question'), msg({type:'text',content:'Hello',streaming:true})];
		const options = {isStreaming:true, groupingEnabled:true, sessionId:'s1'};
		for (const patch of [{sessionId:'s2'}, {groupingEnabled:false}, {latestTurnUnsettled:false},
			{swarms:[{id:'workflow',session_id:'s1',tasks:[]} as unknown as SwarmSnapshot]}]) {
			const before = build(messages, options);
			const changed = {...options,...patch};
			const actual = build([...messages.slice(0,-1), {...messages.at(-1)!,content:'updated'}], changed);
			expect(actual).toEqual(buildTranscriptModel([...messages.slice(0,-1), {...messages.at(-1)!,content:'updated'}], changed));
			// The tail fast path never handles an options change. The per-turn
			// cache may still reuse an unchanged prompt row, except when the
			// session or grouping changes: those discard every cached turn.
			if ('sessionId' in patch || 'groupingEnabled' in patch) expect(actual.items[0]).not.toBe(before.items[0]);
			expect(actual.items.at(-1)).not.toBe(before.items.at(-1));
		}
		build(messages, options);
		const metadata = [...messages.slice(0,-1), {...messages.at(-1)!,metadata:{input_tokens:123}}];
		expect(build(metadata, options)).toEqual(buildTranscriptModel(metadata, options));
		expect(build(metadata, options).lastAssistantMetadata).toEqual({input_tokens:123});
	});
	it('rebuilds when a streaming text replacement becomes a hidden placeholder', () => {
		const build = createTranscriptModelBuilder();
		const prompt = user('Question');
		const tail = msg({type:'text', content:'Hello', streaming:true});
		const options = {isStreaming:true, groupingEnabled:true};
		build([prompt, tail], options);
		const next = [prompt, {...tail, content:'   '}];
		expect(build(next, options)).toEqual(buildTranscriptModel(next, options));
		expect(build(next, options).items).toHaveLength(1);
	});
	it('rebuilds for historical changes, metadata and settlement', () => {
		const build = createTranscriptModelBuilder();
		let messages = [user('Question'), tool(), msg({type:'text',content:'Hello',streaming:true})];
		let options = {isStreaming:true, groupingEnabled:true};
		build(messages, options);
		for (const patch of [{content:'changed'}, {metadata:{input_tokens:123}}, {streaming:false}]) {
			messages = messages.map((m,i) => i === 1 ? {...m,toolResult:'late result'} : i === 2 ? {...m,...patch} : m);
			expect(build(messages, options)).toEqual(buildTranscriptModel(messages, options));
		}
		options = {...options, isStreaming:false};
		expect(build(messages, options)).toEqual(buildTranscriptModel(messages, options));
	});
});

describe('incremental model reuse (#1282)', () => {
	let n = 0;
	const event = (type: string, fields: Record<string, unknown> = {}): WireEvent => {
		n += 1;
		return { type, id: `ev-${n}`, seq: n, session_id: 's1', created_at: new Date(Date.UTC(2026, 9, 11, 0, 0, n)).toISOString(), ...fields } as WireEvent;
	};
	/** Three settled turns with a worker each, then a parked fourth with a running worker. */
	function history() {
		const events: WireEvent[] = [];
		for (let t = 0; t < 4; t++) {
			events.push(event('turn.started', { turn_id: `turn-${t}` }));
			events.push(event('message.user', { content: `Prompt ${t}`, turn_id: `turn-${t}` }));
			for (let k = 0; k < 3; k++) {
				events.push(event('tool.use', { tool_use_id: `tu-${t}-${k}`, name: 'Read', input: { file_path: `/f${k}` } }));
				events.push(event('tool.result', { tool_use_id: `tu-${t}-${k}`, output: 'ok' }));
			}
			events.push(event('subagent.started', { task_id: `task-${t}`, description: `Worker ${t}` }));
			events.push(event('assistant.text_delta', { text: t % 2 ? `Answer ${t}\n\n![a](/api/a${t}.png) ![b](/api/b${t}.png)` : `Answer ${t}` }));
			if (t < 3) {
				events.push(event('subagent.completed', { task_id: `task-${t}`, result: 'done' }));
				events.push(event('turn.completed', {}));
			}
		}
		let state = initialState();
		for (const e of events) state = reduce(state, e);
		return state;
	}

	it('matches a fresh build after every mixed update, including option flips', () => {
		const build = createTranscriptModelBuilder();
		let state = history();
		let options: Parameters<typeof buildTranscriptModel>[1] = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 's1', runningToolIds: new Set(['task-3']) };
		const steps: Array<() => void> = [
			() => { state = reduce(state, event('subagent.progress', { task_id: 'task-3', kind: 'chunk', note: 'working' })); },
			() => { state = reduce(state, event('subagent.progress', { task_id: 'task-3', kind: 'tool_use', payload: { name: 'Read', tool_use_id: 'c1' } })); },
			// A late update on a settled turn's worker re-opens it.
			() => { state = reduce(state, event('subagent.progress', { task_id: 'task-1', kind: 'chunk', note: 'late' })); },
			() => { options = { ...options, runningToolIds: new Set(['task-3', 'task-1']) }; },
			() => { state = reduce(state, event('subagent.progress', { task_id: 'task-1', kind: 'tool_use', payload: { name: 'Bash', tool_use_id: 'c2' } })); },
			() => { state = reduce(state, event('tool.use', { tool_use_id: 'main-1', name: 'Grep', input: {} })); },
			() => { state = reduce(state, event('tool.result', { tool_use_id: 'main-1', output: 'hit' })); },
			() => { state = reduce(state, event('subagent.completed', { task_id: 'task-1', result: 'done' })); },
			() => { options = { ...options, runningToolIds: new Set(['task-3']) }; },
			() => { options = { ...options, groupingEnabled: false }; },
			() => { state = reduce(state, event('subagent.progress', { task_id: 'task-3', kind: 'chunk', note: 'more' })); },
			() => { options = { ...options, groupingEnabled: true }; },
			() => { state = reduce(state, event('subagent.completed', { task_id: 'task-3', result: 'all done' })); },
			() => { options = { ...options, latestTurnUnsettled: false, runningToolIds: new Set() }; },
			() => { state = reduce(state, event('message.user', { content: 'Next', turn_id: 'turn-4' })); },
			() => { options = { ...options, sessionId: 's2' }; },
		];
		expect(build(releaseMessages(state), options)).toEqual(buildTranscriptModel(state.messages, options));
		for (const step of steps) {
			step();
			expect(build(releaseMessages(state), options)).toEqual(buildTranscriptModel(state.messages, options));
		}
	});

	it('a worker update replaces only the group that holds its card', () => {
		const build = createTranscriptModelBuilder();
		let state = history();
		const options = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 's1' };
		const before = build(releaseMessages(state), options);
		state = reduce(state, event('subagent.progress', { task_id: 'task-3', kind: 'chunk', note: 'working' }));
		const after = build(releaseMessages(state), options);
		expect(after).toEqual(buildTranscriptModel(state.messages, options));
		const changed = after.items.filter((item, i) => item !== before.items[i]);
		expect(changed).toHaveLength(1);
		expect(changed[0].kind === 'work_log' && changed[0].sequence.some((e) => e.message.type === 'subagent' && e.message.agentChildren?.length)).toBe(true);
		expect(after.promptOccurrence).toBe(before.promptOccurrence);
		expect(after.subagentToolIds).toBe(before.subagentToolIds);
	});

	it('takes the full path when a card changes anything grouping reads', () => {
		const build = createTranscriptModelBuilder();
		const prompt = user('go', undefined, 't1');
		const card = subagent({ toolId: 'w1', agentStatus: 'running', turnId: 't1' });
		const answer = text('done');
		const options = { isStreaming: false, latestTurnUnsettled: false, groupingEnabled: true };
		build([prompt, card, answer], options);
		for (const patch of [{ metadata: { duration_ms: 5000 } }, { timestamp: '2026-10-11T00:00:00Z' }, { toolId: 'w2' }, { toolStatus: 'error' as const }]) {
			const next = [prompt, { ...card, ...patch, agentChildren: [{ id: 'c', type: 'text' as const, content: 'x' }] }, answer];
			expect(build(next, options)).toEqual(buildTranscriptModel(next, options));
		}
	});

	it('does not regroup or re-summarize unchanged settled turns when a row is appended', () => {
		const build = createTranscriptModelBuilder();
		let state = history();
		const options = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 's1' };
		const before = build(releaseMessages(state), options);
		state = reduce(state, event('tool.use', { tool_use_id: 'main-2', name: 'Read', input: {} }));
		const after = build(releaseMessages(state), options);
		expect(after).toEqual(buildTranscriptModel(state.messages, options));
		// Every settled turn's items keep identity; only the live turn's group is new.
		expect(after.items.filter((item) => !before.items.includes(item))).toHaveLength(1);
		expect(after.items.at(-1)).not.toBe(before.items.at(-1));
	});

	it('keeps plain-row identity when an ungrouped long turn grows, and matches a fresh build', () => {
		const build = createTranscriptModelBuilder();
		let state = history();
		const options = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: false, sessionId: 's1' };
		let before = build(releaseMessages(state), options);
		for (let k = 0; k < 3; k++) {
			state = reduce(state, event('tool.use', { tool_use_id: `long-${k}`, name: 'Read', input: {} }));
			state = reduce(state, event('tool.result', { tool_use_id: `long-${k}`, output: 'ok' }));
			const after = build(releaseMessages(state), options);
			expect(after).toEqual(buildTranscriptModel(state.messages, options));
			// Every row whose message did not change keeps its item object; only
			// the appended row (and a row the append finalized) is new.
			const reused = new Map(before.items.flatMap((item) => (item.kind === 'message' ? [[item.message, item] as const] : [])));
			let kept = 0;
			for (const item of after.items) {
				if (item.kind !== 'message' || !reused.has(item.message)) continue;
				expect(item).toBe(reused.get(item.message));
				kept++;
			}
			expect(kept).toBeGreaterThan(20);
			expect(after.items.filter((item) => !before.items.includes(item)).length).toBeLessThanOrEqual(2);
			before = after;
		}
	});

	it('drops a session\'s card-patch locator when the next build is another session', () => {
		const build = createTranscriptModelBuilder();
		let state = history();
		const options = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 's1' };
		build(releaseMessages(state), options);
		state = reduce(state, event('subagent.progress', { task_id: 'task-3', kind: 'chunk', note: 'working' }));
		const patchedA = build(releaseMessages(state), options);
		expect(build.retainedModels()).toEqual([patchedA]);
		// Switch to an empty session B: nothing of A may stay referenced.
		const empty = build([], { ...options, sessionId: 's2' });
		expect(build.retainedModels()).toHaveLength(1);
		expect(build.retainedModels()[0]).toBe(empty);
		expect(build.retainedModels()[0]).not.toBe(patchedA);
		// A card patch in B after the switch is still correct (no stale index map).
		const prompt = user('go', undefined, 'tb');
		const card = subagent({ toolId: 'wb', agentStatus: 'running', turnId: 'tb' });
		const bOptions = { ...options, sessionId: 's2' };
		build([prompt, card], bOptions);
		const progressed = [prompt, { ...card, agentChildren: [{ id: 'c', type: 'text' as const, content: 'x' }] }];
		expect(build(progressed, bOptions)).toEqual(buildTranscriptModel(progressed, bOptions));
	});
});

let seq = 0;
function msg(over: Partial<ChatMessage>): ChatMessage {
	return {
		id: `m-${seq++}`,
		role: 'assistant',
		content: '',
		...over
	} as ChatMessage;
}

function user(content: string, timestamp?: string, turnId?: string): ChatMessage {
	return msg({ role: 'user', content, timestamp, turnId });
}

function tool(over: Partial<ChatMessage> = {}): ChatMessage {
	return msg({
		type: 'tool_use',
		toolName: 'Bash',
		toolStatus: 'complete',
		toolInput: { command: 'ls' },
		...over
	});
}

function text(content: string, timestamp?: string): ChatMessage {
	return msg({ type: 'text', content, timestamp });
}

function subagent(over: Partial<ChatMessage> = {}): ChatMessage {
	return msg({ type: 'subagent', agentName: 'General Purpose', agentStatus: 'completed', ...over });
}

function retryCard(systemData: Record<string, unknown> | undefined = undefined): ChatMessage {
	return msg({
		role: 'system',
		type: 'system',
		systemSubtype: 'provider_retry',
		content: 'Retrying…',
		...(systemData ? { systemData } : {})
	});
}

function workEntries(messages: ChatMessage[]): WorkLogEntry[] {
	return messages.map((message, index) => ({ message, index }));
}

function collapsedVisibleMessages(items: DisplayItem[]): ChatMessage[] {
	return items.flatMap((item) => {
		if (item.kind === 'message') return [item.message];
		if (item.kind === 'system_group') return item.messages;
		if (item.sequence.length > 0) {
			return item.sequence
				.filter((entry) => !entry.hiddenWhenCollapsed && !entry.foldsInRun)
				.map((entry) => entry.message);
		}
		return item.tail.map((entry) => entry.message);
	});
}

/** Settled folds only: a live turn is one work log in live mode, never settled. */
/**
 * Settled groups that draw a disclosure. A settled turn with nothing to fold
 * keeps its group (so settling never remounts its rows) but shows no fold.
 */
function settledFolds(items: DisplayItem[]) {
	return items.filter((item) => item.kind === 'work_log' && item.mode === 'settled'
		&& (item.sequence.some((entry) => entry.hiddenWhenCollapsed) || (item.gallery?.length ?? 0) > 1));
}

/** The live turn's one work log, or undefined when the turn emitted none. */
function liveLog(items: DisplayItem[]) {
	return items.find((item): item is Extract<DisplayItem, { kind: 'work_log' }> =>
		item.kind === 'work_log' && item.mode === 'live');
}

const SETTLED = { isStreaming: false, groupingEnabled: true };
const LIVE = { isStreaming: true, groupingEnabled: true };

describe('agent deliveries in settled work', () => {
	const peer = (sender = 'reviewer') => user(`[agent\x20message from='${sender}' — a message from another agent in this session, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\nReview complete.\n[/agent\x20message]`);
	const receipt = () => msg({ role: 'system', type: 'system', systemSubtype: 'agent_message',
		systemData: { sender: 'tester', content: 'Tests pass.', delivery: 'delivered' } });
	const held = () => reduce(initialState(), {
		type: 'message.user', id: 'held', seq: 1, session_id: 'delivery-test',
		created_at: '2026-10-08T10:00:00Z', content: producer.prompts.promotion
	} as WireEvent).messages;

	it('folds both forms and reducer-produced held messages in their original order', () => {
		const prompt = user('Review the change');
		const deliveries = [receipt(), peer(), ...held()];
		const answer = text('All done');
		const messages = [prompt, tool({ toolName: 'Read' }), deliveries[0], tool(), ...deliveries.slice(1), answer];
		const items = buildDisplayItems(messages, SETTLED);
		expect(items.map(item => item.kind)).toEqual(['message', 'work_log']);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, answer]);
		const group = items[1];
		if (group.kind !== 'work_log') throw new Error('expected work log');
		expect(group.sequence.map(entry => entry.message)).toEqual(messages.slice(1));
		expect(group.folded.map(entry => entry.message)).toEqual(messages.slice(1, -1));
		expect(group.summary).toBe('Read 1 file, ran 1 command, and got 4 agent messages');
		expect(group.compactSummary).toBe('Read 1 · Ran 1 · Msgs 4');
	});

	it.each([LIVE, { ...SETTLED, latestTurnUnsettled: true }, { ...SETTLED, groupingEnabled: false }])(
		'keeps deliveries visible while live/parked until prose follows, or when grouping is disabled (%j)', options => {
			const messages = [user('Review'), receipt(), peer(), ...held()];
			expect(collapsedVisibleMessages(buildDisplayItems(messages, options))).toEqual(messages);
		}
	);

	it.each([LIVE, { ...SETTLED, latestTurnUnsettled: true }])(
		'folds live deliveries into the run the agent\'s next prose ends (%j)', options => {
			const prompt = user('Review');
			const deliveries = [receipt(), peer(), ...held()];
			const done = text('Done');
			const items = buildDisplayItems([prompt, ...deliveries, done], options);
			expect(collapsedVisibleMessages(items)).toEqual([prompt, done]);
			expect(liveLog(items)!.runs!.map((run) => run.summary)).toEqual(['Got 4 agent messages']);
		}
	);

	it.each([receipt, peer, held])('folds an agent-initiated wake with no human prompt (%#)', make => {
		const deliveries = [make()].flat();
		const answer = text('Follow-up complete');
		const messages = [...deliveries, tool(), answer];
		const model = buildTranscriptModel(messages, SETTLED);
		expect(model.items).toHaveLength(1);
		expect(model.items[0].kind).toBe('work_log');
		expect(collapsedVisibleMessages(model.items)).toEqual([answer]);
		const group = model.items[0];
		if (group.kind !== 'work_log') throw new Error('expected work log');
		expect(group.sequence.map(entry => entry.message)).toEqual(messages);
		expect(group.key).toBe(`wl-turn-${deliveries[0].id}`);
		expect(model.latestUserTurnId).toBeNull();
		expect([...model.itemIndexByTurnId]).toEqual([]);
		expect(model.rewindEligible.every(eligible => !eligible)).toBe(true);
		// Live, the answer ends the run of deliveries and tools before it.
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual([answer]);
		expect(collapsedVisibleMessages(buildDisplayItems(messages.slice(0, -1), LIVE))).toEqual(messages.slice(0, -1));
	});

	it('does not consume human checkpoint ordinals or turn navigation', () => {
		const delivery = { ...peer(), turnId: 'next', checkpointBacked: true, jsonlUuid: 'not-a-human-checkpoint' };
		const prompt = user('again', undefined, 'first');
		const next = user('again', undefined, 'next');
		const messages = [prompt, delivery, receipt(), ...held(), text('Done'), next, tool(), text('Done again')];
		const model = buildTranscriptModel(messages, SETTLED);
		expect(model.rewindEligible[1]).toBe(false);
		expect(model.rewindEligible[6]).toBe(true);
		expect(model.promptOccurrence[6]).toBe(1);
		expect(model.latestUserTurnId).toBe(next.id);
		expect([...model.itemIndexByTurnId.keys()]).toEqual([prompt.id, next.id]);
		expect(deriveUserTurnNavigationItems(messages).map(item => item.id)).toEqual([prompt.id, next.id]);
	});

	it.each(['idle_single', 'promotion', 'urgent_with_held'] as const)(
		'keeps a previous final answer outside the new idle wake fold (%s)', form => {
			let state = initialState();
			let seq = 0;
			const apply = (type: string, fields: Record<string, unknown> = {}) => {
				state = reduce(state, { type, id: `wake-${++seq}`, seq, session_id: 'wake-test',
					created_at: '2026-10-08T10:00:00Z', ...fields } as WireEvent);
			};
			apply('turn.started', { turn_id: 'human-turn', source: 'user' });
			apply('message.user', { turn_id: 'human-turn', content: 'Review' });
			apply('message.assistant', { content: 'Original final answer' });
			apply('turn.completed');
			apply('turn.started', { turn_id: 'idle-wake', source: form === 'promotion' ? 'promotion' : 'agent_message' });
			apply('message.user', { turn_id: 'idle-wake', content: producer.prompts[form] });
			const live = buildTranscriptModel(state.messages, LIVE);
			expect(collapsedVisibleMessages(live.items).some(m => m.content === 'Original final answer')).toBe(true);
			apply('message.assistant', { content: 'Wake final answer' });
			apply('turn.completed');
			const model = buildTranscriptModel(state.messages, SETTLED);
			expect(collapsedVisibleMessages(model.items).map(m => m.content)).toEqual([
				'Review', 'Original final answer', 'Wake final answer'
			]);
			const group = model.items.at(-1)!;
			if (group.kind !== 'work_log') throw new Error('expected wake work log');
			expect(group.folded).toHaveLength(form === 'idle_single' ? 1 : form === 'promotion' ? 2 : 3);
			expect(group.sequence.at(-1)?.message.content).toBe('Wake final answer');
			expect(model.latestUserTurnId).toBe(state.messages[0].id);
		});

	it.each([
		['live', 'idle_single'], ['parked', 'idle_single'],
		['live', 'promotion'], ['parked', 'promotion']
	] as const)('keeps a %s peer wake (%s) in the ongoing logical turn', (lifecycle, form) => {
		let state = initialState();
		let seq = 0;
		const apply = (type: string, fields: Record<string, unknown> = {}) => {
			state = reduce(state, { type, id: `ongoing-${++seq}`, seq, session_id: 'wake-test',
				created_at: '2026-10-08T10:00:00Z', ...fields } as WireEvent);
		};
		apply('turn.started', { turn_id: 'human-turn', source: 'user' });
		apply('message.user', { turn_id: 'human-turn', content: 'Review' });
		apply('message.assistant', { content: 'Waiting for agents' });
		if (lifecycle === 'parked') {
			apply('turn.completed');
			apply('turn.parked');
		}
		apply('turn.started', { turn_id: 'peer-wake', source: form === 'promotion' ? 'promotion' : 'agent_message' });
		apply('message.user', { turn_id: 'peer-wake', content: producer.prompts[form] });
		apply('message.assistant', { content: 'Final answer' });
		apply('turn.completed');
		const model = buildTranscriptModel(state.messages, SETTLED);
		expect(model.items.filter(item => item.kind === 'work_log')).toHaveLength(1);
		expect(collapsedVisibleMessages(model.items).map(m => m.content)).toEqual(['Review', 'Final answer']);
	});

	it('counts delivery-only work with singular and plural labels', () => {
		expect(summarizeWork(workEntries([receipt()]))).toBe('Got 1 agent message');
		expect(summarizeWorkCompact(workEntries([peer()]))).toBe('Msgs 1');
		expect(summarizeWork(workEntries(held()))).toBe('Got 2 agent messages');
	});
});

describe('Swarm workflow visibility', () => {
 it.each(['Swarm', 'mcp__fluid__Swarm'])('keeps %s outside settled work folds', (toolName) => {
  const workflow = tool({ toolName });
  const messages = [user('Review release'), tool(), workflow, text('Agents are working.')];
  expect(collapsedVisibleMessages(buildDisplayItems(messages, SETTLED))).toContain(workflow);
 });
});

describe('Swarm child deduplication', () => {
	const snapshot: SwarmSnapshot = {
		id: 'swarm-1', session_id: 'session-1', title: 'Review', status: 'running',
		created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:01:00Z',
		tasks: [{ id: 'review', title: 'Review', agent: 'General Purpose', depends_on: [], status: 'running',
			task_id: 'task-1', worker_session_id: 'worker-1' }]
	};
	const allRows = (items: DisplayItem[]) => items.flatMap((item) =>
		item.kind === 'work_log' ? item.sequence.map((entry) => entry.message)
			: item.kind === 'system_group' ? item.messages : [item.message]);

	it.each([LIVE, SETTLED, { ...SETTLED, groupingEnabled: false }])(
		'hides owned cards and Task/Agent rows, not standalone agents in mixed turns (%j)', (options) => {
			const workflow = tool({ toolName: 'mcp__fluid__Swarm', toolId: 'swarm-launch', toolResult: JSON.stringify({ swarm: snapshot }) });
			const children = [
				subagent({ toolId: 'task-1', agentStatus: 'running' }),
				subagent({ toolId: 'other-task', agentSessionId: 'worker-1', agentStatus: 'completed', agentLaunchToolUseId: 'child-launch' }),
				subagent({ toolId: 'early-worker', agentLaunchToolUseId: 'swarm-launch' }),
				tool({ toolName: 'Task', toolId: 'task-1', toolStatus: 'running' }),
				tool({ toolName: 'mcp__fluid__Agent', toolId: 'child-launch' }),
				msg({ type: 'tool_result', toolId: 'task-1', content: 'child done' })
			];
			const standalone = subagent({ toolId: 'standalone', agentLaunchToolUseId: 'standalone-launch' });
			const standaloneTool = tool({ toolName: 'Task', toolId: 'standalone-launch' });
			const messages = [user('Review'), standaloneTool, workflow, ...children, standalone, text('Done'), user('Review'), subagent()];
			const model = buildTranscriptModel(messages, { ...options, sessionId: snapshot.session_id });
			const rows = allRows(model.items);
			for (const child of children) expect(rows).not.toContain(child);
			expect(rows).toEqual(messages.filter((message) => !children.includes(message)));
			expect(model.promptOccurrence[messages.length - 2]).toBe(1);
			expect(model.items[model.itemIndexByTurnId.get(messages[messages.length - 2].id)!]).toMatchObject({ message: messages[messages.length - 2] });
			for (const item of model.items) {
				const entries = item.kind === 'work_log' ? [...item.sequence, ...item.folded, ...item.tail]
					: item.kind === 'message' ? [item] : item.messages.map((message, i) => ({ message, index: item.indices[i] }));
				for (const entry of entries) expect(messages[entry.index]).toBe(entry.message);
			}
		}
	);

	it('uses live/hydrated worker IDs before the Swarm tool result and keeps the raw transcript intact', () => {
		const workflow = tool({ toolName: 'Swarm', toolId: 'launch', toolStatus: 'running', toolInput: { tasks: snapshot.tasks } });
		const child = subagent({ toolId: 'unknown-task', agentSessionId: 'worker-1' });
		const messages = [user('Review'), workflow, child];
		expect(allRows(buildDisplayItems(messages, LIVE))).toContain(child);
		for (const status of ['running', 'completed'] as const) {
			const rows = allRows(buildDisplayItems(messages, { ...LIVE, sessionId: snapshot.session_id, swarms: [{ ...snapshot, status }] }));
			expect(rows).toEqual([messages[0], workflow]);
		}
		expect(messages).toContain(child);
	});

	it('uses the aggregate launch ID even with a truncated result and no snapshot yet', () => {
		const workflow = tool({ toolName: 'Swarm', toolId: 'launch', toolResult: '{"swarm":' });
		const child = subagent({ toolId: 'worker', agentLaunchToolUseId: 'launch' });
		const standalone = subagent({ toolId: 'independent', agentLaunchToolUseId: 'other-launch' });
		expect(allRows(buildDisplayItems([workflow, child, standalone], LIVE))).toEqual([workflow, standalone]);
	});

	it('does not infer ownership from task labels, malformed payloads, unrelated tools or other sessions', () => {
		const children = [subagent({ toolId: 'review', agentName: 'General Purpose' }), subagent({ toolId: 'task-1' }), subagent()];
		const unrelated = tool({ toolName: 'Bash', toolId: 'task-1' });
		for (const workflow of [
			tool({ toolName: 'Swarm', toolResult: '{"swarm":', toolInput: {} }),
			tool({ toolName: 'Swarm', toolResult: JSON.stringify({ ...snapshot, tasks: [{ task_id: 'task-1' }] }) }),
			tool({ toolName: 'SwarmStatus', toolResult: JSON.stringify(snapshot) }),
			tool({ toolName: 'Swarm', toolResult: JSON.stringify({ ...snapshot, session_id: 'other-session' }) })
		]) {
			const messages = [workflow, unrelated, ...children];
			expect(allRows(buildDisplayItems(messages, { ...LIVE, sessionId: snapshot.session_id,
				swarms: [{ ...snapshot, session_id: 'other-session' }] }))).toEqual(messages);
		}
		const workflow = tool({ toolName: 'Swarm', toolResult: JSON.stringify(snapshot) });
		expect(allRows(buildDisplayItems([workflow, unrelated, children[0]], LIVE))).toEqual([workflow, unrelated, children[0]]);
	});

	it('excludes duplicate results from settled group summaries and keeps original indices', () => {
		const workflow = tool({ toolName: 'Swarm', toolId: 'launch', toolResult: JSON.stringify(snapshot) });
		const read = tool({ toolName: 'Read', toolInput: { file_path: '/tmp/a' } });
		const standalone = tool({ toolName: 'Agent', toolId: 'independent' });
		const duplicate = tool({ toolName: 'Task', toolId: 'task-1' });
		const messages = [user('Review'), read, workflow, duplicate, subagent({ toolId: 'task-1' }), standalone, text('Done')];
		const work = buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log');
		expect(work?.kind).toBe('work_log');
		if (work?.kind !== 'work_log') return;
		expect(work.folded).toEqual([{ message: read, index: 1 }]);
		expect(work.summary).toBe('Read 1 file');
		expect(work.compactSummary).toBe('Read 1');
		expect(work.sequence.map((entry) => entry.index)).toEqual([1, 2, 5, 6]);
	});
});

describe('buildDisplayItems — settled turns', () => {
	it('folds non-final commentary and routine work while keeping the final answer visible', () => {
		const messages = [
			user('do it'),
			tool(),
			tool(),
			text('progress so far'),
			tool(),
			tool(),
			text('done')
		];
		const items = buildDisplayItems(messages, SETTLED);

		expect(items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		const fold = items[1];
		if (fold.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.mode).toBe('settled');
		expect(fold.folded).toHaveLength(5);
		expect(fold.folded.some((entry) => entry.message.content === 'progress so far')).toBe(true);
		expect(fold.tail).toHaveLength(0);
		expect(fold.sequence.map((entry) => entry.message.content)).toEqual([
			'', '', 'progress so far', '', '', 'done'
		]);
		expect(collapsedVisibleMessages(items).at(-1)?.content).toBe('done');
	});

	it('folds even a single completed routine tool after settlement', () => {
		const items = buildDisplayItems([user('do it'), tool(), text('done')], SETTLED);
		expect(items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		const fold = items[1];
		if (fold.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.folded).toHaveLength(1);
		expect(fold.summary).toBe('Ran 1 command');
		expect(collapsedVisibleMessages(items).at(-1)?.content).toBe('done');
	});

	it('normalizes the historical completed tool status at the presentation boundary', () => {
		const historical = tool({ toolStatus: 'completed' as never, streaming: false });
		const fold = buildDisplayItems([user('go'), historical, text('done')], SETTLED)
			.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.folded.map((entry) => entry.message.id)).toContain(historical.id);
	});

	it('folds completed thinking alongside routine tools', () => {
		const messages = [user('go'), msg({ type: 'thinking', content: 'hmm', streaming: false }), tool(), text('done')];
		const fold = buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.folded).toHaveLength(2);
	});

	it('folds failures but never screenshots, running tools, or still-streaming reasoning', () => {
		const failed = tool({ toolStatus: 'error' });
		const screenshot = tool({ toolScreenshot: 'data:image/png;base64,x' });
		const running = tool({ toolStatus: 'running', streaming: true });
		const thinking = msg({ type: 'thinking', content: 'still reasoning', streaming: true });
		const items = buildDisplayItems(
			[user('go'), tool(), failed, screenshot, running, thinking, text('done')],
			SETTLED
		);
		const visible = collapsedVisibleMessages(items);
		expect(visible.some((item) => item.id === failed.id)).toBe(false);
		for (const message of [screenshot, running, thinking]) {
			expect(visible.some((item) => item.id === message.id)).toBe(true);
		}
	});

	it('marks a failed tool call hidden inside the settled sequence', () => {
		const failed = tool({ toolStatus: 'error' });
		const fold = buildDisplayItems([user('go'), tool(), failed, text('done')], SETTLED)
			.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.folded.map((entry) => entry.message.id)).toContain(failed.id);
		const entry = fold.sequence.find((item) => item.message.id === failed.id);
		expect(entry?.hiddenWhenCollapsed).toBe(true);
	});

	it('keeps worker cards visible in every status (spec §3)', () => {
		const statuses = ['completed', 'error', 'stopped', 'running', 'pending'] as const;
		const cards = statuses.map((agentStatus) => subagent({ agentStatus }));
		const items = buildDisplayItems(
			[user('go'), tool({ toolName: 'Grep' }), ...cards, text('done')],
			SETTLED
		);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		// The fold now describes the routine work only; the cards speak for themselves.
		expect(fold.summary).toBe('Searched code 1 time');
		for (const card of cards) {
			expect(fold.folded.map((entry) => entry.message.id)).not.toContain(card.id);
			expect(fold.sequence.find((entry) => entry.message.id === card.id)?.hiddenWhenCollapsed).toBe(false);
		}
		expect(collapsedVisibleMessages(items).filter((message) => message.type === 'subagent')).toEqual(cards);
	});

	it('keeps a stopped subagent card visible instead of folding it away (#1806)', () => {
		const stopped = subagent({ agentStatus: 'stopped', agentStopNote: 'Stopped by user' });
		const items = buildDisplayItems([user('go'), tool(), stopped, text('done')], SETTLED);
		expect(collapsedVisibleMessages(items).some((message) => message.id === stopped.id)).toBe(true);
	});

	it('keeps a still-running subagent card visible', () => {
		const running = subagent({ agentStatus: 'running' });
		const items = buildDisplayItems([user('go'), tool(), running, text('done')], SETTLED);
		expect(collapsedVisibleMessages(items).some((message) => message.id === running.id)).toBe(true);
	});

	it('never folds agent, todo, question, plan, or subagent surfaces', () => {
		const messages = [
			user('go'),
			tool(),
			msg({ type: 'subagent', agentName: 'General Purpose' }),
			tool({ toolName: 'TodoWrite' }),
			tool({ toolName: 'Task' }),
			tool({ toolName: 'Agent' }),
			tool({ toolName: 'AskUserQuestion' }),
			tool({ toolName: 'AskUser' }),
			text('done')
		];
		const visibleMessages = collapsedVisibleMessages(buildDisplayItems(messages, SETTLED));

		expect(visibleMessages.some((message) => message.type === 'subagent')).toBe(true);
		for (const name of ['TodoWrite', 'Task', 'Agent', 'AskUserQuestion', 'AskUser']) {
			expect(visibleMessages.some((message) => message.toolName === name)).toBe(true);
		}
	});

	it('folds Note tools into the work-log group instead of standalone rows', () => {
		const note = tool({ toolName: 'Note', toolInput: { action: 'add', text: 'step 1' } });
		const items = buildDisplayItems([user('go'), tool(), note, text('done')], SETTLED);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.folded.map((entry) => entry.message.id)).toContain(note.id);
		expect(fold.sequence.find((entry) => entry.message.id === note.id)?.hiddenWhenCollapsed).toBe(true);
		expect(collapsedVisibleMessages(items).some((message) => message.id === note.id)).toBe(false);
		expect(fold.summary).toBe('Ran 1 command and took 1 note');
	});

	it('counts note tools in the compact summary tokens', () => {
		const entries = workEntries([
			tool({ toolName: 'Note' }),
			tool({ toolName: 'Note' }),
			tool({ toolName: 'Read' })
		]);
		expect(summarizeWork(entries)).toBe('Took 2 notes and read 1 file');
		expect(summarizeWorkCompact(entries)).toBe('Notes 2 · Read 1');
	});

	it('uses whole-turn duration and authoritative final metadata', () => {
		const messages = [
			user('go', '2026-08-19T10:00:00.000Z'),
			tool({ timestamp: '2026-08-19T10:00:30.000Z' }),
			text('done', '2026-08-19T10:02:14.000Z')
		];
		let fold = buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('Worked for 2m 14s');

		messages[2].metadata = { duration_ms: 9_000 };
		fold = buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('Worked for 9s');
	});

	it('anchors the duration at the turn start when a prompt is steered mid-turn', () => {
		// A message handed to a running turn carries THAT turn's id and lands
		// wherever the model received it — it must not re-anchor the fold.
		const messages = [
			user('go', '2026-08-19T10:00:00.000Z', 't1'),
			tool({ timestamp: '2026-08-19T10:00:30.000Z' }),
			user('also check the logs', '2026-08-19T10:02:46.000Z', 't1'),
			tool({ timestamp: '2026-08-19T10:02:48.000Z' }),
			text('done', '2026-08-19T10:02:52.000Z')
		];
		const folds = buildDisplayItems(messages, SETTLED).filter((item) => item.kind === 'work_log');

		// One turn, one fold: the steer does not cut it, the duration shows once
		// and the summary counts the whole turn.
		expect(folds).toHaveLength(1);
		expect(folds.map((fold) => fold.durationLabel)).toEqual(['Worked for 2m 52s']);
		expect(folds.map((fold) => fold.summary)).toEqual(['Ran 2 commands']);
		expect(folds[0].sequence.map((entry) => entry.message))
			.toEqual([messages[1], messages[2], messages[3], messages[4]]);
		// The steer is a protected row inside the sequence, in time order.
		expect(folds[0].sequence.filter((entry) => !entry.hiddenWhenCollapsed).map((entry) => entry.message))
			.toEqual([messages[2], messages[4]]);
		const model = buildTranscriptModel(messages, SETTLED);
		expect(model.items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		expect(model.itemIndexByTurnId.get(messages[2].id!)).toBe(1);
		// The steered prompt still shows: it is the user talking, not folded work.
		const visible = collapsedVisibleMessages(buildDisplayItems(messages, SETTLED));
		expect(visible.filter((message) => message.role === 'user')).toHaveLength(2);
	});

	it('keeps the true span of an interrupted turn that received a steer', () => {
		const messages = [
			user('go', '2026-08-19T10:00:00.000Z', 't1'),
			tool({ timestamp: '2026-08-19T10:00:30.000Z' }),
			user('stop and summarize', '2026-08-19T10:02:46.000Z', 't1'),
			text('half an answer\n\n[Stopped]', '2026-08-19T10:02:52.000Z')
		];
		const fold = buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.durationLabel).toBe('You stopped after 2m 52s');
	});

	it('still opens a turn for a user message belonging to a different turn', () => {
		const messages = [
			user('first', '2026-08-19T10:00:00.000Z', 't1'),
			text('one', '2026-08-19T10:00:09.000Z'),
			user('second', '2026-08-19T10:01:00.000Z', 't2'),
			tool({ timestamp: '2026-08-19T10:01:02.000Z' }),
			text('two', '2026-08-19T10:01:39.000Z')
		];
		const folds = settledFolds(buildDisplayItems(messages, SETTLED));

		expect(folds).toHaveLength(1);
		if (folds[0].kind !== 'work_log') throw new Error('expected a work_log');
		expect(folds[0].durationLabel).toBe('Worked for 39s');
	});

	it('uses explicit interruption wording and a quiet fallback', () => {
		const interrupted = [
			user('go', '2026-08-19T10:00:00.000Z'),
			tool({ timestamp: '2026-08-19T10:00:05.000Z' }),
			text('half an answer\n\n[Stopped]', '2026-08-19T10:00:12.000Z')
		];
		let fold = buildDisplayItems(interrupted, SETTLED).find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('You stopped after 12s');

		fold = buildDisplayItems([user('go'), tool(), text('done')], SETTLED)
			.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('Worked');
	});

	it('omits a settled retry card even from expanded work', () => {
		const settledRetry = retryCard({ settled: true, attempt: 3 });
		const items = buildDisplayItems([user('go'), tool(), settledRetry, text('done')], SETTLED);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.folded.map((entry) => entry.message.id)).not.toContain(settledRetry.id);
		expect(fold.sequence.find((entry) => entry.message.id === settledRetry.id)).toBeUndefined();
		expect(collapsedVisibleMessages(items).some((message) => message.id === settledRetry.id))
			.toBe(false);
		expect(items.some((item) => item.kind === 'message' && item.message.id === settledRetry.id))
			.toBe(false);
	});

	it('keeps an exhausted retry card visible while it still owns the Retry button', () => {
		const exhausted = retryCard({ exhausted: true, attempt: 10 });
		const items = buildDisplayItems([user('go'), tool(), exhausted, text('done')], SETTLED);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.folded.map((entry) => entry.message.id)).not.toContain(exhausted.id);
		expect(fold.sequence.find((entry) => entry.message.id === exhausted.id)?.hiddenWhenCollapsed)
			.toBe(false);
		expect(collapsedVisibleMessages(items).some((message) => message.id === exhausted.id)).toBe(true);
	});

	it('folds an exhausted retry card once it is superseded or recovered', () => {
		for (const outcome of [{ superseded: true }, { recovered: true }]) {
			const retired = retryCard({ exhausted: true, ...outcome });
			const items = buildDisplayItems([user('go'), tool(), retired, text('done')], SETTLED);
			const fold = items.find((item) => item.kind === 'work_log');
			if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

			expect(fold.folded.map((entry) => entry.message.id)).toContain(retired.id);
			expect(collapsedVisibleMessages(items).some((message) => message.id === retired.id))
				.toBe(false);
		}
	});
});

describe('buildDisplayItems — live turn', () => {
	it('keeps every completed and running tool visible', () => {
		const messages = [user('go'), tool(), tool(), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems(messages, LIVE);
		expect(settledFolds(items)).toEqual([]);
		expect(collapsedVisibleMessages(items)).toEqual(messages);
	});

	it('keeps live prose and streaming reasoning visible', () => {
		const prose = text('working on it');
		const thinking = msg({ type: 'thinking', content: 'active thought', streaming: true });
		const items = buildDisplayItems([user('go'), tool(), prose, thinking], LIVE);
		expect(collapsedVisibleMessages(items)).toContain(prose);
		expect(collapsedVisibleMessages(items)).toContain(thinking);
	});


	it('renders a single active tool plainly', () => {
		const messages = [user('go'), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems(messages, LIVE);
		expect(items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		expect(liveLog(items)!.runs).toEqual([]);
		expect(collapsedVisibleMessages(items)).toEqual(messages);
	});

	it('ignores an empty assistant phantom without hiding completed tools', () => {
		const messages = [user('go'), tool(), tool(), tool({ toolStatus: 'running', streaming: true })];
		expect(collapsedVisibleMessages(buildDisplayItems([...messages, text('')], LIVE))).toEqual(messages);
	});

	it('keeps completed tools visible between calls', () => {
		const messages = [user('go'), tool(), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual(messages);
	});

	it('keeps completed subagent cards visible, folding the run they end', () => {
		const messages = [user('go'), tool(), subagent(), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual([messages[0], messages[2], messages[3]]);
	});

	it('shows a streaming code-mode cell as the live tail instead of going silent', () => {
		const cell = tool({ toolName: 'run_python', toolStatus: 'running', streaming: true });
		// A turn whose only work is the streaming cell keeps the cell itself visible.
		const lone = buildDisplayItems([user('go'), cell], LIVE);
		expect(collapsedVisibleMessages(lone)).toContain(cell);

		// Once the cell executes inner calls, only those calls need visible rows.
		const executing = tool({ toolName: 'run_python', toolStatus: 'running', streaming: false });
		const inner = tool({ toolName: 'Read', toolStatus: 'running', streaming: true });
		const prompt = user('go');
		const items = buildDisplayItems([prompt, executing, inner], LIVE);
		expect(settledFolds(items)).toEqual([]);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, inner]);
	});

	it('never lets a still-streaming cell mask the long-running tool it dispatched', () => {
		// The cell's input finished streaming but its result is minutes away while
		// the video generation inside it runs. The visible row must be the real
		// work — never the enclosing Python cell.
		const cell = tool({ toolName: 'run_python', toolStatus: 'running', streaming: true });
		const video = tool({ toolName: 'mcp__builtins__generateVideo', toolStatus: 'running', streaming: true });
		const items = buildDisplayItems([user('go'), cell, video], LIVE);
		const visible = collapsedVisibleMessages(items);
		expect(visible.some((m) => m.id === video.id)).toBe(true);
		expect(visible.some((m) => m.id === cell.id)).toBe(false);
	});

	it('hides a lone streaming cell once its non-routine inner call takes over', () => {
		// Screenshot-bearing tools are never routine rows, so before this rule the
		// cell fell back to being the live tail and showed raw Python mid-capture.
		const cell = tool({ toolName: 'run_python', toolStatus: 'running', streaming: true });
		const shot = tool({
			toolName: 'browser_screenshot',
			toolStatus: 'running',
			streaming: true,
			toolScreenshot: 'data:image/png;base64,abc'
		});
		const items = buildDisplayItems([user('go'), cell, shot], LIVE);
		const visible = collapsedVisibleMessages(items);
		expect(visible.some((m) => m.id === shot.id)).toBe(true);
		expect(visible.some((m) => m.id === cell.id)).toBe(false);
	});

	it('settles earlier turns while the final turn streams', () => {
		const messages = [user('one'), tool(), text('answer'), user('two'), tool({ toolStatus: 'running' })];
		const folds = buildDisplayItems(messages, LIVE).filter((item) => item.kind === 'work_log');
		expect(folds.map((fold) => fold.kind === 'work_log' && fold.mode)).toEqual(['settled', 'live']);
	});

	it('does not fold a parked or otherwise unsettled latest turn', () => {
		const items = buildDisplayItems([user('go'), tool(), tool(), text('waiting on background work'), tool(), tool()], {
			isStreaming: false,
			latestTurnUnsettled: true,
			groupingEnabled: true
		});
		expect(settledFolds(items)).toEqual([]);
		expect(collapsedVisibleMessages(items).some((message) => message.content === 'waiting on background work')).toBe(true);
	});

	it('omits settled retry cards while the turn is live', () => {
		const messages = [user('go'), tool(), retryCard({ settled: true, attempt: 2 }), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual([messages[0], messages[1], messages[3]]);
	});

	it('omits an active retry card from the live run', () => {
		const activeRetry = retryCard();
		const items = buildDisplayItems([user('go'), tool(), activeRetry, tool()], LIVE);

		expect(items.some((item) => item.kind === 'message' && item.message.id === activeRetry.id))
			.toBe(false);
		for (const item of items) {
			if (item.kind !== 'work_log') continue;
			expect([...item.folded, ...item.tail].map((entry) => entry.message.id))
				.not.toContain(activeRetry.id);
		}
	});
	it('folds live work behind each completed prose, then folds once after the final response', () => {
		const messages = [
			user('go'), tool(), tool(), text('progress'),
			msg({ type: 'thinking', content: 'reasoning', streaming: false }),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_started' }),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_response' }),
			tool(), tool(), text('done')
		];
		// Check every arrival, including final prose before turn.completed.
		for (let length = 1; length <= messages.length; length++) {
			const prefix = messages.slice(0, length);
			const live = buildDisplayItems(prefix, LIVE);
			expect(settledFolds(live)).toEqual([]);
			// The prompt, the latest prose and everything after it stay on screen;
			// the routine work before the latest prose folds into its run.
			const latest = prefix.findLastIndex((m) => m.type === 'text');
			const expected = latest < 0 ? prefix : [prefix[0], ...prefix.slice(latest)];
			expect(collapsedVisibleMessages(live)).toEqual(expected);
		}
		const settled = buildDisplayItems(messages, SETTLED);
		expect(settled.filter((item) => item.kind === 'work_log')).toHaveLength(1);
		expect(collapsedVisibleMessages(settled)).toEqual([messages[0], messages.at(-1)]);
	});

});

describe('provider retries from wire events', () => {
	it.each([true, false])('hides automatic retries with grouping=%s but keeps terminal errors', (groupingEnabled) => {
		let state = initialState();
		let seq = 0;
		const apply = (type: string, fields: Record<string, unknown> = {}) => {
			state = reduce(state, {
				type, id: `retry-event-${++seq}`, seq, session_id: 'retry-test',
				created_at: '2026-09-11T10:00:00Z', ...fields
			} as WireEvent);
			return buildDisplayItems(state.messages, { isStreaming: state.isStreaming, groupingEnabled });
		};
		apply('message.user', { content: 'go', turn_id: 'turn-1' });
		apply('turn.started', { turn_id: 'turn-1' });
		const retry = (attempt: number) => apply('system.note', {
			subtype: 'provider_retry',
			data: { attempt, max_attempts: 10, reason: 'empty response from provider', delay_seconds: 1 }
		});
		expect(retry(1)).toHaveLength(1);
		expect(retry(2)).toHaveLength(1);
		expect(state.messages.some((m) => m.systemSubtype === 'provider_retry')).toBe(true);
		apply('assistant.text_delta', { text: 'Done.' });
		const completed = apply('turn.completed');
		expect(completed).toHaveLength(2);
		expect(collapsedVisibleMessages(completed).map((m) => m.content)).toEqual(['go', 'Done.']);
		// Reload uses the same retained messages, with no retry-only disclosure.
		expect(buildDisplayItems(structuredClone(state.messages), { isStreaming: false, groupingEnabled }))
			.toEqual(completed);

		apply('message.user', { content: 'again', turn_id: 'turn-2' });
		apply('turn.started', { turn_id: 'turn-2' });
		retry(10);
		const failed = apply('turn.aborted', { reason: 'error', message: 'empty response, giving up' });
		expect(collapsedVisibleMessages(failed).some((m) => m.systemData?.exhausted === true)).toBe(true);
	});
});

describe('work folds from wire events', () => {
	it('waits for turn completion rather than each assistant text segment', () => {
		let state = initialState();
		let seq = 0;
		const events: Array<[string, Record<string, unknown>]> = [
			['message.user', { content: 'Check two files', turn_id: 'turn-1' }],
			['turn.started', { turn_id: 'turn-1' }],
			['assistant.text_delta', { text: 'Checking the first file.' }],
			['tool.use', { tool_use_id: 'read-1', name: 'Read', input: { file_path: 'one.txt' } }],
			['tool.result', { tool_use_id: 'read-1', content_preview: 'one', is_error: false }],
			['message.user', { content: 'Also check the second file', turn_id: 'turn-1' }],
			['message.user', { content: 'And summarize it', turn_id: 'turn-1' }],
			['assistant.text_delta', { text: 'Now the second file.' }],
			['tool.use', { tool_use_id: 'read-2', name: 'Read', input: { file_path: 'two.txt' } }],
			['tool.result', { tool_use_id: 'read-2', content_preview: 'two', is_error: false }],
			['assistant.text_delta', { text: 'Both files checked.' }]
		];
		const apply = (type: string, fields: Record<string, unknown> = {}) => {
			const event: WireEvent = {
				type, id: `event-${++seq}`, seq, session_id: 'work-fold-test',
				created_at: '2026-09-10T10:00:00Z', ...fields
			};
			state = reduce(state, event);
			return buildDisplayItems(state.messages, {
				isStreaming: state.isStreaming,
				latestTurnUnsettled: state.isStreaming || Boolean(state.liveTurnId),
				groupingEnabled: true
			});
		};
		for (const [type, fields] of events) {
			const items = apply(type, fields);
			expect(settledFolds(items)).toEqual([]);
			// Every prompt and the latest prose stay; earlier work folds per run.
			const visible = collapsedVisibleMessages(items);
			for (const m of state.messages.filter((m) => m.role === 'user')) expect(visible).toContain(m);
			const latestProse = state.messages.findLast((m) => m.role === 'assistant' && m.type === 'text');
			if (latestProse) expect(visible).toContain(latestProse);
		}
		const settled = apply('turn.completed');
		expect(settled.map((item) => item.kind)).toEqual(['message', 'work_log']);
		expect(collapsedVisibleMessages(settled).map((message) => message.content))
			// The first prose after the steers is their reply and stays (#2267).
			.toEqual(['Check two files', 'Checking the first file.', 'Also check the second file', 'And summarize it', 'Now the second file.', 'Both files checked.']);
		expect(buildDisplayItems(structuredClone(state.messages), SETTLED)).toEqual(settled);
		const parked = buildDisplayItems(state.messages, { ...SETTLED, latestTurnUnsettled: true });
		expect(settledFolds(parked)).toEqual([]);
		// Parked again, the turn is live. A steer never ends a run (spec §1.3);
		// the prose around the steers does, so the live turn shows what the
		// settled one does, with the reads folded into their runs.
		expect(collapsedVisibleMessages(parked)).toEqual(collapsedVisibleMessages(settled));
	});
});

describe('work action summaries', () => {
	it('uses exact action labels and plurals for completed tools', () => {
		const entries = workEntries([
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Edit' }),
			tool({ toolName: 'Bash' }),
			tool({ toolName: 'Grep' }),
			tool({ toolName: 'Glob' }),
			tool({ toolName: 'WebSearch' }),
			tool({ toolName: 'WebFetch' }),
			tool({ toolName: 'CustomTool' })
		]);
		expect(summarizeWork(entries)).toBe(
			'Read 2 files, changed 1 file, ran 1 command, searched code 2 times, searched the web 2 times, and used 1 tool'
		);
	});

	it('counts failed tool calls after the action sentence', () => {
		const withFailures = workEntries([
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Read', toolStatus: 'error' }),
			tool({ toolName: 'Bash', toolStatus: 'error' })
		]);
		expect(summarizeWork(withFailures)).toBe('Read 2 files and ran 1 command, 2 failed');

		const oneFailure = workEntries([tool({ toolName: 'Bash', toolStatus: 'error' })]);
		expect(summarizeWork(oneFailure)).toBe('Ran 1 command, 1 failed');
	});

	it('leaves hidden code-mode cells out of the tally', () => {
		const withCell = workEntries([
			tool({ toolName: 'run_python', toolStatus: 'complete' }),
			tool({ toolName: 'Bash', toolStatus: 'complete' })
		]);
		expect(summarizeWork(withCell)).toBe('Ran 1 command');

		const runningCell = workEntries([
			tool({ toolName: 'run_python', toolStatus: 'running' }),
			tool({ toolName: 'Bash', toolStatus: 'complete' })
		]);
		expect(summarizeWork(runningCell)).toBe('Ran 1 command');

		const failedCell = workEntries([
			tool({ toolName: 'run_python', toolStatus: 'error' }),
			tool({ toolName: 'Bash', toolStatus: 'complete' })
		]);
		expect(summarizeWork(failedCell)).toBe('Used 1 tool and ran 1 command, 1 failed');
	});

	it('names media generation instead of lumping it into used-tools', () => {
		const entries = workEntries([
			tool({ toolName: 'generateImage' }),
			tool({ toolName: 'generateImage' }),
			tool({ toolName: 'editImage' }),
			tool({ toolName: 'generateVideo' }),
			tool({ toolName: 'generateAudio' }),
			tool({ toolName: 'textTo3D' }),
			tool({ toolName: 'mcp__builtins__generateImage' })
		]);
		expect(summarizeWork(entries)).toBe(
			'Generated 3 images, edited 1 image, generated 1 video, generated 1 audio track, and built 1 3D model'
		);
		expect(summarizeWorkCompact(entries)).toBe('Images 3 · ImgEdits 1 · Videos 1 · Audio 1 · 3D 1');
	});

	it('mentions folded thinking as part of the work', () => {
		const entries = workEntries([
			msg({ type: 'thinking', content: 'hmm' }),
			tool({ toolName: 'Read' }),
			msg({ type: 'thinking', content: 'again' })
		]);
		expect(summarizeWork(entries)).toBe('Read 1 file and thought 2 times');
		expect(summarizeWorkCompact(entries)).toBe('Read 1 · Thought 2');
	});

	it('closes the sentence with delegated tasks', () => {
		const entries = workEntries([
			msg({ type: 'subagent', agentStatus: 'completed' }),
			tool({ toolName: 'Read' })
		]);
		expect(summarizeWork(entries)).toBe('Read 1 file and delegated 1 task');
	});

	it('describes reasoning-only and empty groups', () => {
		expect(summarizeWork(workEntries([msg({ type: 'thinking', content: 'hmm' })])))
			.toBe('Reasoning and interim work');
		expect(summarizeWork([])).toBeNull();
	});
});

describe('compact work summaries', () => {
	it('condenses the same tally into tokens that fit a phone row', () => {
		const entries = workEntries([
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Edit' }),
			tool({ toolName: 'Bash' }),
			tool({ toolName: 'Grep' }),
			tool({ toolName: 'Glob' }),
			tool({ toolName: 'WebSearch' }),
			tool({ toolName: 'WebFetch' }),
			tool({ toolName: 'CustomTool' })
		]);
		expect(summarizeWorkCompact(entries)).toBe('Read 2 · Edit 1 · Ran 1 · Search 2 · Web 2 · Tools 1');
	});

	it('keeps the failure count and delegates, matching the prose order', () => {
		const entries = workEntries([
			tool({ toolName: 'Read' }),
			tool({ toolName: 'Read', toolStatus: 'error' }),
			msg({ type: 'subagent', agentStatus: 'completed' })
		]);
		expect(summarizeWorkCompact(entries)).toBe('Read 2 · Delegated 1 · 1 failed');
	});

	it('excludes hidden code-mode cells like the prose summary', () => {
		const entries = workEntries([
			tool({ toolName: 'run_python', toolStatus: 'complete' }),
			tool({ toolName: 'Bash', toolStatus: 'complete' })
		]);
		expect(summarizeWorkCompact(entries)).toBe('Ran 1');
	});

	it('reports a thinking-only fold instead of hiding it, and null otherwise', () => {
		expect(summarizeWorkCompact(workEntries([msg({ type: 'thinking', content: 'hmm' })])))
			.toBe('Thought 1');
		expect(summarizeWorkCompact(workEntries([msg({ type: 'text', content: 'hi' })]))).toBeNull();
		expect(summarizeWorkCompact([])).toBeNull();
	});
});

describe('buildDisplayItems — compact summary plumbing', () => {
	it('attaches compactSummary wherever a summary is built', () => {
		const items = buildDisplayItems([user('go'), tool({ toolName: 'Read' }), tool({ toolName: 'Grep' }), text('done')], SETTLED);
		const folds = items.filter((item): item is Extract<DisplayItem, { kind: 'work_log' }> => item.kind === 'work_log');
		expect(folds.length).toBe(1);
		expect(folds[0].summary).toBe('Read 1 file and searched code 1 time');
		expect(folds[0].compactSummary).toBe('Read 1 · Search 1');
	});
});

describe('buildDisplayItems — misc', () => {
	it('draws no fold when the only hidden row is a code-mode cell', () => {
		// Browser-found: a Python cell that dispatched a worker settled into a
		// "Show work details" button that revealed an empty row.
		const cell = tool({ toolName: 'run_python', toolStatus: 'complete' });
		const worker = subagent({ agentName: 'Backend Engineer' });
		const answer = text('done');
		const items = buildDisplayItems([user('go'), cell, worker, answer], SETTLED);
		expect(settledFolds(items)).toEqual([]);
		const log = items.find((item) => item.kind === 'work_log') as Extract<DisplayItem, { kind: 'work_log' }>;
		expect(log.sequence.map((entry) => entry.message)).toEqual([worker, answer]);

		const failed = tool({ toolName: 'run_python', toolStatus: 'error' });
		const withFailure = buildDisplayItems([user('go'), failed, worker, answer], SETTLED);
		const failedLog = withFailure.find((item) => item.kind === 'work_log') as Extract<DisplayItem, { kind: 'work_log' }>;
		expect(failedLog.sequence.map((entry) => entry.message)).toEqual([failed, worker, answer]);
	});

	it('renders work flat when grouping is disabled while retaining system grouping', () => {
		const messages = [
			user('go'),
			tool(),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_started' }),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_response' }),
			text('done')
		];
		const items = buildDisplayItems(messages, { isStreaming: false, groupingEnabled: false });
		expect(settledFolds(items)).toEqual([]);
		expect(items.some((item) => item.kind === 'system_group')).toBe(true);
	});

	it('drops duplicate ids so keyed rendering cannot crash', () => {
		const duplicate = tool();
		const items = buildDisplayItems([user('go'), duplicate, duplicate, text('done')], SETTLED);
		const ids = items.flatMap((item) => {
			if (item.kind === 'work_log') return item.folded.map((entry) => entry.message.id);
			if (item.kind === 'message') return [item.message.id];
			return item.messages.map((message) => message.id);
		});
		expect(new Set(ids).size).toBe(ids.length);
	});
});

describe('duration helpers', () => {
	it('formats seconds, minutes and hours', () => {
		expect(formatDuration(900)).toBe('1s');
		expect(formatDuration(45_000)).toBe('45s');
		expect(formatDuration(134_000)).toBe('2m 14s');
		expect(formatDuration(120_000)).toBe('2m');
		expect(formatDuration(3_780_000)).toBe('1h 03m');
	});

	it('needs two usable stamps unless metadata reports duration', () => {
		expect(turnDurationMs([text('a')])).toBeNull();
		expect(turnDurationMs([text('a', 'nonsense'), text('b', 'also nonsense')])).toBeNull();
		expect(
			turnDurationMs([text('a', '2026-08-19T10:00:00Z'), text('b', '2026-08-19T10:00:09Z')])
		).toBe(9000);
		expect(turnDurationMs([msg({ metadata: { duration_ms: 2_500 } })])).toBe(2_500);
	});

	it('detects the interrupt marker', () => {
		expect(turnWasInterrupted([text('partial\n\n[Stopped]')])).toBe(true);
		expect(turnWasInterrupted([text('all done')])).toBe(false);
	});
});


/**
 * The transcript model carries the two whole-array lookups the template used to
 * recompute per rendered row (PERFORMANCE_REVIEW §3.2 R3). These pin the
 * precomputed values against the original per-row implementations, which are
 * kept in work-log.ts as the oracle.
 */
describe('transcript model lookups', () => {
	it.each([true, false])('locates mid-turn prompts across live and settled rendering (grouping=%s)', (groupingEnabled) => {
		const first = user('first', undefined, 'turn-1');
		const steer = user('also check this', undefined, 'turn-1');
		const messages = [first, tool(), steer, tool(), text('done')];
		for (const isStreaming of [true, false]) {
			const model = buildTranscriptModel(messages, { isStreaming, groupingEnabled });
			for (const prompt of [first, steer]) {
				const itemIndex = model.itemIndexByTurnId.get(prompt.id!);
				expect(itemIndex).toBeDefined();
				const item = model.items[itemIndex!];
				// Every prompt, including a same-turn steer, remains independently
				// navigable without expanding a settled work disclosure: the opening
				// prompt is its own row, a settled steer is a protected row of the
				// turn's one work log.
				const entry = item.kind === 'message'
					? item
					: item.kind === 'work_log'
						? item.sequence.find((row) => row.message === prompt && !row.hiddenWhenCollapsed)
						: undefined;
				expect(entry).toBeDefined();
				expect(entry!.message).toBe(prompt);
				expect(entry!.index).toBe(messages.indexOf(prompt));
				// Grouping on, a steer rides inside the turn's one work log, live
				// (as a run boundary) and settled (as a protected row).
				const groupedSteer = groupingEnabled && prompt === steer;
				expect(item.kind).toBe(groupedSteer ? 'work_log' : 'message');
			}
		}
	});

	function linkedSubagentByScan(messages: ChatMessage[], toolId: string | undefined): boolean {
		return messages.some((m) => m.type === 'subagent' && m.toolId === toolId);
	}

	const SETTLED_OPTS = { isStreaming: false, groupingEnabled: true };

	it('excludes explicit and legacy same-turn steers from checkpoint occurrences', () => {
		const first = user('again', undefined, 'turn-1');
		const legacySteer = user('again', undefined, 'turn-1');
		const explicitSteer = { ...user('again'), checkpointBacked: false };
		const next = user('again', undefined, 'turn-2');
		const messages = [first, tool(), legacySteer, explicitSteer, next];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(model.rewindEligible).toEqual([true, false, false, false, true]);
		expect(model.promptOccurrence[4]).toBe(1);
	});

	it('does not treat the shared backfill sentinel as a mid-turn steer', () => {
		const messages = [user('again', undefined, 'backfill'), user('again', undefined, 'backfill')];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(model.rewindEligible).toEqual([true, true]);
		expect(model.promptOccurrence).toEqual([0, 1]);
	});

	it('matches the per-row prompt-occurrence scan for every message', () => {
		const messages: ChatMessage[] = [
			user('deploy the site'),
			text('done'),
			user('deploy the site'),
			text('done again'),
			msg({ role: 'user', type: 'command', content: 'deploy the site', commandName: '/deploy' }),
			user('  deploy the site  '),
			user('something else'),
			text('ok')
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(model.promptOccurrence).toHaveLength(messages.length);
		messages.forEach((message, index) => {
			if (message.role !== 'user' || message.type === 'command') return;
			expect(model.promptOccurrence[index]).toBe(userPromptOccurrenceAt(messages, index));
		});
		// The interesting case: identical prompts get 0, 1, 2 — trimmed, and with
		// the slash-command row not counted.
		expect([0, 2, 5].map((i) => model.promptOccurrence[i])).toEqual([0, 1, 2]);
	});

	it('counts prompts that the display list drops (duplicate ids)', () => {
		const dupe = user('again');
		const messages = [user('again'), dupe, dupe, text('ok')];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		messages.forEach((_, index) => {
			expect(model.promptOccurrence[index]).toBe(userPromptOccurrenceAt(messages, index));
		});
	});

	it('matches the per-row subagent scan, including the id-less case', () => {
		const messages: ChatMessage[] = [
			user('research this'),
			msg({ type: 'tool_use', toolName: 'Task', toolId: 'tool-1', toolStatus: 'complete' }),
			msg({ type: 'subagent', toolId: 'tool-1', agentStatus: 'completed' }),
			msg({ type: 'tool_use', toolName: 'Task', toolId: 'tool-2', toolStatus: 'complete' }),
			text('done')
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		for (const toolId of ['tool-1', 'tool-2', undefined]) {
			expect(hasLinkedSubagent(model, toolId)).toBe(linkedSubagentByScan(messages, toolId));
		}
		expect(hasLinkedSubagent(model, 'tool-1')).toBe(true);
		expect(hasLinkedSubagent(model, 'tool-2')).toBe(false);
	});

	it('matches the id-less scan when a subagent card carries no toolId', () => {
		const messages: ChatMessage[] = [
			user('go'),
			msg({ type: 'tool_use', toolName: 'Task', toolStatus: 'complete' }),
			msg({ type: 'subagent', agentStatus: 'completed' }),
			text('done')
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(hasLinkedSubagent(model, undefined)).toBe(linkedSubagentByScan(messages, undefined));
		expect(hasLinkedSubagent(model, undefined)).toBe(true);
		expect(hasLinkedSubagent(model, 'other')).toBe(false);
	});

	it('locates each user turn in the display list so windowing can reach it', () => {
		const messages: ChatMessage[] = [
			user('first'),
			tool(),
			tool(),
			text('one'),
			user('second'),
			tool(),
			text('two')
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		for (const message of messages) {
			if (message.role !== 'user' || !message.id) continue;
			const itemIndex = model.itemIndexByTurnId.get(message.id);
			expect(itemIndex).toBeDefined();
			const item = model.items[itemIndex as number];
			expect(item.kind).toBe('message');
			expect(item.kind === 'message' && item.message.id).toBe(message.id);
		}
	});

	it('reports the last assistant metadata and latest user turn in one pass', () => {
		const first = user('first');
		const last = user('second');
		const messages: ChatMessage[] = [
			first,
			msg({ role: 'assistant', type: 'text', content: 'a', metadata: { cost_usd: 1, duration_ms: 10 } }),
			last,
			msg({ role: 'assistant', type: 'text', content: 'b', metadata: { cost_usd: 2, duration_ms: 20 } }),
			tool()
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(model.lastAssistantMetadata).toEqual({ cost_usd: 2, duration_ms: 20 });
		expect(model.latestUserTurnId).toBe(last.id);

		const withoutMetadata = buildTranscriptModel([user('only')], SETTLED_OPTS);
		expect(withoutMetadata.lastAssistantMetadata).toBeNull();

		const withoutTurns = buildTranscriptModel([text('orphan')], SETTLED_OPTS);
		expect(withoutTurns.latestUserTurnId).toBeNull();
	});

	it('ignores slash-command rows when resolving the latest user turn', () => {
		const prompt = user('do the thing');
		const messages: ChatMessage[] = [
			prompt,
			msg({ role: 'user', type: 'command', content: '/commit', commandName: '/commit' })
		];
		expect(buildTranscriptModel(messages, SETTLED_OPTS).latestUserTurnId).toBe(prompt.id);
	});

	it('produces exactly the list buildDisplayItems returns', () => {
		const messages: ChatMessage[] = [
			user('go'),
			msg({ type: 'thinking', content: 'hmm' }),
			tool(),
			tool({ toolStatus: 'error' }),
			text('the answer')
		];
		const model = buildTranscriptModel(messages, SETTLED_OPTS);
		expect(model.items).toEqual(buildDisplayItems(messages, SETTLED_OPTS));
	});
});

describe('context compaction inside a turn', () => {
	function compacted(subtype = 'compact_boundary'): ChatMessage {
		return msg({
			role: 'system',
			type: 'system',
			systemSubtype: subtype,
			content: '',
			systemData: { compact_metadata: { trigger: 'auto', pre_tokens: 180_000 } }
		});
	}

	it.each(['compact_boundary', 'microcompact_boundary'])(
		'folds a mid-turn %s card into the Worked group and keeps it as an expanded row',
		(subtype) => {
			const card = compacted(subtype);
			const answer = text('done');
			const messages = [user('go'), tool(), card, tool(), answer];
			const items = buildDisplayItems(messages, SETTLED);
			expect(items.map((item) => item.kind)).toEqual(['message', 'work_log']);
			const log = items[1] as Extract<DisplayItem, { kind: 'work_log' }>;
			expect(log.folded.map((entry) => entry.message)).toContain(card);
			expect(log.sequence.find((entry) => entry.message === card)?.hiddenWhenCollapsed).toBe(true);
			expect(collapsedVisibleMessages(items)).toEqual([messages[0], answer]);
			// The payload is untouched: the expanded row renders the same card.
			expect(log.sequence.find((entry) => entry.message === card)?.message).toBe(card);
		}
	);

	it('mentions the compaction in the fold summary', () => {
		const rows = workEntries([tool(), compacted()]);
		expect(summarizeWork(rows)).toBe('Ran 1 command and compacted the context');
		expect(summarizeWorkCompact(rows)).toBe('Ran 1 · Compacted 1');
		expect(summarizeWork(workEntries([compacted()]))).toBe('Compacted the context');
	});

	it('leaves a compaction that trails the answer on its own row', () => {
		const card = compacted();
		const messages = [user('go'), tool(), text('done'), card];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, SETTLED))).toContain(card);
	});

	it('leaves the replay elided-prefix marker and failed compactions visible', () => {
		const marker = compacted('compaction_boundary');
		const failed = compacted('compact_failed');
		const messages = [user('go'), tool(), marker, failed, tool(), text('done')];
		const visible = collapsedVisibleMessages(buildDisplayItems(messages, SETTLED));
		expect(visible).toContain(marker);
		expect(visible).toContain(failed);
	});

	it('shows the card flat while the turn is still running', () => {
		const card = compacted();
		const messages = [user('go'), tool(), card, tool({ toolStatus: 'running' })];
		const items = buildDisplayItems(messages, { ...SETTLED, latestTurnUnsettled: true });
		expect(settledFolds(items)).toEqual([]);
		expect(collapsedVisibleMessages(items)).toContain(card);
	});
});

describe('settled work log identity', () => {
	it('keeps its key when a late result folds an earlier row', () => {
		const prompt = user('go');
		const running = tool({ toolStatus: 'running' });
		const before = [prompt, running, tool(), text('done')];
		const after = [prompt, { ...running, toolStatus: 'complete' as const }, before[2], before[3]];
		const keyOf = (messages: ChatMessage[]) =>
			buildDisplayItems(messages, SETTLED).find((item) => item.kind === 'work_log')!;
		const first = keyOf(before) as Extract<DisplayItem, { kind: 'work_log' }>;
		const second = keyOf(after) as Extract<DisplayItem, { kind: 'work_log' }>;
		expect(second.folded.length).toBe(first.folded.length + 1);
		expect(second.key).toBe(first.key);
	});

	it('keys a steered turn by its opening prompt, once', () => {
		const messages = [
			user('go', undefined, 't1'), tool(), text('first'),
			user('also this', undefined, 't1'), tool(), text('second')
		];
		const items = buildDisplayItems(messages, SETTLED);
		const keys = items
			.filter((item) => item.kind === 'work_log')
			.map((item) => (item as Extract<DisplayItem, { kind: 'work_log' }>).key);
		expect(keys).toEqual([`wl-turn-${messages[0].id}`]);
		// The answer the steer replied to and the final answer both stay visible.
		expect(collapsedVisibleMessages(items)).toEqual([messages[0], messages[2], messages[3], messages[5]]);
	});

	// #2267: the steer's own reply used to fold, so the steer looked ignored.
	it('keeps the prose that answers a mid-turn steer visible when collapsed', () => {
		const prompt = user('Run the checks', undefined, 't1');
		const first = text('Starting the checks in the background.');
		const park = tool({ toolName: 'Monitor' });
		const steer = user('While that runs: reply with the single word steered.', undefined, 't1');
		const reply = text('steered');
		const final = text('Checks passed.');
		const items = buildDisplayItems([prompt, first, park, steer, reply, tool(), final], SETTLED);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, first, steer, reply, final]);
		// The reply still lives in the expanded log, in time order.
		const log = items.find((item) => item.kind === 'work_log') as Extract<DisplayItem, { kind: 'work_log' }>;
		expect(log.sequence.map((entry) => entry.message)).toEqual([first, park, steer, reply, log.sequence[4].message, final]);
	});

	it('lets the final prose answer a steer followed only by tools, shown once', () => {
		const prompt = user('Run the checks', undefined, 't1');
		const first = text('Starting the checks.');
		const steer = user('Also lint it.', undefined, 't1');
		const final = text('Checks and lint passed.');
		const items = buildDisplayItems([prompt, first, tool(), steer, tool(), tool(), final], SETTLED);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, first, steer, final]);
	});
});

/**
 * Turn unity (transcript-organization spec §1): one turn is one fold, a steer
 * stays inside the turn it interrupted, and a send never folds a turn whose
 * work is still in flight.
 */
describe('turn unity', () => {
	it('renders the live long turn as runs, steer in place', () => {
		const t = longTurn();
		const items = buildDisplayItems(t.messages, { ...SETTLED, latestTurnUnsettled: true, runningToolIds: new Set(['monitor-1']) });
		expect(settledFolds(items)).toEqual([]);
		expect(items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		const log = liveLog(items)!;
		// Same key the settled fold will have, so settling flips mode in place.
		expect(log.key).toBe(`wl-turn-${t.prompt.id}`);
		expect(log.sequence.map((entry) => entry.message)).toEqual(t.messages.slice(1));
		// Each earlier run folds behind the row that ended it, which stays
		// below it: the finding, the question's lead-in, the first worker, the
		// second worker and the final prose. A run is keyed by its first row.
		// The question and the steer's reply end runs with nothing to fold. Steers end no run: each stays in place
		// inside its run. The failed Edit is routine work: it folds with its run
		// and the summary counts it. The running Monitor finished its call, so it
		// folds with its run.
		expect(log.runs!.map((run) => [run.index, run.key, run.folded.map((entry) => entry.message)])).toEqual([
			[0, t.read.id, [t.read]],
			[1, t.grep.id, [t.grep]],
			[2, t.monitor.id, [t.monitor]],
			[5, t.build.id, [t.build, t.failed, t.retry]],
			[6, t.steer2.id, [t.grep2, t.report]]
		]);
		expect(log.runs!.map((run) => run.summary)).toEqual([
			'Read 1 file', 'Searched code 1 time', 'Ran 1 command',
			'Ran 1 command and changed 2 files, 1 failed', 'Searched code 1 time and got 1 agent message'
		]);
		// Live shows what the settled turn will: nothing after the final prose
		// is still open, so the current run is empty.
		expect(collapsedVisibleMessages(items)).toEqual([
			t.prompt, t.finding, t.interim, t.worker, t.question, t.steer, t.ack,
			t.worker2, t.steer2, t.final
		]);
	});

	it('shows fewer rows once the long turn settles into its one fold', () => {
		const t = longTurn();
		const live = buildDisplayItems(t.messages, { ...SETTLED, latestTurnUnsettled: true });
		const settled = buildDisplayItems(t.messages, SETTLED);
		expect(liveLog(live)!.key).toBe((settledFolds(settled)[0] as Extract<DisplayItem, { kind: 'work_log' }>).key);
		// Settled, the turn is one fold: the failed Edit was retried and folds;
		// the acknowledgement stays as the answer the second steer replied to.
		expect(collapsedVisibleMessages(settled)).toEqual([
			t.prompt, t.finding, t.interim, t.worker, t.question, t.steer, t.ack, t.worker2, t.steer2, t.final
		]);
	});

	it('settles the long turn into one fold with the steer retained inside', () => {
		const t = longTurn();
		const messages = t.messages.map((m) => (m === t.worker ? { ...m, agentStatus: 'completed' as const } : m));
		const model = buildTranscriptModel(messages, SETTLED);
		const folds = model.items.filter((item) => item.kind === 'work_log');
		expect(folds).toHaveLength(1);
		expect(model.items.map((item) => item.kind)).toEqual(['message', 'work_log']);
		const fold = folds[0] as Extract<DisplayItem, { kind: 'work_log' }>;
		expect(fold.key).toBe(`wl-turn-${t.prompt.id}`);
		expect(fold.durationLabel).toBe('Worked for 30m');
		// Every row of the turn, in time order, in the one sequence.
		expect(fold.sequence.map((entry) => entry.message)).toEqual(messages.slice(1));
		const visible = collapsedVisibleMessages(model.items).map((m) => m.id);
		// The prose the steer interrupted stays, as it did before turns were unified.
		// The interim finding and the finished worker card stay too (spec §3); the
		// acknowledgement stays as the answer the second steer replied to.
		expect(visible).toEqual([
			t.prompt.id, t.finding.id, t.interim.id, t.worker.id, t.question.id, t.steer.id, t.ack.id,
			t.worker2.id, t.steer2.id, t.final.id
		]);
		expect(model.itemIndexByTurnId.get(t.steer.id!)).toBe(1);
		expect(model.itemIndexByTurnId.get(t.steer2.id!)).toBe(1);
		expect(model.midTurnUserIds).toEqual(new Set([t.steer.id, t.steer2.id]));
	});

	it('needs you while the question waits for the steer that answers it, and not once settled (spec §5)', () => {
		const LOCAL = { status: 'active', is_remote: false, remote_runtime_state: null } as const;
		const t = longTurn();
		const until = (row: ChatMessage) => t.messages.slice(0, t.messages.indexOf(row) + 1);
		const pending = [{ request_id: 'ask-1' }];
		// Live, the turn has asked: its QuestionTray item counts, and its lead-in
		// prose is that question rather than a second one.
		const asking = openSessionNeeds({ messages: until(t.question), pendingQuestions: pending }, true);
		expect(asking).toEqual({ permissions: 0, questions: 1, proseQuestion: false });
		expect(needsYouCount(LOCAL, null, 0, asking)).toBe(1);
		expect(deriveStatus({ streaming: true, attention: null, isActive: true, open: asking })).toBe('has_question');
		// Asked in prose instead of the tool, the same turn still needs you...
		const prose = text('Staging too, or prod only?', '2026-10-10T20:03:00.000Z');
		const proseAsk = [...until(t.worker), prose];
		expect(needsYouCount(LOCAL, null, 0, openSessionNeeds({ messages: proseAsk }, true))).toBe(1);
		// ...and the steer that answers it clears both forms.
		const answered = [...proseAsk, t.steer, t.ack];
		expect(needsYouCount(LOCAL, null, 0, openSessionNeeds({ messages: answered, pendingQuestions: [] }, true))).toBe(0);
		expect(needsYouCount(LOCAL, null, 0, openSessionNeeds({ messages: until(t.steer), pendingQuestions: [] }, true))).toBe(0);
		// Settled, nothing waits.
		expect(needsYouCount(LOCAL, null, 0, openSessionNeeds({ messages: t.messages, pendingQuestions: [] }, true))).toBe(0);
	});

	it('keeps a turn flat while its background work runs, even after a later turn opened', () => {
		const t = longTurn();
		const next = user('Thanks, now the docs', '2026-10-10T20:40:00.000Z', 'turn-2');
		const messages = [...t.messages, next, text('Docs next.')];
		const running = buildDisplayItems(messages, { ...SETTLED, latestTurnUnsettled: false, runningToolIds: new Set(['monitor-1']) });
		expect(settledFolds(running)).toEqual([]);
		const done = buildDisplayItems(messages, { ...SETTLED, latestTurnUnsettled: false, runningToolIds: new Set() });
		const folds = settledFolds(done) as Extract<DisplayItem, { kind: 'work_log' }>[];
		expect(folds.map((fold) => fold.key)).toEqual([`wl-turn-${t.prompt.id}`]);
	});

	it('keeps a send into a parked turn inside that turn until the server starts the next one', () => {
		let state = initialState();
		let seq = 0;
		const apply = (type: string, fields: Record<string, unknown> = {}) => {
			state = reduce(state, {
				type, id: `event-${++seq}`, seq, session_id: 'turn-unity',
				created_at: '2026-10-10T20:00:00Z', ...fields
			} as WireEvent);
		};
		apply('turn.started', { turn_id: 'turn-1' });
		apply('message.user', { content: 'Watch the deploy', turn_id: 'turn-1' });
		apply('tool.use', { tool_use_id: 'monitor-1', name: 'Monitor', input: { command: 'tail -f deploy.log' } });
		apply('tool.result', { tool_use_id: 'monitor-1', content_preview: 'watching', is_error: false });
		apply('assistant.text_delta', { text: 'Watching the deploy in the background.' });
		apply('turn.completed', {});
		apply('turn.parked', { turn_id: 'turn-1' });
		const activeTasks = [{ status: 'running', toolUseId: 'monitor-1' }];
		const parked = { ...state, turnParked: state.parked, activeTasks };
		expect(parked.isStreaming).toBe(false);
		expect(parked.liveTurnId).toBeNull();

		// The optimistic steer (what message-sender appends).
		const turnId = parkedSendTurnId(parked);
		expect(turnId).toBe('turn-1');
		const steer: ChatMessage = { id: 'user-optimistic', role: 'user', content: 'Also ping me when done', turnId };
		state = { ...state, messages: [...state.messages, steer] };
		const options = { ...SETTLED, latestTurnUnsettled: true, runningToolIds: new Set(['monitor-1']) };
		const optimistic = buildTranscriptModel(state.messages, options);
		expect(settledFolds(optimistic.items)).toEqual([]);
		expect(optimistic.midTurnUserIds.has('user-optimistic')).toBe(true);
		// A steer is part of its turn, not a navigation stop of its own (#2268).
		expect(deriveUserTurnNavigationItems(state.messages).map((item) => item.id)).toEqual([state.messages[0].id]);

		// The backend starts the next turn; only its message.user re-homes the row.
		apply('turn.started', { turn_id: 'turn-2' });
		expect(state.messages.find((m) => m.id === 'user-optimistic')?.turnId).toBe('turn-1');
		// The send was queued behind the parked turn: the server names that turn in
		// queued_turn_id, but grouping follows the owning turn_id alone.
		apply('message.user', { content: 'Also ping me when done', turn_id: 'turn-2', queued_turn_id: 'turn-1' });
		const rehomed = state.messages.find((m) => m.id === 'user-optimistic');
		expect(rehomed?.turnId).toBe('turn-2');
		expect(rehomed?.queuedTurnId).toBe('turn-1');
		// Re-homed, the send opens turn-2, so it becomes that turn's stop.
		expect(deriveUserTurnNavigationItems(state.messages).map((item) => item.id).at(-1)).toBe('user-optimistic');
		expect(deriveUserTurnNavigationItems(state.messages)).toHaveLength(2);
		// The parked turn's Monitor still runs: nothing above the steer folds.
		const after = buildDisplayItems(state.messages, options);
		expect(settledFolds(after)).toEqual([]);
	});

	it('a send during the settle hold does not fold the turn it interrupted, with no background work', () => {
		vi.useFakeTimers();
		try {
			const prompt = user('Summarize the logs', '2026-10-10T20:00:00.000Z', 'turn-1');
			const read = tool({ toolName: 'Read', toolInput: { file_path: 'app.log' }, timestamp: '2026-10-10T20:00:05.000Z' });
			const answer = text('Nothing unusual.', '2026-10-10T20:00:10.000Z');
			const before = [prompt, read, answer];
			const settle = createSettleHold(() => {});
			const handoff = createTurnHandoffHold(() => {});
			const view = (messages: ChatMessage[], rawUnsettled: boolean) => {
				const latestTurnUnsettled = settle.update({ sessionId: 's', unsettled: rawUnsettled, parked: false });
				const heldTurnKeys = handoff.update({ sessionId: 's', latestTurnKey: latestTurnKey(messages), unsettled: latestTurnUnsettled, parked: false });
				return buildDisplayItems(messages, { ...SETTLED, latestTurnUnsettled, heldTurnKeys, runningToolIds: new Set() })
					.map((item) => (item.kind === 'work_log' ? `work_log:${item.mode}` : item.kind));
			};
			expect(view(before, true)).toEqual(['message', 'work_log:live']);
			// turn.completed: the hold keeps it live.
			expect(view(before, false)).toEqual(['message', 'work_log:live']);
			// The user sends inside the hold: the optimistic row opens a new turn.
			const after = [...before, user('And the errors?')];
			expect(latestTurnKey(after)).toBe(after[3].id);
			expect(view(after, false)).toEqual(['message', 'work_log:live', 'message']);
			// Once the hold runs out, the earlier turn settles exactly once.
			vi.advanceTimersByTime(SETTLE_HOLD_MS);
			expect(view(after, true)).toEqual(['message', 'work_log:settled', 'message']);
		} finally {
			vi.useRealTimers();
		}
	});

	it('names the latest turn the same way its fold is keyed', () => {
		const t = longTurn();
		const next = user('Thanks, now the docs', '2026-10-10T20:40:00.000Z', 'turn-2');
		const messages = [...t.messages, next, text('Docs next.')];
		expect(latestTurnKey(t.messages)).toBe(t.prompt.id);
		expect(latestTurnKey(messages)).toBe(next.id);
		const folds = buildDisplayItems(messages, { ...SETTLED, heldTurnKeys: new Set([t.prompt.id!]) });
		expect(settledFolds(folds)).toHaveLength(0);
		expect(latestTurnKey([])).toBeNull();
	});

	it('names the parked turn for a send only while the turn is parked', () => {
		const prompt = user('go', undefined, 'turn-1');
		const base = { isStreaming: false, liveTurnId: null, messages: [prompt, tool()] };
		expect(parkedSendTurnId(base)).toBeUndefined();
		expect(parkedSendTurnId({ ...base, turnParked: true })).toBe('turn-1');
		const launch = tool({ toolName: 'Monitor', toolId: 'monitor-1' });
		const withLaunch = { ...base, messages: [prompt, launch] };
		expect(parkedSendTurnId({ ...withLaunch, activeTasks: [{ status: 'running', toolUseId: 'monitor-1' }] })).toBe('turn-1');
		// A running task with no known launch row proves nothing about the latest turn.
		expect(parkedSendTurnId({ ...withLaunch, activeTasks: [{ status: 'running' }] })).toBeUndefined();
		expect(parkedSendTurnId({ ...base, activeTasks: [{ status: 'running', toolUseId: 'monitor-1' }], liveTurnId: 'turn-9' })).toBe('turn-9');
		expect(parkedSendTurnId({ ...base, isStreaming: true, turnParked: true })).toBeUndefined();
		// Background work launched by turn 1 still runs, but turn 2 completed and is
		// not parked: a send there must not join turn 2.
		const second = user('next', undefined, 'turn-2');
		expect(parkedSendTurnId({
			isStreaming: false, liveTurnId: null, turnParked: false,
			activeTasks: [{ status: 'running', toolUseId: 'monitor-1' }],
			messages: [prompt, launch, second, text('done')]
		})).toBeUndefined();
		expect(parkedSendTurnId({ ...base, turnParked: true, messages: [user('legacy'), tool()] })).toBeUndefined();
	});
});

/**
 * Protected prose (transcript-organization spec §3): a settled turn folds
 * narration, never a question or a finding.
 */
describe('protected prose', () => {
	function hiddenProse(...rows: ChatMessage[]) {
		const items = buildDisplayItems([user('go'), ...rows, tool(), text('All done.')], SETTLED);
		const visible = new Set(collapsedVisibleMessages(items).map((m) => m.id));
		return rows.filter((m) => m.type === 'text' && !visible.has(m.id)).map((m) => m.content);
	}

	it('folds one short line without a question as narration', () => {
		expect(hiddenProse(text('Now checking the workflow.'), tool())).toEqual(['Now checking the workflow.']);
	});

	it('keeps prose that asks', () => {
		expect(hiddenProse(text('Should I also update staging?'), tool())).toEqual([]);
	});

	it('ignores a question mark inside a link or inline code', () => {
		expect(isProtectedProse('Fetching https://example.com/a?b=1 now.')).toBe(false);
		expect(isProtectedProse('Running `ls ?.txt` next.')).toBe(false);
		expect(isProtectedProse('Is https://example.com/a?b=1 right?')).toBe(true);
		// Sentence-ending punctuation after a URL is not part of it.
		expect(isProtectedProse('Should I use https://example.com?')).toBe(true);
		expect(isProtectedProse('Should I use https://example.com/a?b=1?')).toBe(true);
		expect(isProtectedProse('See https://example.com/a?b=1.')).toBe(false);
		// A query string in a markdown link target is not a question.
		expect(isProtectedProse('Reading [guide](/docs?page=1).')).toBe(false);
		expect(isProtectedProse('Reading <https://example.com/a?b=1> now.')).toBe(false);
		expect(isProtectedProse('Is [the guide](/docs?page=1) current?')).toBe(true);
	});

	it('keeps the lead-in to a question tool in the same step', () => {
		const lead = text('One choice before I continue.');
		const ask = tool({ toolName: 'AskUserQuestion' });
		expect(hiddenProse(lead, ask, tool())).toEqual([]);
		// A question tool after later prose does not protect the earlier line.
		expect(hiddenProse(text('Reading the config.'), tool(), text('Choosing a branch.'), ask, tool()))
			.toEqual(['Reading the config.']);
	});

	it.each([
		['two or more lines', 'The deploy uses wrangler 3.\nStaging shares the prod token.'],
		['a heading', '## Findings'],
		['a list', '- the token is shared'],
		['a numbered list', '1. rotate the token'],
		['a table', '| job | token |'],
		['a code fence', '```yaml']
	])('keeps a finding with %s', (_label, content) => {
		expect(isProtectedProse(content)).toBe(true);
		expect(hiddenProse(text(content), tool())).toEqual([]);
	});

	it('still keeps the last prose of the turn, however short', () => {
		const items = buildDisplayItems([user('go'), tool(), text('Done.')], SETTLED);
		expect(collapsedVisibleMessages(items).map((m) => m.content)).toContain('Done.');
	});

	it('leaves routine tools folded as before', () => {
		const read = tool({ toolName: 'Read' });
		const items = buildDisplayItems([user('go'), read, text('Line one.\nLine two.'), tool(), text('Done.')], SETTLED);
		expect(collapsedVisibleMessages(items).some((m) => m.id === read.id)).toBe(false);
	});
});

/**
 * Progressive live folding (transcript-organization spec §2): the live turn is
 * one work log cut into runs at each protected row. Earlier runs fold, the row
 * that ended each stays visible, and the current run is open.
 */
describe('live runs', () => {
	type WorkLog = Extract<DisplayItem, { kind: 'work_log' }>;
	const runsOf = (log: WorkLog) => log.runs!.map((run) => [run.index, run.folded.map((entry) => entry.message)]);

	it.each([
		['a finding in prose', () => text('Found it:\n- the token is shared')],
		['a question', () => tool({ toolName: 'AskUserQuestion', toolInput: { questions: [] } })],
		['a worker card', () => subagent({ agentStatus: 'running' })],
		['a special tool', () => tool({ toolName: 'TodoWrite', toolInput: { todos: [] } })]
	])('ends a run at %s, which stays visible below its fold', (_label, boundary) => {
		const prompt = user('go', undefined, 'turn-1');
		const [a, b, edge, c, d] = [tool(), tool(), boundary(), tool(), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems([prompt, a, b, edge, c, d], LIVE);
		const log = liveLog(items)!;
		expect(runsOf(log)).toEqual([[0, [a, b]]]);
		expect(log.runs![0].summary).toBe(summarizeWork(workEntries([a, b])));
		// Earlier run folded; the boundary and the whole current run stay open.
		expect(collapsedVisibleMessages(items)).toEqual([prompt, edge, c, d]);
		expect(log.sequence.map((entry) => entry.run)).toEqual([0, 0, 0, 1, 1]);
		expect(log.sequence.filter((entry) => entry.foldsInRun).map((entry) => entry.message)).toEqual([a, b]);
	});

	it('keeps every row of the current run open, with no fold before the first boundary', () => {
		const messages = [user('go'), tool(), tool({ toolStatus: 'error' }), tool(), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems(messages, LIVE);
		expect(liveLog(items)!.runs).toEqual([]);
		expect(collapsedVisibleMessages(items)).toEqual(messages);
	});

	it('folds a failed tool with its run and counts it, keeping it in the expanded run', () => {
		const prompt = user('go', undefined, 'turn-1');
		const [a, failed, b] = [tool(), tool({ toolStatus: 'error', toolResult: 'boom' }), tool()];
		const finding = text('Found it:\n- the token is shared');
		const tail = tool({ toolStatus: 'running', streaming: true });
		const items = buildDisplayItems([prompt, a, failed, b, finding, tail], LIVE);
		const log = liveLog(items)!;
		expect(runsOf(log)).toEqual([[0, [a, failed, b]]]);
		expect(log.runs![0].summary).toBe('Ran 3 commands, 1 failed');
		expect(log.runs![0].compactSummary).toBe('Ran 3 · 1 failed');
		// Folded, only the boundary and the current run show; the failure is a
		// row of the run, so opening the run shows it in place.
		expect(collapsedVisibleMessages(items)).toEqual([prompt, finding, tail]);
		expect(log.sequence.find((entry) => entry.message === failed)).toMatchObject({ run: 0, foldsInRun: true });
	});

	it('ends a run at the latest completed one-line prose, folding the work behind it', () => {
		const prompt = user('go', undefined, 'turn-1');
		const [a, b] = [tool(), tool()];
		const narration = text('Now the workflow.');
		const running = tool({ toolStatus: 'running', streaming: true });
		const items = buildDisplayItems([prompt, a, b, narration, running], LIVE);
		expect(runsOf(liveLog(items)!)).toEqual([[0, [a, b]]]);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, narration, running]);
		// Newer prose takes over: the earlier narration folds into the next run.
		const ran = { ...running, toolStatus: 'complete' as const, streaming: false };
		const [c, latest] = [tool(), text('Workflow checked.')];
		const after = buildDisplayItems([prompt, a, b, narration, ran, c, latest], LIVE);
		expect(runsOf(liveLog(after)!)).toEqual([[0, [a, b, narration, ran, c]]]);
		expect(collapsedVisibleMessages(after)).toEqual([prompt, latest]);
		// While the next prose streams, the last finished prose still ends the
		// run, so nothing unfolds for the moment the new paragraph arrives.
		const streaming = msg({ type: 'text', content: 'Writing', streaming: true });
		const mid = buildDisplayItems([prompt, a, b, narration, streaming], LIVE);
		expect(runsOf(liveLog(mid)!)).toEqual([[0, [a, b]]]);
		expect(collapsedVisibleMessages(mid)).toEqual([prompt, narration, streaming]);
	});

	// The owner's coordinator turn (2026-10-11): a failed SendMessage and Bash
	// mid-run, a task notification, a Monitor and one-line prose between them
	// used to leave a lone row at every one of those points.
	it('folds a coordinator turn into prose between runs', () => {
		const prompt = user('Merge the batch', undefined, 'turn-1');
		const send = tool({ toolName: 'mcp__fluid_bundled__mcp__builtins__SendMessage', toolStatus: 'error' });
		const [bash1, bash2] = [tool(), tool({ toolStatus: 'error' })];
		const getTask = tool({ toolName: 'mcp__fluid_bundled__mcp__builtins__TaskGet' });
		const table = text('Batch landed:\n\n| PR | Merge |\n| --- | --- |\n| #2291 | 6860e83 |');
		const notice = msg({ role: 'system', type: 'system', systemSubtype: 'task_notification', systemData: { status: 'completed' } });
		const narration = text('Attest passed. Checking the publish run.');
		const [bash3, bash4] = [tool(), tool()];
		const monitor = tool({ toolName: 'mcp__fluid_bundled__mcp__builtins__Monitor', toolStatus: 'running', streaming: true });
		const watching = text('A fresh attest is now running.');
		const messages = [prompt, send, bash1, bash2, getTask, table, notice, narration, bash3, bash4, monitor, watching];
		const items = buildDisplayItems(messages, LIVE);
		const log = liveLog(items)!;
		expect(log.runs!.map((run) => run.summary)).toEqual([
			'Sent 1 message, ran 2 commands, and checked 1 task, 2 failed',
			'Ran 2 commands and got 1 task result'
		]);
		expect(log.runs!.map((run) => run.compactSummary)).toEqual([
			'Sent 1 · Ran 2 · Checked 1 · 2 failed',
			'Ran 2 · Results 1'
		]);
		// The Monitor still runs, so it keeps its place between the folds.
		expect(collapsedVisibleMessages(items)).toEqual([prompt, table, monitor, watching]);
	});

	it('folds each earlier run on its own and leaves the current one open', () => {
		const prompt = user('go');
		const [a, b] = [tool(), tool()];
		const first = text('First finding:\n- one');
		const [c, narration] = [tool(), text('Checking more.')];
		const second = text('Second finding:\n- two');
		const [d, e] = [tool(), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems([prompt, a, b, first, c, narration, second, d, e], LIVE);
		expect(runsOf(liveLog(items)!)).toEqual([[0, [a, b]], [1, [c, narration]]]);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, first, second, d, e]);
	});

	it('keys each run by the row it starts at, so a later boundary does not shift it', () => {
		const prompt = user('go', undefined, 'turn-1');
		const [a, b] = [tool(), msg({ type: 'text', content: 'Found:\n- partial', streaming: true })];
		const finding = text('Found it:\n- one');
		const [c, d] = [tool(), tool()];
		const worker = subagent({ toolId: 'task-1', agentStatus: 'running' });
		const tail = tool({ toolStatus: 'running', streaming: true });
		const before = liveLog(buildDisplayItems([prompt, a, b, finding, c, d, worker, tail], LIVE))!;
		expect(before.runs!.map((run) => [run.index, run.key])).toEqual([[0, a.id], [1, c.id]]);
		// `b` finishes streaming: it becomes a boundary inside the first run,
		// so ordinals shift, but the run [c, d] keeps the key it starts at.
		const done = { ...b, streaming: false };
		const after = liveLog(buildDisplayItems([prompt, a, done, finding, c, d, worker, tail], LIVE))!;
		expect(after.runs!.map((run) => [run.key, run.folded.map((entry) => entry.message)]))
			.toEqual([[a.id, [a]], [c.id, [c, d]]]);
		expect(after.runs!.find((run) => run.key === c.id)!.index)
			.not.toBe(before.runs!.find((run) => run.key === c.id)!.index);
	});

	// Review of #2297: keyed by the prose that ended it, a run the reader had
	// opened changed key, and folded shut, when newer prose finished.
	it('keeps a run\'s key when newer prose finishes and replaces the boundary that ended it', () => {
		const prompt = user('go', undefined, 'turn-1');
		const read = tool({ toolName: 'Read' });
		const first = text('Checking the config.');
		const keyOf = (rows: ChatMessage[]) => liveLog(buildDisplayItems([prompt, ...rows], LIVE))!.runs!.map((run) => run.key);
		expect(keyOf([read, first])).toEqual([read.id]);
		const next = msg({ type: 'text', content: 'Config checked.', streaming: true });
		expect(keyOf([read, first, next])).toEqual([read.id]);
		expect(keyOf([read, first, tool(), { ...next, streaming: false }])).toEqual([read.id]);
	});

	it('keys a run started by an optimistic row by its server id once reconciled', () => {
		const prompt = user('go', undefined, 'turn-1');
		const a = { ...tool(), serverId: 'srv-9' };
		const finding = text('Found it:\n- one');
		const tail = tool({ toolStatus: 'running', streaming: true });
		const log = liveLog(buildDisplayItems([prompt, a, finding, tail], LIVE))!;
		expect(log.runs!.map((run) => run.key)).toEqual(['srv-9']);
	});

	it('does not end a run at a steer: it stays visible in place inside the current run', () => {
		const prompt = user('go', undefined, 'turn-1');
		const a = tool();
		const steer = user('check prod too', undefined, 'turn-1');
		const b = tool({ toolStatus: 'running', streaming: true });
		const items = buildDisplayItems([prompt, a, steer, b], LIVE);
		const log = liveLog(items)!;
		expect(log.runs).toEqual([]);
		expect(log.sequence.map((entry) => entry.run)).toEqual([0, 0, 0]);
		expect(collapsedVisibleMessages(items)).toEqual([prompt, a, steer, b]);
	});

	// Spec §1.3: a turn never folds because the user sent something.
	it.each([
		['after completed work', () => [tool(), tool()]],
		['after narration', () => [tool(), text('Checking more.'), tool()]],
		['after a finding and more work', () => [tool(), text('Found it:\n- one'), tool(), text('Hmm.')]],
		['while a tool runs', () => [tool(), tool({ toolStatus: 'running', streaming: true })]],
		['after a question', () => [tool(), text('Which one?'), tool({ toolName: 'AskUserQuestion', toolInput: { questions: [] } })]]
	])('never hides a visible row when a same-turn steer arrives %s', (_label, work) => {
		const prompt = user('go', undefined, 'turn-1');
		const before = [prompt, ...work()];
		const visibleBefore = collapsedVisibleMessages(buildDisplayItems(before, LIVE));
		const steer = user('also check prod', undefined, 'turn-1');
		const visibleAfter = collapsedVisibleMessages(buildDisplayItems([...before, steer], LIVE));
		for (const row of visibleBefore) expect(visibleAfter).toContain(row);
		expect(visibleAfter).toContain(steer);
	});

	it('does not end a run at prose that is still streaming', () => {
		const prompt = user('go');
		const a = tool();
		const streaming = msg({ type: 'text', content: 'Found it:\n- partial', streaming: true });
		const items = buildDisplayItems([prompt, a, streaming], LIVE);
		expect(liveLog(items)!.runs).toEqual([]);
		const done = buildDisplayItems([prompt, a, { ...streaming, streaming: false }], LIVE);
		expect(runsOf(liveLog(done)!)).toEqual([[0, [a]]]);
	});

	it('emits no runs and no work log when grouping is off', () => {
		const messages = [user('go'), tool(), text('Found it:\n- x'), tool()];
		const items = buildDisplayItems(messages, { isStreaming: true, groupingEnabled: false });
		expect(items.every((item) => item.kind === 'message')).toBe(true);
		expect(collapsedVisibleMessages(items)).toEqual(messages);
	});

	it('keys the live log exactly as the settled fold, and by server id for a reconciled send', () => {
		const prompt = { ...user('go'), serverId: 'srv-1' };
		const messages = [prompt, tool(), text('Found it:\n- x'), tool(), text('done')];
		const live = liveLog(buildDisplayItems(messages, LIVE))!;
		const settled = settledFolds(buildDisplayItems(messages, SETTLED))[0] as WorkLog;
		expect(live.key).toBe(`wl-turn-${prompt.id}`);
		expect(settled.key).toBe(live.key);
		expect(live.foldKey).toBe('srv-1');
		expect(live.foldAlias).toBe(prompt.id);
		expect(settled.foldKey).toBe('srv-1');
	});

	it('reuses the model on the fast path while the current run streams', () => {
		const build = createTranscriptModelBuilder();
		const prompt = user('go');
		const head = [prompt, tool(), text('Found it:\n- x'), tool()];
		const tail = msg({ type: 'text', content: 'Writing', streaming: true });
		const before = build([...head, tail], LIVE);
		const next = [...head, { ...tail, content: 'Writing more' }];
		const after = build(next, LIVE);
		expect(after.items[0]).toBe(before.items[0]);
		expect(after).toEqual(buildTranscriptModel(next, LIVE));
	});
});
