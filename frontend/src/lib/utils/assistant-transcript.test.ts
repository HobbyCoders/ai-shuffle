import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { buildTranscriptModel, type DisplayItem } from './work-log';
import { assistantRowRole, foldAssistantTranscript, isStreamingProse } from './assistant-transcript';

const msg = (id: string, patch: Partial<ChatMessage>): ChatMessage => ({ id, role: 'assistant', content: '', ...patch });
const user = (id: string, content: string, turnId = `t-${id}`) => msg(id, { role: 'user', content, type: 'text', turnId });
const text = (id: string, content: string) => msg(id, { type: 'text', content });
const tool = (id: string, toolName = 'Bash') => msg(id, { type: 'tool_use', toolName, toolId: `tool-${id}`, toolInput: { command: 'ls' }, toolStatus: 'complete', toolResult: 'ok' });
const thinking = (id: string) => msg(id, { type: 'thinking', content: 'pondering' });

function fold(messages: ChatMessage[], live = false) {
	const model = buildTranscriptModel(messages, { isStreaming: live, latestTurnUnsettled: live, sessionId: 'home', swarms: [], groupingEnabled: true });
	return foldAssistantTranscript(model.items, live);
}

const shape = (items: ReturnType<typeof fold>['items']) =>
	items.map((item) => item.kind === 'details' ? `details(${item.entries.map(e => e.message.id).join(',')})` : item.kind === 'message' ? item.message.id : item.kind);

describe('assistantRowRole', () => {
	it('keeps prompts and prose, folds everything else', () => {
		expect(assistantRowRole(user('u', 'hi'))).toBe('prompt');
		expect(assistantRowRole(msg('c', { role: 'user', type: 'command', content: '/x' }))).toBe('prompt');
		expect(assistantRowRole(text('a', 'Done.'))).toBe('spoken');
		for (const m of [tool('t'), thinking('k'), msg('s', { role: 'system', type: 'system', systemSubtype: 'permission_mode_changed' }), msg('sa', { type: 'subagent' }), msg('n', { type: 'tool_use', toolName: 'Task' })]) {
			expect(assistantRowRole(m)).toBe('detail');
		}
	});

	it('folds approval outcomes and reports from started chats under Details', () => {
		const outcome = msg('o', { role: 'system', type: 'system', systemSubtype: 'assistant_confirm', content: 'Authorized: Do you authorize me to run `git push`?', systemData: { decision: 'approved' } });
		const report = msg('r', { role: 'system', type: 'system', systemSubtype: 'task_notification', systemData: { kind: 'session', status: 'completed', result: 'raw output' } });
		expect(assistantRowRole(outcome)).toBe('detail');
		expect(assistantRowRole(report)).toBe('detail');
		const { items } = foldAssistantTranscript([user('u1', 'push it'), outcome, report, text('a1', 'Pushed. Checks are green.')].map((message, index) => ({ kind: 'message' as const, message, index })));
		expect(shape(items)).toEqual(['u1', 'a1', 'details(o,r)']);
	});

	it('treats an agent delivery as detail even though it arrives in the user role', () => {
		const delivery = msg('d', { role: 'system', type: 'system', systemSubtype: 'agent_message', systemData: { sender: 's1', content: 'done' } });
		expect(assistantRowRole(delivery)).toBe('detail');
	});
});

describe('foldAssistantTranscript', () => {
	it('puts a settled turn\'s work behind one Details item after the reply', () => {
		const { items } = fold([user('u1', 'fix it'), thinking('k1'), tool('t1'), text('a0', 'Looking.'), tool('t2'), text('a1', 'Fixed.')]);
		expect(shape(items)).toEqual(['u1', 'a0', 'a1', 'details(k1,t1,t2)']);
	});

	it('keeps each turn\'s details with that turn', () => {
		const { items } = fold([user('u1', 'one'), tool('t1'), text('a1', 'First.'), user('u2', 'two'), tool('t2'), text('a2', 'Second.')]);
		expect(shape(items)).toEqual(['u1', 'a1', 'details(t1)', 'u2', 'a2', 'details(t2)']);
	});

	it('shows no Details item for a turn that only talked', () => {
		const { items } = fold([user('u1', 'hello'), text('a1', 'Hi there.')]);
		expect(shape(items)).toEqual(['u1', 'a1']);
	});

	it('marks only the running turn as live, even before any words arrive', () => {
		const { items } = fold([user('u1', 'one'), tool('t1'), text('a1', 'Done.'), user('u2', 'two'), tool('t2')], true);
		const details = items.filter((item) => item.kind === 'details');
		expect(details.map((d) => d.kind === 'details' && d.live)).toEqual([false, true]);
		expect(shape(items)).toEqual(['u1', 'a1', 'details(t1)', 'u2', 'details(t2)']);
	});

	it('folds system groups and keeps keys stable', () => {
		const group: DisplayItem = { kind: 'system_group', messages: [msg('s1', { role: 'system', type: 'system' }), msg('s2', { role: 'system', type: 'system' })], indices: [1, 2] };
		const items: DisplayItem[] = [{ kind: 'message', message: user('u1', 'go'), index: 0 }, group, { kind: 'message', message: text('a1', 'Ok.'), index: 3 }];
		const first = foldAssistantTranscript(items);
		const second = foldAssistantTranscript(items);
		expect(shape(first.items)).toEqual(['u1', 'a1', 'details(s1,s2)']);
		expect(first.items.map((i) => i.kind === 'details' ? i.key : '')).toEqual(second.items.map((i) => i.kind === 'details' ? i.key : ''));
	});

	it('keeps the original message items so rows are not remounted', () => {
		const items: DisplayItem[] = [{ kind: 'message', message: user('u1', 'go'), index: 0 }, { kind: 'message', message: text('a1', 'Ok.'), index: 1 }];
		const folded = foldAssistantTranscript(items);
		expect(folded.items[0]).toBe(items[0]);
		expect(folded.items[1]).toBe(items[1]);
	});

	it('indexes user turns against the folded list for turn navigation', () => {
		const { items, itemIndexByTurnId } = fold([user('u1', 'one'), tool('t1'), text('a1', 'A.'), user('u2', 'two'), text('a2', 'B.')]);
		expect(items[itemIndexByTurnId.get('u2')!]).toMatchObject({ kind: 'message', message: { id: 'u2' } });
		expect(itemIndexByTurnId.get('u1')).toBe(0);
	});
});

describe('isStreamingProse', () => {
	it('is true only for streaming assistant words with content', () => {
		expect(isStreamingProse(msg('a', { type: 'text', content: 'Hi', streaming: true }))).toBe(true);
		expect(isStreamingProse(msg('a', { type: 'text', content: '', streaming: true }))).toBe(false);
		expect(isStreamingProse(msg('t', { type: 'tool_use', streaming: true, content: 'x' }))).toBe(false);
		expect(isStreamingProse(undefined)).toBe(false);
	});
});
