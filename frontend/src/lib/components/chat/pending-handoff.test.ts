import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { handoffPlaceholder, moveBlocked, pendingHandoff } from './pending-handoff';

const call = (result: unknown, input: Record<string, unknown> = { worker: 'DesktopWorker' }, extra: Partial<ChatMessage> = {}): ChatMessage => ({
	id: `call-${Math.random()}`, role: 'assistant', type: 'tool_use', content: '', toolStatus: 'complete',
	toolName: 'mcp__fluid_bundled__mcp__builtins__HandOffSession', toolInput: input,
	toolResult: (typeof result === 'string' ? result : result) as string, ...extra
});
const note = (status: string, worker = 'DesktopWorker'): ChatMessage => ({
	id: `note-${status}`, role: 'system', type: 'system', content: '', systemSubtype: 'handoff', systemData: { status, worker }
});
const said = (content: string): ChatMessage => ({ id: content, role: 'assistant', type: 'text', content });
const queued = JSON.stringify({ queued: true, worker: 'DesktopWorker', message: 'queued' });

describe('pendingHandoff', () => {
	it('is nothing without a hand-off', () => {
		expect(pendingHandoff([said('hello')])).toBeNull();
	});

	it('reads a queued hand-off from the tool result, as text or object', () => {
		expect(pendingHandoff([call(queued), said('Wrapping up.')])).toEqual({ worker: 'DesktopWorker', phase: 'queued' });
		expect(pendingHandoff([call({ queued: true, worker: 'DesktopWorker' })])).toEqual({ worker: 'DesktopWorker', phase: 'queued' });
	});

	it('follows the recorded rows: started is moving, then moved, failed or timeout end it', () => {
		expect(pendingHandoff([call(queued), note('started')])).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		for (const end of ['moved', 'failed', 'timeout']) {
			expect(pendingHandoff([call(queued), note('started'), note(end)])).toBeNull();
		}
		expect(pendingHandoff([call(queued), note('timeout')])).toBeNull();
	});

	it('reads an owner-queued move and its cancel (#1819)', () => {
		expect(pendingHandoff([said('working'), note('queued')])).toEqual({ worker: 'DesktopWorker', phase: 'queued' });
		expect(pendingHandoff([note('queued'), note('started')])).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		expect(pendingHandoff([note('queued'), note('cancelled')])).toBeNull();
		expect(pendingHandoff([call(queued), note('cancelled', '')])).toBeNull();
	});

	it('ends on a cancel, and a refused call leaves an earlier request standing', () => {
		expect(pendingHandoff([call(queued), call(JSON.stringify({ cancelled: true }), { cancel: true })])).toBeNull();
		expect(pendingHandoff([call(queued), call('Unknown worker', { worker: 'Nope' }, { toolStatus: 'error' })]))
			.toEqual({ worker: 'DesktopWorker', phase: 'queued' });
		expect(pendingHandoff([call('', {}, { toolStatus: 'running', toolResult: undefined })])).toBeNull();
	});

	it('a newer queued hand-off after a finished one is pending again', () => {
		const other = JSON.stringify({ queued: true, worker: 'Unraid' });
		expect(pendingHandoff([call(queued), note('moved'), call(other)])).toEqual({ worker: 'Unraid', phase: 'queued' });
	});
});

describe('#1819 move waits', () => {
	const row = (data: Record<string, unknown>): ChatMessage => ({
		id: `row-${Math.random()}`, role: 'system', type: 'system', content: '', systemSubtype: 'handoff', systemData: data
	});

	it('reads a move queued behind queued messages', () => {
		expect(pendingHandoff([row({ status: 'queued', worker: 'DesktopWorker', after: 'queued' })])).toEqual({
			worker: 'DesktopWorker', phase: 'queued', afterQueued: true
		});
		expect(pendingHandoff([row({ status: 'queued', worker: 'DesktopWorker', after: 'turn' })])).toEqual({
			worker: 'DesktopWorker', phase: 'queued'
		});
	});

	it('tells the composer where new messages go', () => {
		expect(handoffPlaceholder(null)).toBeNull();
		expect(handoffPlaceholder({ worker: 'DesktopWorker', phase: 'queued' })).toBe('Moves to DesktopWorker after this turn');
		expect(handoffPlaceholder({ worker: 'DesktopWorker', phase: 'queued', afterQueued: true })).toBe(
			'Moves to DesktopWorker after the queued messages'
		);
		expect(handoffPlaceholder({ worker: 'DesktopWorker', phase: 'moving' })).toBe('Moving to DesktopWorker…');
	});

	it('ends the wait on a timeout, a refusal or a restart it could not resume', () => {
		const queuedRow = row({ status: 'queued', worker: 'DesktopWorker' });
		for (const status of ['timeout', 'failed', 'cancelled']) {
			expect(pendingHandoff([queuedRow, row({ status, worker: 'DesktopWorker' })])).toBeNull();
		}
	});

	it('counts a wake-up about to fire as busy', () => {
		expect(moveBlocked({ running: false, tasks: [] })).toBe(false);
		expect(moveBlocked({ running: false, tasks: [], queued: 0, wake: true })).toBe(true);
		expect(moveBlocked({ running: true, tasks: [], queued: 2 })).toBe(true);
	});
});
