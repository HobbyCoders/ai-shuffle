import { describe, expect, it } from 'vitest';
import type { ChatMessage, Task } from '$lib/stores/types';
import { linkedTranscripts, startedChats, transcriptForTask } from './background-work';

const NOW = Date.parse('2026-10-03T12:00:00Z');
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();
const startSession = (id: string, sessionId: string, title = 'Started job'): ChatMessage => ({
	id, role: 'assistant', content: '', type: 'tool_use', toolName: 'mcp__fluid_bundled__mcp__builtins__StartSession',
	toolInput: { title, prompt: 'go', request_id: 'r' }, toolStatus: 'complete',
	toolResult: JSON.stringify({ session_id: sessionId, status: 'queued' }), timestamp: ago(9)
});

describe('startedChats', () => {
	it('lists chats it started that are still working, and skips idle ones', () => {
		const chats = startedChats({
			messages: [startSession('m1', 'chat-a', 'Release notes'), startSession('m2', 'chat-b'), startSession('m3', 'chat-c')],
			sessions: [
				{ id: 'chat-a', title: 'Writing the release notes', work_started_at: ago(12), device_name: 'OfficeWorker' },
				{ id: 'chat-b', title: 'Idle chat', remote_runtime_state: 'idle' },
				{ id: 'chat-c', title: 'Parked chat', parked_task_kind: 'shell', created_at: ago(30) }
			],
			streaming: new Map([['chat-a', {}]])
		});
		// chat-a has run 12 min (its work clock); chat-c counts from its StartSession call, 9 min ago.
		expect(chats).toEqual([
			{ id: 'chat-a', title: 'Writing the release notes', startedAt: Date.parse(ago(12)), worker: 'OfficeWorker' },
			{ id: 'chat-c', title: 'Parked chat', startedAt: Date.parse(ago(9)), worker: null }
		]);
	});

	it('counts a streaming started chat that is not in the loaded list, by its StartSession title', () => {
		const chats = startedChats({ messages: [startSession('m1', 'chat-x', 'Watching the release checks')], sessions: [], streaming: new Set(['chat-x']) });
		expect(chats.map((chat) => [chat.title, chat.id])).toEqual([['Watching the release checks', 'chat-x']]);
		expect(startedChats({ messages: [startSession('m1', 'chat-x')], sessions: [], streaming: new Set() })).toEqual([]);
	});

	it('lists a chat started twice once', () => {
		const chats = startedChats({
			messages: [startSession('m1', 'chat-a'), startSession('m2', 'chat-a')],
			sessions: [{ id: 'chat-a', title: 'x', remote_runtime_state: 'working' }],
			streaming: new Set()
		});
		expect(chats.map((chat) => chat.id)).toEqual(['chat-a']);
	});

	it('ignores failed StartSession calls', () => {
		const failed = { ...startSession('m1', 'chat-a'), toolStatus: 'error' as const };
		expect(startedChats({ messages: [failed], sessions: [{ id: 'chat-a', remote_runtime_state: 'working' }], streaming: new Set() })).toEqual([]);
	});
});

describe('transcriptForTask', () => {
	it('finds the worker chat a Task card links to', () => {
		const messages: ChatMessage[] = [{ id: 'card', role: 'assistant', content: '', type: 'subagent', toolId: 'launch-1', agentSessionId: 'worker-chat' }];
		const linked = linkedTranscripts(messages);
		const task = { id: 't1', toolUseId: 'launch-1' } as Pick<Task, 'id' | 'toolUseId'>;
		expect(transcriptForTask(task, linked)).toBe('worker-chat');
		expect(transcriptForTask({ id: 'other', toolUseId: null }, linked)).toBeNull();
	});
});
