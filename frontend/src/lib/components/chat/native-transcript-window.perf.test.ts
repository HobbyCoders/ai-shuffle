/** Deterministic model/mount-plan counters, not a browser frame-time benchmark.
 * Drive the native reducer so seq/epoch and copy-on-publish match real feeds. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { marked } from 'marked';
import { initialState, reduce, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { buildTranscriptModel, createTranscriptModelBuilder } from '$lib/utils/work-log';
import { createTranscriptWindow } from './transcript-window';

function nativeHistory() {
	let state = initialState();
	let seq = 0;
	const emit = (type: string, fields: Record<string, unknown> = {}) => {
		seq++;
		state = reduce(state, { type, id: `event-${seq}`, seq, session_id: 'perf-session',
			created_at: '2026-10-01T00:00:00Z', ...fields } as WireEvent);
	};
	for (let t = 0; t < 400; t++) {
		emit('turn.started', { turn_id: `turn-${t}` });
		emit('message.user', { content: `Question ${t}`, turn_id: `turn-${t}` });
		emit('tool.use', { tool_use_id: `tool-${t}`, name: 'Read', input: { file_path: 'example.ts' } });
		emit('tool.result', { tool_use_id: `tool-${t}`, output: 'done' });
		emit('assistant.thinking_delta', { text: `Reasoning ${t}` });
		emit('assistant.text_delta', { text: `Answer **${t}**\n\n![one](/api/files/one-${t}.png) ![two](/api/files/two-${t}.png)\n\n\`\`\`ts\nconst n = ${t};\n\`\`\`` });
		emit('subagent.started', { task_id: `worker-${t}`, description: `Review ${t}` });
		emit('subagent.completed', { task_id: `worker-${t}`, result: 'Reviewed' });
		emit('turn.completed');
	}
	return { emit, get state() { return state; } };
}

afterEach(() => vi.restoreAllMocks());

describe('2,000-row mixed native transcript budget', () => {
	it.each([
		{ kind: 'text', withTools: false }, { kind: 'text', withTools: true },
		{ kind: 'thinking', withTools: false }, { kind: 'thinking', withTools: true }
	])('patches native $kind deltas (tools: $withTools) without historical reads', ({ kind, withTools }) => {
		const feed = nativeHistory();
		expect(feed.state.messages).toHaveLength(2000);
		const history = [...releaseMessages(feed.state)];
		feed.emit('turn.started', { turn_id: 'live' });
		feed.emit('message.user', { content: 'Continue', turn_id: 'live' });
		if (withTools) {
			feed.emit('tool.use', { tool_use_id: 'live-tool', name: 'Read', input: {} });
			feed.emit('tool.result', { tool_use_id: 'live-tool', output: 'done' });
		}
		feed.emit(`assistant.${kind}_delta`, { text: 'Start' });
		const build = createTranscriptModelBuilder();
		const options = { isStreaming: true, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 'perf-session' };
		let model = build(releaseMessages(feed.state), options);
		const window = createTranscriptWindow({ cap: 120, step: 30 });
		window.setItemCount(model.items.length);
		let historicalContentReads = 0;
		for (const message of history) {
			const content = message.content;
			Object.defineProperty(message, 'content', { configurable: true,
				get() { historicalContentReads++; return content; } });
		}
		const lexer = vi.spyOn(marked, 'lexer');
		let changedItems = 0;
		let stableLookups = true;
		for (let delta = 0; delta < 20; delta++) {
			feed.emit(`assistant.${kind}_delta`, { text: ` token-${delta}` });
			const next = build(releaseMessages(feed.state), options);
			changedItems += next.items.filter((item, i) => item !== model.items[i]).length;
			stableLookups &&= next.promptOccurrence === model.promptOccurrence
				&& next.rewindEligible === model.rewindEligible;
			const range = window.setItemCount(next.items.length);
			expect(next.items.slice(range.start, range.end)).toHaveLength(120);
			model = next;
		}
		expect(historicalContentReads).toBe(0);
		expect(lexer).not.toHaveBeenCalled();
		expect(changedItems).toBe(20);
		expect(stableLookups).toBe(true);
		// Correctness oracle after counting: no fixture-only fields bypass production.
		expect(model).toEqual(buildTranscriptModel(feed.state.messages, options));
	});

	it('reuses unrelated groups during native worker progress without historical lexing', () => {
		const feed = nativeHistory();
		expect(feed.state.messages).toHaveLength(2000);
		const build = createTranscriptModelBuilder();
		const options = { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true, sessionId: 'perf-session' };
		let model = build(releaseMessages(feed.state), options);
		const lexer = vi.spyOn(marked, 'lexer');
		const window = createTranscriptWindow();
		for (let delta = 0; delta < 20; delta++) {
			feed.emit('subagent.progress', { task_id: 'worker-399', kind: 'chunk', note: `Progress ${delta}` });
			const next = build(releaseMessages(feed.state), options);
			expect(next.items.filter((item, i) => item !== model.items[i])).toHaveLength(1);
			expect(next.promptOccurrence).toBe(model.promptOccurrence);
			const range = window.setItemCount(next.items.length);
			expect(range.end - range.start).toBeLessThanOrEqual(120);
			model = next;
		}
		expect(lexer).not.toHaveBeenCalled();
		expect(model).toEqual(buildTranscriptModel(feed.state.messages, options));
	});
});
