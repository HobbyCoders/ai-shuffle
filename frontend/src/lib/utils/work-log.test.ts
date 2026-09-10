/**
 * Behavior tests for the transcript's turn grouping and settled disclosures.
 */
import { describe, it, expect } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { initialState, reduce, type WireEvent } from '$lib/stores/session/v2/reducer';
import {
	buildDisplayItems,
	buildTranscriptModel,
	hasLinkedSubagent,
	userPromptOccurrenceAt,
	formatDuration,
	summarizeWork,
	summarizeWorkCompact,
	turnDurationMs,
	turnWasInterrupted,
	type DisplayItem,
	type WorkLogEntry
} from './work-log';

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
	return msg({ type: 'subagent', agentName: 'Explore', agentStatus: 'completed', ...over });
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
				.filter((entry) => !entry.hiddenWhenCollapsed)
				.map((entry) => entry.message);
		}
		return item.tail.map((entry) => entry.message);
	});
}

const SETTLED = { isStreaming: false, groupingEnabled: true };
const LIVE = { isStreaming: true, groupingEnabled: true };

describe('Swarm workflow visibility', () => {
 it.each(['Swarm', 'mcp__fluid__Swarm'])('keeps %s outside settled work folds', (toolName) => {
  const workflow = tool({ toolName });
  const messages = [user('Review release'), tool(), workflow, text('Agents are working.')];
  expect(collapsedVisibleMessages(buildDisplayItems(messages, SETTLED))).toContain(workflow);
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

	it('folds settled subagent cards and counts them as delegated work', () => {
		const first = subagent();
		const second = subagent({ agentStatus: 'error' });
		const items = buildDisplayItems(
			[user('go'), tool({ toolName: 'Grep' }), first, second, text('done')],
			SETTLED
		);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.summary).toBe('Searched code 1 time and delegated 2 tasks');
		for (const card of [first, second]) {
			expect(fold.folded.map((entry) => entry.message.id)).toContain(card.id);
			expect(fold.sequence.find((entry) => entry.message.id === card.id)?.hiddenWhenCollapsed).toBe(true);
		}
		expect(collapsedVisibleMessages(items).some((message) => message.type === 'subagent')).toBe(false);
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
			msg({ type: 'subagent', agentName: 'Explore' }),
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

		expect(folds).toHaveLength(1);
		if (folds[0].kind !== 'work_log') throw new Error('expected a work_log');
		expect(folds[0].durationLabel).toBe('Worked for 2m 52s');
		expect(folds[0].summary).toBe('Ran 2 commands');
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
		const folds = buildDisplayItems(messages, SETTLED).filter((item) => item.kind === 'work_log');

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

	it('folds a settled retry card instead of stranding it beside the turn', () => {
		const settledRetry = retryCard({ settled: true, attempt: 3 });
		const items = buildDisplayItems([user('go'), tool(), settledRetry, text('done')], SETTLED);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.folded.map((entry) => entry.message.id)).toContain(settledRetry.id);
		expect(fold.sequence.find((entry) => entry.message.id === settledRetry.id)?.hiddenWhenCollapsed)
			.toBe(true);
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
		expect(items.every((item) => item.kind === 'message')).toBe(true);
		expect(collapsedVisibleMessages(items)).toEqual(messages);
	});

	it('keeps live prose and streaming reasoning visible', () => {
		const prose = text('working on it');
		const thinking = msg({ type: 'thinking', content: 'active thought', streaming: true });
		const items = buildDisplayItems([user('go'), tool(), prose, thinking], LIVE);
		expect(items.some((item) => item.kind === 'message' && item.message.id === prose.id)).toBe(true);
		expect(items.some((item) => item.kind === 'message' && item.message.id === thinking.id)).toBe(true);
	});


	it('renders a single active tool plainly', () => {
		const items = buildDisplayItems(
			[user('go'), tool({ toolStatus: 'running', streaming: true })],
			LIVE
		);
		expect(items.map((item) => item.kind)).toEqual(['message', 'message']);
	});

	it('ignores an empty assistant phantom without hiding completed tools', () => {
		const messages = [user('go'), tool(), tool(), tool({ toolStatus: 'running', streaming: true })];
		expect(collapsedVisibleMessages(buildDisplayItems([...messages, text('')], LIVE))).toEqual(messages);
	});

	it('keeps completed tools visible between calls', () => {
		const messages = [user('go'), tool(), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual(messages);
	});

	it('keeps completed subagent cards visible alongside tools', () => {
		const messages = [user('go'), tool(), subagent(), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual(messages);
	});

	it('shows a streaming code-mode cell as the live tail instead of going silent', () => {
		const cell = tool({ toolName: 'run_python', toolStatus: 'running', streaming: true });
		// A turn whose only work is the streaming cell keeps the cell itself visible.
		const lone = buildDisplayItems([user('go'), cell], LIVE);
		expect(lone.some((item) => item.kind === 'message' && item.message.id === cell.id)).toBe(true);

		// Once the cell executes inner calls, only those calls need visible rows.
		const executing = tool({ toolName: 'run_python', toolStatus: 'running', streaming: false });
		const inner = tool({ toolName: 'Read', toolStatus: 'running', streaming: true });
		const prompt = user('go');
		const items = buildDisplayItems([prompt, executing, inner], LIVE);
		expect(items.some((item) => item.kind === 'work_log')).toBe(false);
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
		expect(folds).toHaveLength(1);
		if (folds[0].kind !== 'work_log') throw new Error('expected a work_log');
		expect(folds[0].mode).toBe('settled');
	});

	it('does not fold a parked or otherwise unsettled latest turn', () => {
		const items = buildDisplayItems([user('go'), tool(), tool(), text('waiting on background work'), tool(), tool()], {
			isStreaming: false,
			latestTurnUnsettled: true,
			groupingEnabled: true
		});
		expect(items.some((item) => item.kind === 'work_log')).toBe(false);
		expect(items.some((item) => item.kind === 'message' && item.message.content === 'waiting on background work')).toBe(true);
	});

	it('keeps settled retry cards visible until the turn finishes', () => {
		const messages = [user('go'), tool(), retryCard({ settled: true, attempt: 2 }), tool()];
		expect(collapsedVisibleMessages(buildDisplayItems(messages, LIVE))).toEqual(messages);
	});

	it('keeps an active retry card out of the live run', () => {
		const activeRetry = retryCard();
		const items = buildDisplayItems([user('go'), tool(), activeRetry, tool()], LIVE);

		expect(items.some((item) => item.kind === 'message' && item.message.id === activeRetry.id))
			.toBe(true);
		for (const item of items) {
			if (item.kind !== 'work_log') continue;
			expect([...item.folded, ...item.tail].map((entry) => entry.message.id))
				.not.toContain(activeRetry.id);
		}
	});
	it('keeps all rows across commentary boundaries, then folds once after the final response', () => {
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
			expect(live.every((item) => item.kind === 'message')).toBe(true);
			expect(collapsedVisibleMessages(live)).toEqual(prefix);
		}
		const settled = buildDisplayItems(messages, SETTLED);
		expect(settled.filter((item) => item.kind === 'work_log')).toHaveLength(1);
		expect(collapsedVisibleMessages(settled)).toEqual([messages[0], messages.at(-1)]);
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
			expect(items.every((item) => item.kind === 'message')).toBe(true);
			expect(collapsedVisibleMessages(items)).toEqual(state.messages);
		}
		const settled = apply('turn.completed');
		expect(settled.filter((item) => item.kind === 'work_log')).toHaveLength(1);
		expect(collapsedVisibleMessages(settled).map((message) => message.content))
			.toEqual(['Check two files', 'Both files checked.']);
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
	it('renders work flat when grouping is disabled while retaining system grouping', () => {
		const messages = [
			user('go'),
			tool(),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_started' }),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_response' }),
			text('done')
		];
		const items = buildDisplayItems(messages, { isStreaming: false, groupingEnabled: false });
		expect(items.some((item) => item.kind === 'work_log')).toBe(false);
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
	function linkedSubagentByScan(messages: ChatMessage[], toolId: string | undefined): boolean {
		return messages.some((m) => m.type === 'subagent' && m.toolId === toolId);
	}

	const SETTLED_OPTS = { isStreaming: false, groupingEnabled: true };

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
