/**
 * Regression: a text -> tool -> text assistant turn must render in stream
 * order with the two text blocks as SEPARATE bubbles (not the tool hoisted
 * above the text, and not the texts merged into one bubble).
 *
 * The actual reorder-on-reload bug was a BACKEND persistence issue (assistant
 * text was merged into one row appended after the tool rows; see
 * app/core/query_persistence.py / query_engine.py). These tests pin the
 * FRONTEND contract: the MessageArea grouping derivation must faithfully
 * preserve whatever order the messages array is in and must never merge two
 * distinct assistant text messages. This guards against a future regression
 * in the grouping logic re-introducing the symptom on the client side.
 *
 * The grouping logic below is a faithful copy of the `displayItems` derivation
 * in MessageArea.svelte (which is not importable from a .svelte component in a
 * plain vitest unit).
 */
import { describe, it, expect } from 'vitest';
import type { ChatMessage } from '../types';

type DisplayItem =
	| { kind: 'message'; message: ChatMessage; index: number }
	| { kind: 'tool_group'; tools: ChatMessage[]; indices: number[] }
	| { kind: 'system_group'; messages: ChatMessage[]; indices: number[] };

function isGroupableTool(msg: ChatMessage): boolean {
	return msg.type === 'tool_use'
		&& msg.toolName !== 'TodoWrite'
		&& msg.toolName !== 'Task'
		&& msg.toolName !== 'Agent';
}

const GROUPABLE_SYSTEM_SUBTYPES = new Set([
	'hook_started', 'hook_response', 'stop_hook_summary', 'turn_duration',
	'task_started', 'task_progress', 'task_updated', 'task_notification'
]);

function isGroupableSystem(msg: ChatMessage): boolean {
	return msg.type === 'system' && GROUPABLE_SYSTEM_SUBTYPES.has(msg.systemSubtype || '');
}

function buildDisplayItems(messages: ChatMessage[], groupingEnabled = true): DisplayItem[] {
	const items: DisplayItem[] = [];
	const seenIds = new Set<string>();
	let i = 0;
	while (i < messages.length) {
		const msg = messages[i];
		if (msg.id && seenIds.has(msg.id)) { i++; continue; }
		if (msg.id) seenIds.add(msg.id);
		if (groupingEnabled && isGroupableTool(msg)) {
			const tools: ChatMessage[] = [msg];
			const indices: number[] = [i];
			let j = i + 1;
			while (j < messages.length && isGroupableTool(messages[j])) {
				const toolMsg = messages[j];
				if (toolMsg.id && seenIds.has(toolMsg.id)) { j++; continue; }
				if (toolMsg.id) seenIds.add(toolMsg.id);
				tools.push(toolMsg);
				indices.push(j);
				j++;
			}
			if (tools.length >= 2) items.push({ kind: 'tool_group', tools, indices });
			else items.push({ kind: 'message', message: msg, index: i });
			i = j;
		} else if (isGroupableSystem(msg)) {
			const sysMsgs: ChatMessage[] = [msg];
			const indices: number[] = [i];
			let j = i + 1;
			while (j < messages.length && isGroupableSystem(messages[j])) {
				const sysMsg = messages[j];
				if (sysMsg.id && seenIds.has(sysMsg.id)) { j++; continue; }
				if (sysMsg.id) seenIds.add(sysMsg.id);
				sysMsgs.push(sysMsg);
				indices.push(j);
				j++;
			}
			if (sysMsgs.length >= 2) items.push({ kind: 'system_group', messages: sysMsgs, indices });
			else items.push({ kind: 'message', message: msg, index: i });
			i = j;
		} else {
			items.push({ kind: 'message', message: msg, index: i });
			i++;
		}
	}
	return items;
}

function shape(items: DisplayItem[]): string[] {
	return items.map(it => {
		if (it.kind === 'tool_group') return `toolgroup[${it.tools.map(t => t.toolName).join(',')}]`;
		if (it.kind === 'system_group') return 'sysgroup';
		const m = it.message;
		if (m.role === 'user') return 'user';
		if (m.type === 'tool_use') return `tool:${m.toolName}`;
		return `text:${m.content}`;
	});
}

const u = (id: string, content: string): ChatMessage =>
	({ id, role: 'user', type: undefined as never, content, streaming: false });
const text = (id: string, content: string): ChatMessage =>
	({ id, role: 'assistant', type: 'text', content, streaming: false });
const toolUse = (id: string, name: string): ChatMessage =>
	({ id, role: 'assistant', type: 'tool_use', toolName: name, toolId: id, content: '', streaming: false, toolStatus: 'complete' });

describe('MessageArea display order (text -> tool -> text)', () => {
	it('renders text, tool, text in order as three separate items', () => {
		const messages = [
			u('msg-1', 'run a check'),
			text('text-uuid-2', "I'll run a quick check."),
			toolUse('tool-toolu_01BASQ', 'Bash'),
			text('text-uuid-5', 'Output confirmed: ok'),
		];
		expect(shape(buildDisplayItems(messages))).toEqual([
			'user',
			"text:I'll run a quick check.",
			'tool:Bash',
			'text:Output confirmed: ok',
		]);
	});

	it('does NOT merge the two distinct assistant text blocks', () => {
		const messages = [
			text('text-uuid-2', 'first block'),
			toolUse('tool-1', 'Bash'),
			text('text-uuid-5', 'second block'),
		];
		const items = buildDisplayItems(messages);
		const textItems = items.filter(it => it.kind === 'message' && it.message.type === 'text');
		expect(textItems).toHaveLength(2);
		// And the tool is NOT hoisted to the front.
		expect((items[0] as { message: ChatMessage }).message.type).toBe('text');
	});

	it('does not group a single Bash tool between two texts into a tool_group', () => {
		const messages = [
			text('t1', 'a'),
			toolUse('tool-1', 'Bash'),
			text('t2', 'b'),
		];
		const items = buildDisplayItems(messages);
		expect(items.some(it => it.kind === 'tool_group')).toBe(false);
	});
});

describe('MessageArea tool grouping toggle (groupToolCalls)', () => {
	const consecutiveTools = () => [
		u('u-1', 'do the thing'),
		toolUse('tool-1', 'Bash'),
		toolUse('tool-2', 'Read'),
		toolUse('tool-3', 'Grep'),
	];

	it('flag on (default): consecutive tools still collapse into one tool_group', () => {
		expect(shape(buildDisplayItems(consecutiveTools(), true))).toEqual([
			'user',
			'toolgroup[Bash,Read,Grep]',
		]);
	});

	it('flag off: each tool renders as its own message item in original order', () => {
		expect(shape(buildDisplayItems(consecutiveTools(), false))).toEqual([
			'user',
			'tool:Bash',
			'tool:Read',
			'tool:Grep',
		]);
		// No tool_group items are produced when grouping is disabled.
		expect(buildDisplayItems(consecutiveTools(), false).some(it => it.kind === 'tool_group')).toBe(false);
	});

	it('flag off: system grouping is unaffected by the tool toggle', () => {
		const messages = [
			{ id: 's-1', role: 'system', type: 'system', systemSubtype: 'hook_started', content: '', streaming: false } as unknown as ChatMessage,
			{ id: 's-2', role: 'system', type: 'system', systemSubtype: 'hook_response', content: '', streaming: false } as unknown as ChatMessage,
		];
		const items = buildDisplayItems(messages, false);
		expect(items.some(it => it.kind === 'system_group')).toBe(true);
	});
});
