/**
 * Behavior tests for the transcript's turn grouping.
 *
 * These pin the rules that decide what a user can and cannot see at a glance:
 * a live turn keeps its newest step on screen, a settled turn keeps its answer
 * on screen, and failures / screenshots / subagents are never hidden.
 */
import { describe, it, expect } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { buildDisplayItems, formatDuration, turnDurationMs, turnWasInterrupted } from './work-log';

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

const SETTLED = { isStreaming: false, groupingEnabled: true };
const LIVE = { isStreaming: true, groupingEnabled: true };

describe('buildDisplayItems — settled turns', () => {
	it('folds each run of tools separately, keeping every assistant message visible', () => {
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

		expect(items.map((i) => i.kind)).toEqual([
			'message', // user
			'work_log', // first two tools
			'message', // progress so far
			'work_log', // second two tools
			'message' // done
		]);
		for (const fold of items.filter((i) => i.kind === 'work_log')) {
			if (fold.kind !== 'work_log') continue;
			expect(fold.mode).toBe('settled');
			expect(fold.folded).toHaveLength(2);
			expect(fold.tail).toHaveLength(0);
		}
		const commentary = items[2];
		if (commentary.kind !== 'message') throw new Error('expected a message');
		expect(commentary.message.content).toBe('progress so far');
	});

	it('renders a lone tool between assistant messages flat, with no fold chrome', () => {
		const messages = [user('do it'), tool(), text('looking'), tool(), text('done')];
		const items = buildDisplayItems(messages, SETTLED);
		expect(items.every((i) => i.kind === 'message')).toBe(true);
		expect(items).toHaveLength(5);
	});

	it('folds thinking blocks into the run alongside tools', () => {
		const messages = [user('go'), msg({ type: 'thinking', content: 'hmm' }), tool(), text('done')];
		const items = buildDisplayItems(messages, SETTLED);
		const fold = items.find((i) => i.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.folded).toHaveLength(2);
	});

	it('never folds a failed tool row', () => {
		const messages = [user('go'), tool(), tool({ toolStatus: 'error' }), text('done')];
		const items = buildDisplayItems(messages, SETTLED);

		const visible = items.filter((i) => i.kind === 'message');
		expect(visible.some((i) => i.kind === 'message' && i.message.toolStatus === 'error')).toBe(true);
	});

	it('never folds a row that produced a screenshot', () => {
		const messages = [user('go'), tool(), tool({ toolScreenshot: 'data:image/png;base64,x' }), text('done')];
		const items = buildDisplayItems(messages, SETTLED);

		expect(
			items.some((i) => i.kind === 'message' && !!i.message.toolScreenshot)
		).toBe(true);
	});

	it('never folds subagent cards, todo lists or questions', () => {
		const messages = [
			user('go'),
			tool(),
			msg({ type: 'subagent', agentName: 'Explore' }),
			tool({ toolName: 'TodoWrite' }),
			tool({ toolName: 'Note' }),
			tool({ toolName: 'AskUserQuestion' }),
			text('done')
		];
		const items = buildDisplayItems(messages, SETTLED);
		const kinds = items
			.filter((i) => i.kind === 'message')
			.map((i) => (i.kind === 'message' ? (i.message.type === 'subagent' ? 'subagent' : i.message.toolName) : ''));

		expect(kinds).toContain('subagent');
		expect(kinds).toContain('TodoWrite');
		expect(kinds).toContain('Note');
		expect(kinds).toContain('AskUserQuestion');
	});

	it('labels the fold with the run duration', () => {
		const messages = [
			user('go', '2026-08-19T10:00:00.000Z'),
			tool({ timestamp: '2026-08-19T10:00:30.000Z' }),
			tool({ timestamp: '2026-08-19T10:02:14.000Z' }),
			text('done', '2026-08-19T10:02:14.000Z')
		];
		const items = buildDisplayItems(messages, SETTLED);
		const fold = items.find((i) => i.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('Worked for 1m 44s');
	});

	it('says "Stopped after" on the final run of an interrupted turn', () => {
		const messages = [
			user('go', '2026-08-19T10:00:00.000Z'),
			text('half an answer\n\n[Stopped]', '2026-08-19T10:00:02.000Z'),
			tool({ timestamp: '2026-08-19T10:00:05.000Z' }),
			tool({ timestamp: '2026-08-19T10:00:12.000Z' })
		];
		const items = buildDisplayItems(messages, SETTLED);
		const fold = items.find((i) => i.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('Stopped after 7s');
	});

	it('falls back to a tool count when no timestamps are available', () => {
		const items = buildDisplayItems([user('go'), tool(), tool(), text('done')], SETTLED);
		const fold = items.find((i) => i.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');
		expect(fold.durationLabel).toBe('2 tool calls');
	});
});

describe('buildDisplayItems — the live turn', () => {
	it('keeps the newest tool row visible and folds the earlier ones', () => {
		const messages = [user('go'), tool(), tool(), tool()];
		const items = buildDisplayItems(messages, LIVE);
		const fold = items.find((i) => i.kind === 'work_log');
		if (fold?.kind !== 'work_log') throw new Error('expected a work_log');

		expect(fold.mode).toBe('live');
		expect(fold.folded).toHaveLength(2);
		expect(fold.tail).toHaveLength(1);
		expect(fold.tail[0].message.id).toBe(messages[3].id);
	});

	it('does not fold live commentary', () => {
		const messages = [user('go'), tool(), text('working on it'), tool()];
		const items = buildDisplayItems(messages, LIVE);

		expect(
			items.some((i) => i.kind === 'message' && i.message.content === 'working on it')
		).toBe(true);
	});

	it('renders a single tool row plainly, with no fold chrome', () => {
		const items = buildDisplayItems([user('go'), tool()], LIVE);
		expect(items.map((i) => i.kind)).toEqual(['message', 'message']);
	});

	it('folds earlier turns even while the last one streams', () => {
		const messages = [user('one'), tool(), tool(), text('answer'), user('two'), tool()];
		const items = buildDisplayItems(messages, LIVE);
		const folds = items.filter((i) => i.kind === 'work_log');
		expect(folds).toHaveLength(1);
		if (folds[0].kind !== 'work_log') throw new Error('expected a work_log');
		expect(folds[0].mode).toBe('settled');
	});
});

describe('buildDisplayItems — misc', () => {
	it('renders everything flat when grouping is disabled', () => {
		const messages = [user('go'), tool(), tool(), text('done')];
		const items = buildDisplayItems(messages, { isStreaming: false, groupingEnabled: false });
		expect(items.every((i) => i.kind === 'message')).toBe(true);
		expect(items).toHaveLength(4);
	});

	it('drops duplicate ids so the keyed each cannot crash', () => {
		const dup = tool();
		const items = buildDisplayItems([user('go'), dup, dup, text('done')], SETTLED);
		const ids = items.flatMap((i) =>
			i.kind === 'work_log' ? i.folded.map((f) => f.message.id) : i.kind === 'message' ? [i.message.id] : []
		);
		expect(new Set(ids).size).toBe(ids.length);
	});

	it('groups consecutive low-signal system messages', () => {
		const messages = [
			user('go'),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_started' }),
			msg({ role: 'system', type: 'system', systemSubtype: 'hook_response' }),
			text('done')
		];
		const items = buildDisplayItems(messages, SETTLED);
		expect(items.some((i) => i.kind === 'system_group')).toBe(true);
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

	it('needs two usable stamps', () => {
		expect(turnDurationMs([text('a')])).toBeNull();
		expect(turnDurationMs([text('a', 'nonsense'), text('b', 'also nonsense')])).toBeNull();
		expect(
			turnDurationMs([text('a', '2026-08-19T10:00:00Z'), text('b', '2026-08-19T10:00:09Z')])
		).toBe(9000);
	});

	it('detects the interrupt marker', () => {
		expect(turnWasInterrupted([text('partial\n\n[Stopped]')])).toBe(true);
		expect(turnWasInterrupted([text('all done')])).toBe(false);
	});
});
