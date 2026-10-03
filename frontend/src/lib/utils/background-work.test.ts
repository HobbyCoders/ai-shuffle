import { describe, expect, it } from 'vitest';
import type { ChatMessage, Task } from '$lib/stores/types';
import { buildBackgroundJobs, formatJobAge } from './background-work';

const NOW = Date.parse('2026-10-03T12:00:00Z');
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();
const task = (id: string, patch: Partial<Task> = {}): Task => ({
	id, subject: `Job ${id}`, status: 'running', kind: 'agent', summary: null, toolUseId: null,
	startedAt: ago(5), updatedAt: ago(1), agent: 'Frontend Designer', ...patch
});
const startSession = (id: string, sessionId: string, title = 'Started job'): ChatMessage => ({
	id, role: 'assistant', content: '', type: 'tool_use', toolName: 'mcp__fluid_bundled__mcp__builtins__StartSession',
	toolInput: { title, prompt: 'go', request_id: 'r' }, toolStatus: 'complete',
	toolResult: JSON.stringify({ session_id: sessionId, status: 'queued' }), timestamp: ago(9)
});

describe('buildBackgroundJobs', () => {
	it('lists running tasks with their linked chat, never ids, agents or models', () => {
		const messages: ChatMessage[] = [{ id: 'card', role: 'assistant', content: '', type: 'subagent', toolId: 'launch-1', agentSessionId: 'worker-chat', agentName: 'Frontend Designer', agentType: 'claude-opus-5-5' }];
		const jobs = buildBackgroundJobs({
			tasks: [task('t1', { subject: 'Fixing the sidebar bug', toolUseId: 'launch-1' }), task('t2', { status: 'completed' })],
			messages, sessions: [], streaming: new Set()
		});
		expect(jobs).toEqual([{ key: 'task:t1', label: 'Fixing the sidebar bug', startedAt: Date.parse(ago(5)), sessionId: 'worker-chat', stop: { kind: 'task', taskId: 't1' } }]);
		const text = JSON.stringify(jobs.map((job) => job.label));
		expect(text).not.toMatch(/Frontend Designer|opus|t1/);
	});

	it('falls back to the summary, then a plain label', () => {
		const jobs = buildBackgroundJobs({ tasks: [task('a', { subject: '', summary: 'Checking CI' }), task('b', { subject: ' ', summary: null })], messages: [], sessions: [], streaming: new Set() });
		expect(jobs.map((job) => job.label)).toEqual(['Checking CI', 'Background job']);
	});

	it('adds chats it started that are still working, and skips idle ones', () => {
		const jobs = buildBackgroundJobs({
			tasks: [],
			messages: [startSession('m1', 'chat-a', 'Release notes'), startSession('m2', 'chat-b'), startSession('m3', 'chat-c')],
			sessions: [
				{ id: 'chat-a', title: 'Writing the release notes', work_started_at: ago(12) },
				{ id: 'chat-b', title: 'Idle chat', remote_runtime_state: 'idle' },
				{ id: 'chat-c', title: 'Parked chat', parked_task_kind: 'shell', created_at: ago(30) }
			],
			streaming: new Map([['chat-a', {}]])
		});
		// chat-a has run 12 min (its work clock); chat-c counts from its StartSession call, 9 min ago.
		expect(jobs.map((job) => [job.label, job.sessionId, job.stop])).toEqual([
			['Writing the release notes', 'chat-a', { kind: 'session', sessionId: 'chat-a' }],
			['Parked chat', 'chat-c', { kind: 'session', sessionId: 'chat-c' }]
		]);
	});

	it('counts a streaming started chat that is not in the loaded list, by its StartSession title', () => {
		const jobs = buildBackgroundJobs({ tasks: [], messages: [startSession('m1', 'chat-x', 'Watching the release checks')], sessions: [], streaming: new Set(['chat-x']) });
		expect(jobs.map((job) => [job.label, job.sessionId])).toEqual([['Watching the release checks', 'chat-x']]);
		expect(buildBackgroundJobs({ tasks: [], messages: [startSession('m1', 'chat-x')], sessions: [], streaming: new Set() })).toEqual([]);
	});

	it('does not list a worker chat twice when a task already covers it', () => {
		const messages: ChatMessage[] = [
			{ id: 'card', role: 'assistant', content: '', type: 'subagent', agentId: 't1', agentSessionId: 'chat-a' },
			startSession('m1', 'chat-a')
		];
		const jobs = buildBackgroundJobs({ tasks: [task('t1')], messages, sessions: [{ id: 'chat-a', title: 'x', remote_runtime_state: 'working' }], streaming: new Set() });
		expect(jobs.map((job) => job.key)).toEqual(['task:t1']);
	});

	it('ignores failed StartSession calls', () => {
		const failed = { ...startSession('m1', 'chat-a'), toolStatus: 'error' as const };
		const jobs = buildBackgroundJobs({ tasks: [], messages: [failed], sessions: [{ id: 'chat-a', remote_runtime_state: 'working' }], streaming: new Set() });
		expect(jobs).toEqual([]);
	});

	it('orders longest-running first', () => {
		const jobs = buildBackgroundJobs({ tasks: [task('new', { startedAt: ago(1) }), task('old', { startedAt: ago(40) })], messages: [], sessions: [], streaming: new Set() });
		expect(jobs.map((job) => job.key)).toEqual(['task:old', 'task:new']);
	});
});

describe('formatJobAge', () => {
	it('speaks in minutes and hours', () => {
		expect(formatJobAge(NOW - 20_000, NOW)).toBe('just started');
		expect(formatJobAge(NOW - 6 * 60_000, NOW)).toBe('6 min');
		expect(formatJobAge(NOW - 65 * 60_000, NOW)).toBe('1 h 5 min');
		expect(formatJobAge(NOW - 120 * 60_000, NOW)).toBe('2 h');
		expect(formatJobAge(null, NOW)).toBeNull();
	});
});
