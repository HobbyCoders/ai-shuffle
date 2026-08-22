/**
 * Behavior tests for the transcript's turn grouping and settled disclosures.
 */
import { describe, it, expect } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import {
	buildDisplayItems,
	formatDuration,
	summarizeWork,
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

function user(content: string, timestamp?: string): ChatMessage {
	return msg({ role: 'user', content, timestamp });
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

	it('never folds agent, todo, note, question, or subagent surfaces', () => {
		const messages = [
			user('go'),
			tool(),
			msg({ type: 'subagent', agentName: 'Explore' }),
			tool({ toolName: 'TodoWrite' }),
			tool({ toolName: 'Note' }),
			tool({ toolName: 'Task' }),
			tool({ toolName: 'Agent' }),
			tool({ toolName: 'AskUserQuestion' }),
			text('done')
		];
		const visibleMessages = collapsedVisibleMessages(buildDisplayItems(messages, SETTLED));

		expect(visibleMessages.some((message) => message.type === 'subagent')).toBe(true);
		for (const name of ['TodoWrite', 'Note', 'Task', 'Agent', 'AskUserQuestion']) {
			expect(visibleMessages.some((message) => message.toolName === name)).toBe(true);
		}
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
});

describe('buildDisplayItems — live turn', () => {
	it('keeps the newest running tool visible and discloses older completed work', () => {
		const messages = [user('go'), tool(), tool(), tool({ toolStatus: 'running', streaming: true })];
		const items = buildDisplayItems(messages, LIVE);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.mode).toBe('live');
		expect(fold.folded).toHaveLength(2);
		expect(fold.tail).toHaveLength(1);
		expect(fold.tail[0].message.id).toBe(messages[3].id);
		expect(fold.summary).toBe('Ran 2 commands');
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

	it('ignores an empty assistant phantom when identifying the live tool tail', () => {
		const running = tool({ toolStatus: 'running', streaming: true });
		const items = buildDisplayItems(
			[user('go'), tool(), tool(), running, text('')],
			LIVE
		);
		const fold = items.find((item) => item.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.mode).toBe('live');
		expect(fold.tail[0].message.id).toBe(running.id);
		expect(items.some((item) => item.kind === 'message' && item.message.content === '')).toBe(false);
	});

	it('settles earlier turns while the final turn streams', () => {
		const messages = [user('one'), tool(), text('answer'), user('two'), tool({ toolStatus: 'running' })];
		const folds = buildDisplayItems(messages, LIVE).filter((item) => item.kind === 'work_log');
		expect(folds).toHaveLength(1);
		if (folds[0].kind !== 'work_log') throw new Error('expected a work_log');
		expect(folds[0].mode).toBe('settled');
	});

	it('does not fold a parked or otherwise unsettled latest turn', () => {
		const items = buildDisplayItems([user('go'), tool(), text('waiting on background work')], {
			isStreaming: false,
			latestTurnUnsettled: true,
			groupingEnabled: true
		});
		expect(items.some((item) => item.kind === 'work_log' && item.mode === 'settled')).toBe(false);
		expect(items.some((item) => item.kind === 'message' && item.message.content === 'waiting on background work')).toBe(true);
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

		const failedCell = workEntries([
			tool({ toolName: 'run_python', toolStatus: 'error' }),
			tool({ toolName: 'Bash', toolStatus: 'complete' })
		]);
		expect(summarizeWork(failedCell)).toBe('Used 1 tool and ran 1 command, 1 failed');
	});

	it('describes reasoning-only and empty groups', () => {
		expect(summarizeWork(workEntries([msg({ type: 'thinking', content: 'hmm' })])))
			.toBe('Reasoning and interim work');
		expect(summarizeWork([])).toBeNull();
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
