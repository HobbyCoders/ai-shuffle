import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { pendingHandoff } from './pending-handoff';

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
